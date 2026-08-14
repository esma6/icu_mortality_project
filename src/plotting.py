from __future__ import annotations

from pathlib import Path
from typing import Dict

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import PrecisionRecallDisplay, RocCurveDisplay


def savefig(path: str | Path, dpi: int = 300) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close()


def plot_etl_stage_times(summary_csv: str | Path, out_png: str | Path) -> None:
    df = pd.read_csv(summary_csv)
    # Expected columns: scenario, node_count, extract_seconds_mean, transform_seconds_mean, load_seconds_mean, total_seconds_mean
    df = df.sort_values("node_count")
    x = np.arange(len(df))
    extract = df["extract_seconds_mean"].to_numpy()
    transform = df["transform_seconds_mean"].to_numpy()
    load = df["load_seconds_mean"].to_numpy()

    plt.figure(figsize=(7.2, 4.6))
    plt.bar(x, extract, label="Extract")
    plt.bar(x, transform, bottom=extract, label="Transform")
    plt.bar(x, load, bottom=extract + transform, label="Load")

    for i, row in df.iterrows():
        idx = list(df.index).index(i)
        total = row["total_seconds_mean"]
        std = row.get("total_seconds_std", np.nan)
        label = f"{total:.1f} s"
        if not np.isnan(std):
            label += f" ± {std:.1f}"
        plt.text(idx, total + max(df["total_seconds_mean"]) * 0.02, label, ha="center", va="bottom", fontsize=8)

    plt.xticks(x, df["scenario"].tolist())
    plt.ylabel("Execution time (s)")
    plt.xlabel("Strong-scaling scenario")
    plt.title("ETL execution time by pipeline stage")
    plt.legend(frameon=False)
    savefig(out_png)


def plot_strong_scaling(scaling_csv: str | Path, out_png: str | Path) -> None:
    df = pd.read_csv(scaling_csv).sort_values("node_count")
    nodes = df["node_count"].to_numpy()

    plt.figure(figsize=(6.8, 4.6))
    plt.plot(nodes, nodes, marker="o", linestyle="--", label="Ideal linear speedup")
    plt.plot(nodes, df["speedup_mean"], marker="o", label="Observed speedup")
    plt.xlabel("Node count")
    plt.ylabel("Speedup")
    plt.title("Strong-scaling speedup")
    plt.xticks(nodes)
    plt.legend(frameon=False)
    savefig(out_png)

    efficiency_path = Path(out_png).with_name(Path(out_png).stem + "_efficiency.png")
    plt.figure(figsize=(6.8, 4.6))
    plt.plot(nodes, 100 * df["efficiency_mean"], marker="o")
    plt.xlabel("Node count")
    plt.ylabel("Scaling efficiency (%)")
    plt.title("Strong-scaling efficiency")
    plt.xticks(nodes)
    savefig(efficiency_path)


def plot_resource_usage(resource_csvs: list[str | Path], out_png: str | Path) -> None:
    frames = []
    for path in resource_csvs:
        p = Path(path)
        if p.exists() and p.stat().st_size > 0:
            df = pd.read_csv(p)
            if not df.empty:
                df["source"] = p.stem
                df["elapsed_min"] = (df["timestamp"] - df["timestamp"].iloc[0]) / 60.0
                frames.append(df)
    if not frames:
        return

    data = pd.concat(frames, ignore_index=True)
    fig, axes = plt.subplots(3, 1, figsize=(8.2, 8.2), sharex=True)
    for source, sub in data.groupby("source"):
        axes[0].plot(sub["elapsed_min"], sub["cpu_percent"], label=source)
        axes[1].plot(sub["elapsed_min"], sub["memory_percent"], label=source)
        axes[2].plot(sub["elapsed_min"], sub["disk_read_mb_s"] + sub["disk_write_mb_s"], label=source)
    axes[0].set_ylabel("CPU (%)")
    axes[1].set_ylabel("RAM (%)")
    axes[2].set_ylabel("Disk I/O (MB/s)")
    axes[2].set_xlabel("Elapsed time (min)")
    axes[0].set_title("Resource usage during ETL execution")
    axes[0].legend(frameon=False, fontsize=8)
    savefig(out_png)


def plot_roc_pr_curves(curve_data: Dict[str, Dict[str, np.ndarray]], out_png: str | Path,
                        label_prefix: str = "Figure 5") -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.4))
    for model_name, d in curve_data.items():
        RocCurveDisplay.from_predictions(
            d["y_true"], d["y_prob"], name=model_name, ax=axes[0]
        )
        PrecisionRecallDisplay.from_predictions(
            d["y_true"], d["y_prob"], name=model_name, ax=axes[1]
        )
    axes[0].set_title(f"{label_prefix}(a). ROC curves")
    axes[1].set_title(f"{label_prefix}(b). Precision-recall curves")
    axes[0].legend(frameon=False, fontsize=8)
    axes[1].legend(frameon=False, fontsize=8)
    savefig(out_png)


def plot_calibration_curve(calibration_data: Dict[str, Dict[str, np.ndarray]],
                            out_png: str | Path, title: str = "Calibration (reliability) curve") -> None:
    plt.figure(figsize=(6.4, 6.0))
    plt.plot([0, 1], [0, 1], linestyle="--", color="gray", label="Perfectly calibrated")
    for model_name, d in calibration_data.items():
        plt.plot(d["mean_predicted"], d["frac_positive"], marker="o", label=model_name)
    plt.xlabel("Mean predicted probability")
    plt.ylabel("Observed frequency")
    plt.title(title)
    plt.legend(frameon=False, fontsize=8)
    savefig(out_png)


def plot_metric_bars(summary_csv: str | Path, out_png: str | Path) -> None:
    df = pd.read_csv(summary_csv)
    metrics = ["auroc", "auprc", "f1", "recall_sensitivity", "specificity"]
    # Use mean columns from grouped summary.
    keep = ["model"] + [f"{m}_mean" for m in metrics if f"{m}_mean" in df.columns]
    df = df[keep].set_index("model")
    ax = df.plot(kind="bar", figsize=(8, 4.8))
    ax.set_ylabel("Metric value")
    ax.set_title("Model performance summary")
    ax.legend([c.replace("_mean", "") for c in df.columns], frameon=False, fontsize=8)
    plt.xticks(rotation=0)
    savefig(out_png)


def plot_feature_importance(importance_csv: str | Path, out_png: str | Path, top_n: int = 15) -> None:
    path = Path(importance_csv)
    if not path.exists():
        return
    df = pd.read_csv(path).sort_values("importance", ascending=False).head(top_n)
    if df.empty:
        return
    plt.figure(figsize=(7.4, 5.0))
    plt.barh(df["feature"][::-1], df["importance"][::-1])
    plt.xlabel("Importance")
    plt.title("Random Forest feature importance")
    savefig(out_png)
