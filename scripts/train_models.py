#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, train_test_split

from src.config import load_config
from src.metrics import classification_metrics
from src.plotting import plot_feature_importance, plot_metric_bars, plot_roc_pr_curves
from src.preprocessing import build_model_pipeline, clean_feature_frame
from src.reporting import summarize_mean_std


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train and evaluate ICU mortality models")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--cv-folds", type=int, default=None)
    p.add_argument("--n-repeats", type=int, default=None)
    return p.parse_args()


def read_feature_matrix(feature_dir: str | Path) -> pd.DataFrame:
    feature_dir = Path(feature_dir)
    canonical_csv_dir = feature_dir / "feature_matrix_csv"
    if canonical_csv_dir.exists():
        csv_parts = sorted(canonical_csv_dir.glob("part-*.csv"))
        if csv_parts:
            return pd.concat((pd.read_csv(p) for p in csv_parts), ignore_index=True)

    # Fallback: use the latest scenario-specific CSV output.
    csv_dirs = sorted(feature_dir.glob("feature_matrix*_csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in csv_dirs:
        parts = sorted(d.glob("part-*.csv"))
        if parts:
            return pd.concat((pd.read_csv(p) for p in parts), ignore_index=True)

    # Optional fallback if pyarrow is installed.
    parquet_path = feature_dir / "feature_matrix.parquet"
    if parquet_path.exists():
        return pd.read_parquet(parquet_path)

    raise FileNotFoundError(f"No feature matrix found in {feature_dir}")


def build_estimators(random_state: int) -> Dict[str, object]:
    return {
        "Logistic Regression": LogisticRegression(
            max_iter=2000,
            class_weight="balanced",
            solver="liblinear",
            random_state=random_state,
        ),
        "Random Forest": RandomForestClassifier(
            n_estimators=300,
            max_depth=None,
            min_samples_leaf=2,
            class_weight="balanced_subsample",
            n_jobs=-1,
            random_state=random_state,
        ),
        "Gradient Boosting": HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=300,
            l2_regularization=0.01,
            class_weight="balanced",
            random_state=random_state,
        ),
    }


def predict_positive_probability(model, X) -> np.ndarray:
    if hasattr(model, "predict_proba"):
        return model.predict_proba(X)[:, 1]
    scores = model.decision_function(X)
    # Logistic transformation fallback.
    return 1 / (1 + np.exp(-scores))


def repeated_holdout(
    X: pd.DataFrame,
    y: pd.Series,
    feature_names: list[str],
    cfg: dict,
    n_repeats: int,
) -> tuple[pd.DataFrame, Dict[str, Dict[str, np.ndarray]], object]:
    ml_cfg = cfg["ml"]
    rows = []
    curve_data: Dict[str, Dict[str, np.ndarray]] = {}
    rf_model_for_importance = None

    for seed in range(int(cfg["project"].get("random_state", 42)), int(cfg["project"].get("random_state", 42)) + n_repeats):
        X_train, X_test, y_train, y_test = train_test_split(
            X,
            y,
            test_size=float(ml_cfg.get("test_size", 0.2)),
            stratify=y,
            random_state=seed,
        )
        estimators = build_estimators(seed)
        for model_name, estimator in estimators.items():
            # Scaling is essential for LR; harmless for tree models because all preprocessing is train-only.
            pipeline = build_model_pipeline(
                estimator=estimator,
                numeric_features=feature_names,
                clip_quantiles=tuple(ml_cfg.get("outlier_clip_quantiles", [0.01, 0.99])),
                scale=True,
                use_feature_selection=bool(ml_cfg.get("use_feature_selection", False)),
                k=ml_cfg.get("feature_selection_k", "all"),
            )
            pipeline.fit(X_train, y_train)
            y_prob = predict_positive_probability(pipeline, X_test)
            m = classification_metrics(y_test, y_prob, threshold=float(ml_cfg.get("probability_threshold", 0.5)))
            m.update({"model": model_name, "seed": seed, "split": "holdout"})
            rows.append(m)

            # Save curves from the base seed only for a clean Figure 5(a,b).
            if seed == int(cfg["project"].get("random_state", 42)):
                curve_data[model_name] = {"y_true": y_test.to_numpy(), "y_prob": y_prob}
                if model_name == "Random Forest":
                    rf_model_for_importance = pipeline

    return pd.DataFrame(rows), curve_data, rf_model_for_importance


def cross_validate_models(
    X: pd.DataFrame,
    y: pd.Series,
    feature_names: list[str],
    cfg: dict,
    cv_folds: int,
) -> pd.DataFrame:
    ml_cfg = cfg["ml"]
    seed = int(cfg["project"].get("random_state", 42))
    skf = StratifiedKFold(n_splits=cv_folds, shuffle=True, random_state=seed)
    rows = []

    for fold, (train_idx, test_idx) in enumerate(skf.split(X, y), start=1):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]
        for model_name, estimator in build_estimators(seed + fold).items():
            pipeline = build_model_pipeline(
                estimator=estimator,
                numeric_features=feature_names,
                clip_quantiles=tuple(ml_cfg.get("outlier_clip_quantiles", [0.01, 0.99])),
                scale=True,
                use_feature_selection=bool(ml_cfg.get("use_feature_selection", False)),
                k=ml_cfg.get("feature_selection_k", "all"),
            )
            pipeline.fit(X_train, y_train)
            y_prob = predict_positive_probability(pipeline, X_test)
            m = classification_metrics(y_test, y_prob, threshold=float(ml_cfg.get("probability_threshold", 0.5)))
            m.update({"model": model_name, "fold": fold, "split": "cv"})
            rows.append(m)
    return pd.DataFrame(rows)


def save_feature_importance(pipeline, feature_names: list[str], out_csv: str | Path) -> None:
    if pipeline is None:
        return
    estimator = pipeline.named_steps.get("model")
    if not hasattr(estimator, "feature_importances_"):
        return

    names = np.asarray(feature_names)
    if "select" in pipeline.named_steps:
        selector = pipeline.named_steps["select"]
        names = names[selector.get_support()]
    imp = pd.DataFrame({"feature": names, "importance": estimator.feature_importances_})
    imp = imp.sort_values("importance", ascending=False)
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    imp.to_csv(out_csv, index=False)


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    table_dir = Path(cfg["paths"]["table_dir"])
    figure_dir = Path(cfg["paths"]["figure_dir"])
    table_dir.mkdir(parents=True, exist_ok=True)
    figure_dir.mkdir(parents=True, exist_ok=True)

    cv_folds = args.cv_folds or int(cfg["ml"].get("cv_folds", 5))
    n_repeats = args.n_repeats or int(cfg["ml"].get("n_repeats", 30))

    df = read_feature_matrix(cfg["paths"]["feature_dir"])
    X, y, feature_names = clean_feature_frame(df, label_col=cfg["ml"].get("label_col", "mortality_label"))

    class_balance = y.value_counts(normalize=True).rename("proportion").reset_index()
    class_balance.columns = ["mortality_label", "proportion"]
    class_balance.to_csv(table_dir / "class_balance.csv", index=False)

    missing_report = X.isna().mean().sort_values(ascending=False).rename("missing_fraction").reset_index()
    missing_report.columns = ["feature", "missing_fraction"]
    missing_report.to_csv(table_dir / "missingness_report.csv", index=False)

    holdout_df, curve_data, rf_pipeline = repeated_holdout(X, y, feature_names, cfg, n_repeats=n_repeats)
    holdout_df.to_csv(table_dir / "holdout_metrics_by_seed.csv", index=False)
    holdout_summary = summarize_mean_std(
        holdout_df,
        group_cols=["model"],
        value_cols=[
            "accuracy", "balanced_accuracy", "precision", "recall_sensitivity", "specificity",
            "f1", "mcc", "auroc", "auprc", "brier_score",
        ],
        digits=4,
    )
    holdout_summary.to_csv(table_dir / "holdout_metrics_summary.csv", index=False)

    cv_df = cross_validate_models(X, y, feature_names, cfg, cv_folds=cv_folds)
    cv_df.to_csv(table_dir / "cv_metrics_by_fold.csv", index=False)
    cv_summary = summarize_mean_std(
        cv_df,
        group_cols=["model"],
        value_cols=[
            "accuracy", "balanced_accuracy", "precision", "recall_sensitivity", "specificity",
            "f1", "mcc", "auroc", "auprc", "brier_score",
        ],
        digits=4,
    )
    cv_summary.to_csv(table_dir / "cv_metrics_summary.csv", index=False)

    importance_csv = table_dir / "random_forest_feature_importance.csv"
    save_feature_importance(rf_pipeline, feature_names, importance_csv)

    plot_roc_pr_curves(curve_data, figure_dir / "figure_5_roc_pr_curves.png")
    plot_metric_bars(table_dir / "holdout_metrics_summary.csv", figure_dir / "figure_6_model_metrics.png")
    plot_feature_importance(importance_csv, figure_dir / "figure_7_feature_importance.png")

    print("[OK] ML outputs written to", table_dir, "and", figure_dir)


if __name__ == "__main__":
    main()
