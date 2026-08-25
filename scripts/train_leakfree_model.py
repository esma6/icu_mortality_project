#!/usr/bin/env python
"""Leak-free 48h early-window ICU mortality validation.

Trains on `outputs/features/feature_matrix_early_window*` (see
`build_feature_matrix(..., feature_set="early_window")` in spark_etl_mimic.py), which
contains only vitals/labs measured in the first `early_window_hours` of ICU admission
and excludes any ICU-discharge-adjacent feature (LOS, stay count). Splits are grouped
by `subject_id` (patient-level, not admission-level) using StratifiedGroupKFold so no
patient's data spans train and test. This is a separate script from train_models.py
(which trains on the whole-stay compact/timeseries products used for the ETL
performance benchmark) so neither pipeline can accidentally affect the other's outputs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import StratifiedGroupKFold

from src.config import load_config
from src.metrics import (
    brier_skill_score,
    calibration_curve_points,
    calibration_intercept_slope,
    classification_metrics,
)
from src.plotting import plot_calibration_curve, plot_feature_importance, plot_metric_bars, plot_roc_pr_curves
from src.preprocessing import build_model_pipeline, clean_feature_frame
from src.reporting import summarize_mean_std
from train_models import build_estimators, predict_positive_probability


def read_early_window_feature_matrix(feature_dir: str | Path) -> pd.DataFrame:
    """Explicitly reads feature_matrix_early_window_csv (not the fuzzy "latest CSV dir"
    fallback in train_models.read_feature_matrix, which would be ambiguous here since
    outputs/features/ also holds many other feature_matrix_*_csv dirs from ETL-benchmark
    scenario runs)."""
    csv_dir = Path(feature_dir) / "feature_matrix_early_window_csv"
    parts = sorted(csv_dir.glob("part-*.csv"))
    if not parts:
        raise FileNotFoundError(f"No early_window feature matrix found at {csv_dir}")
    return pd.concat((pd.read_csv(p) for p in parts), ignore_index=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train and evaluate the leak-free 48h early-window mortality model")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--cv-folds", type=int, default=None)
    p.add_argument("--n-repeats", type=int, default=None)
    return p.parse_args()


def repeated_holdout_grouped(
    X: pd.DataFrame,
    y: pd.Series,
    groups: pd.Series,
    feature_names: list[str],
    cfg: dict,
    n_repeats: int,
):
    ml_cfg = cfg["ml"]
    base_seed = int(cfg["project"].get("random_state", 42))
    test_size = float(ml_cfg.get("test_size", 0.20))
    n_splits = max(2, round(1.0 / test_size))

    rows = []
    diag_rows = []
    oof_true: Dict[str, list] = {}
    oof_prob: Dict[str, list] = {}
    rf_pipeline = None

    for r in range(n_repeats):
        seed = base_seed + r
        sgkf = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)
        train_idx, test_idx = next(sgkf.split(X, y, groups=groups))

        groups_train = set(groups.iloc[train_idx])
        groups_test = set(groups.iloc[test_idx])
        assert groups_train.isdisjoint(groups_test), (
            f"Patient-level leakage detected in repeat {r}: "
            f"{len(groups_train & groups_test)} subject_id(s) span train and test"
        )
        diag_rows.append({
            "repeat": r, "seed": seed,
            "n_groups_train": len(groups_train), "n_groups_test": len(groups_test),
            "n_rows_train": len(train_idx), "n_rows_test": len(test_idx),
            "positive_rate_train": float(y.iloc[train_idx].mean()),
            "positive_rate_test": float(y.iloc[test_idx].mean()),
        })

        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        estimators = build_estimators(seed)
        for model_name, estimator in estimators.items():
            pipeline = build_model_pipeline(
                estimator=estimator,
                numeric_features=feature_names,
                clip_quantiles=tuple(ml_cfg.get("outlier_clip_quantiles", [0.01, 0.99])),
                scale=True,
                use_feature_selection=bool(ml_cfg.get("use_feature_selection", False)),
                k=ml_cfg.get("feature_selection_k", "all"),
            )
            # class_weight="balanced" (see train_models.build_estimators) shifts predicted
            # probabilities away from the true prevalence. Recalibrate with a 5-fold
            # (non-grouped) internal CV entirely WITHIN X_train/y_train -- X_test is never
            # touched by this step, so the outer patient-level holdout guarantee above is
            # unaffected.
            calibrated = CalibratedClassifierCV(pipeline, method="sigmoid", cv=5)
            calibrated.fit(X_train, y_train)
            y_prob = predict_positive_probability(calibrated, X_test)
            m = classification_metrics(y_test, y_prob, threshold=float(ml_cfg.get("probability_threshold", 0.5)))
            m.update(calibration_intercept_slope(y_test, y_prob))
            m["brier_skill_score"] = brier_skill_score(y_test, y_prob)
            m.update({"model": model_name, "seed": seed, "repeat": r, "split": "holdout"})
            rows.append(m)

            oof_true.setdefault(model_name, []).append(y_test.to_numpy())
            oof_prob.setdefault(model_name, []).append(y_prob)
            if r == 0 and model_name == "Random Forest":
                pipeline.fit(X_train, y_train)
                rf_pipeline = pipeline

    curve_data = {
        name: {"y_true": np.concatenate(oof_true[name]), "y_prob": np.concatenate(oof_prob[name])}
        for name in oof_true
    }
    calib_data = {
        name: calibration_curve_points(curve_data[name]["y_true"], curve_data[name]["y_prob"])
        for name in curve_data
    }
    return pd.DataFrame(rows), pd.DataFrame(diag_rows), curve_data, calib_data, rf_pipeline


def cross_validate_models_grouped(
    X: pd.DataFrame,
    y: pd.Series,
    groups: pd.Series,
    feature_names: list[str],
    cfg: dict,
    cv_folds: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    ml_cfg = cfg["ml"]
    seed = int(cfg["project"].get("random_state", 42))
    sgkf = StratifiedGroupKFold(n_splits=cv_folds, shuffle=True, random_state=seed)
    rows = []
    diag_rows = []

    for fold, (train_idx, test_idx) in enumerate(sgkf.split(X, y, groups=groups), start=1):
        groups_train = set(groups.iloc[train_idx])
        groups_test = set(groups.iloc[test_idx])
        assert groups_train.isdisjoint(groups_test), (
            f"Patient-level leakage detected in CV fold {fold}: "
            f"{len(groups_train & groups_test)} subject_id(s) span train and test"
        )
        diag_rows.append({
            "fold": fold,
            "n_groups_train": len(groups_train), "n_groups_test": len(groups_test),
            "n_rows_train": len(train_idx), "n_rows_test": len(test_idx),
            "positive_rate_train": float(y.iloc[train_idx].mean()),
            "positive_rate_test": float(y.iloc[test_idx].mean()),
        })

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
            # Same train-only recalibration as repeated_holdout_grouped; see comment there.
            calibrated = CalibratedClassifierCV(pipeline, method="sigmoid", cv=5)
            calibrated.fit(X_train, y_train)
            y_prob = predict_positive_probability(calibrated, X_test)
            m = classification_metrics(y_test, y_prob, threshold=float(ml_cfg.get("probability_threshold", 0.5)))
            m.update(calibration_intercept_slope(y_test, y_prob))
            m["brier_skill_score"] = brier_skill_score(y_test, y_prob)
            m.update({"model": model_name, "fold": fold, "split": "cv"})
            rows.append(m)
    return pd.DataFrame(rows), pd.DataFrame(diag_rows)


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

    table_dir = Path(cfg["paths"]["output_dir"]) / "tables_ml_leakfree"
    figure_dir = Path(cfg["paths"]["output_dir"]) / "figures_ml_leakfree"
    table_dir.mkdir(parents=True, exist_ok=True)
    figure_dir.mkdir(parents=True, exist_ok=True)

    cv_folds = args.cv_folds or int(cfg["ml"].get("cv_folds", 5))
    n_repeats = args.n_repeats or int(cfg["ml"].get("n_repeats", 30))

    feature_dir = Path(cfg["paths"]["feature_dir"])
    df = read_early_window_feature_matrix(feature_dir)
    n_total = len(df)
    X, y, feature_names = clean_feature_frame(df, label_col=cfg["ml"].get("label_col", "mortality_label"))
    groups = df.loc[X.index, "subject_id"]

    cohort_flow = pd.DataFrame([
        {"stage": "qualifying_admissions_los_ge_48h", "n_rows": n_total, "n_patients": df["subject_id"].nunique()},
        {"stage": "after_label_dropna", "n_rows": len(X), "n_patients": groups.nunique()},
    ])
    cohort_flow.to_csv(table_dir / "cohort_flow.csv", index=False)

    class_balance = y.value_counts(normalize=True).rename("proportion").reset_index()
    class_balance.columns = ["mortality_label", "proportion"]
    class_balance.to_csv(table_dir / "class_balance.csv", index=False)

    missing_report = X.isna().mean().sort_values(ascending=False).rename("missing_fraction").reset_index()
    missing_report.columns = ["feature", "missing_fraction"]
    missing_report.to_csv(table_dir / "missingness_report.csv", index=False)

    holdout_df, holdout_diag, curve_data, calib_data, rf_pipeline = repeated_holdout_grouped(
        X, y, groups, feature_names, cfg, n_repeats=n_repeats,
    )
    holdout_df.to_csv(table_dir / "holdout_metrics_by_seed.csv", index=False)
    holdout_diag.to_csv(table_dir / "split_diagnostics_holdout.csv", index=False)
    metric_cols = [
        "accuracy", "balanced_accuracy", "precision", "recall_sensitivity", "specificity",
        "f1", "mcc", "auroc", "auprc", "brier_score", "brier_skill_score",
        "calibration_intercept", "calibration_slope",
    ]
    holdout_summary = summarize_mean_std(holdout_df, group_cols=["model"], value_cols=metric_cols, digits=4)
    holdout_summary.to_csv(table_dir / "holdout_metrics_summary.csv", index=False)

    cv_df, cv_diag = cross_validate_models_grouped(X, y, groups, feature_names, cfg, cv_folds=cv_folds)
    cv_df.to_csv(table_dir / "cv_metrics_by_fold.csv", index=False)
    cv_diag.to_csv(table_dir / "split_diagnostics_cv.csv", index=False)
    cv_summary = summarize_mean_std(cv_df, group_cols=["model"], value_cols=metric_cols, digits=4)
    cv_summary.to_csv(table_dir / "cv_metrics_summary.csv", index=False)

    importance_csv = table_dir / "random_forest_feature_importance.csv"
    save_feature_importance(rf_pipeline, feature_names, importance_csv)

    plot_roc_pr_curves(curve_data, figure_dir / "figure_ml_roc_pr_curves.png", label_prefix="Figure 6")
    plot_calibration_curve(
        calib_data, figure_dir / "figure_ml_calibration_curve.png",
        title="Calibration curve — 48h early-window landmark cohort (pooled out-of-fold)",
    )
    plot_metric_bars(table_dir / "holdout_metrics_summary.csv", figure_dir / "figure_ml_model_metrics.png")
    plot_feature_importance(importance_csv, figure_dir / "figure_ml_feature_importance.png")

    print("[OK] Leak-free ML outputs written to", table_dir, "and", figure_dir)
    print(f"[OK] Cohort: {len(X)} admissions, {groups.nunique()} unique patients, "
          f"mortality prevalence {y.mean():.4f}")


if __name__ == "__main__":
    main()
