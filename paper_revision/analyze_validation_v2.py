from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
VAL = ROOT / "outputs" / "validation"
OUT = VAL / "analysis"
MANIFEST = VAL / "validation_manifest.csv"


def parse_event_log(path: str | Path) -> dict[str, float]:
    totals = {
        "tasks": 0, "failed_tasks": 0, "executor_run_ms": 0, "executor_cpu_ns": 0,
        "jvm_gc_ms": 0, "memory_spill_bytes": 0, "disk_spill_bytes": 0,
        "shuffle_read_bytes": 0, "shuffle_write_bytes": 0, "input_bytes": 0,
        "output_bytes": 0,
    }
    with Path(path).open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("Event") != "SparkListenerTaskEnd":
                continue
            totals["tasks"] += 1
            if event.get("Task End Reason") != {"Reason": "Success"} and \
                    event.get("Task End Reason", {}).get("Reason") != "Success":
                totals["failed_tasks"] += 1
            m = event.get("Task Metrics") or {}
            totals["executor_run_ms"] += m.get("Executor Run Time", 0) or 0
            totals["executor_cpu_ns"] += m.get("Executor CPU Time", 0) or 0
            totals["jvm_gc_ms"] += m.get("JVM GC Time", 0) or 0
            totals["memory_spill_bytes"] += m.get("Memory Bytes Spilled", 0) or 0
            totals["disk_spill_bytes"] += m.get("Disk Bytes Spilled", 0) or 0
            sr = m.get("Shuffle Read Metrics") or {}
            totals["shuffle_read_bytes"] += (sr.get("Remote Bytes Read", 0) or 0) + \
                                             (sr.get("Local Bytes Read", 0) or 0)
            sw = m.get("Shuffle Write Metrics") or {}
            totals["shuffle_write_bytes"] += sw.get("Shuffle Bytes Written", 0) or 0
            totals["input_bytes"] += (m.get("Input Metrics") or {}).get("Bytes Read", 0) or 0
            totals["output_bytes"] += (m.get("Output Metrics") or {}).get("Bytes Written", 0) or 0
    return totals


def holm(p_values: list[float]) -> list[float]:
    p = np.asarray(p_values, dtype=float)
    order = np.argsort(p)
    adjusted = np.empty_like(p)
    running = 0.0
    m = len(p)
    for rank, idx in enumerate(order):
        running = max(running, (m - rank) * p[idx])
        adjusted[idx] = min(1.0, running)
    return adjusted.tolist()


def paired_contrasts(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for workload, part in df.groupby("workload"):
        wide = part.pivot(index="block", columns="scenario", values="total_seconds")
        for scenario in ["local2", "local4", "standalone1", "standalone2"]:
            paired = wide[["local8", scenario]].dropna()
            diff = paired[scenario] - paired["local8"]
            log_ratio = np.log(paired[scenario] / paired["local8"])
            t = stats.ttest_rel(paired[scenario], paired["local8"])
            ci = stats.t.interval(.95, len(diff) - 1, loc=diff.mean(), scale=stats.sem(diff))
            log_ci = stats.t.interval(.95, len(log_ratio) - 1, loc=log_ratio.mean(), scale=stats.sem(log_ratio))
            rows.append({
                "workload": workload, "comparison": f"{scenario} - local8", "n_blocks": len(diff),
                "mean_difference_s": diff.mean(), "difference_ci_low": ci[0], "difference_ci_high": ci[1],
                "geometric_runtime_ratio": math.exp(log_ratio.mean()),
                "ratio_ci_low": math.exp(log_ci[0]), "ratio_ci_high": math.exp(log_ci[1]),
                "paired_t": t.statistic, "p_raw": t.pvalue,
            })
    out = pd.DataFrame(rows)
    out["p_holm_within_workload"] = np.nan
    for workload, idx in out.groupby("workload").groups.items():
        out.loc[idx, "p_holm_within_workload"] = holm(out.loc[idx, "p_raw"].tolist())
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    runs = pd.read_csv(MANIFEST)
    numeric_cols = ["cpu_budget", "memory_budget_gib", "feature_rows",
                     "extract_seconds", "transform_seconds", "load_seconds", "total_seconds"]
    for col in numeric_cols:
        runs[col] = pd.to_numeric(runs[col].astype(str).str.replace(",", ".", regex=False), errors="coerce")
    expected = 10 + 12 * 10
    if len(runs) != expected or (runs.status != "ok").any():
        raise RuntimeError(f"Validation matrix incomplete: rows={len(runs)}/{expected}, statuses={runs.status.value_counts().to_dict()}")
    measured = runs[runs.phase == "measured"].copy()
    if len(measured) != 120:
        raise RuntimeError(f"Expected 120 measured runs, got {len(measured)}")
    if set(measured.input_format) != {"parquet"}:
        raise RuntimeError("Non-Parquet timing record detected")

    event_rows = []
    for _, row in measured.iterrows():
        metrics = parse_event_log(row.event_log)
        event_rows.append({"run_id": row.run_id, **metrics})
    events = pd.DataFrame(event_rows)
    merged = measured.merge(events, on="run_id", validate="one_to_one")
    merged.to_csv(OUT / "validation_measured_with_event_metrics.csv", index=False)

    summary = merged.groupby(["workload", "scenario"]).agg(
        n=("total_seconds", "size"), total_mean=("total_seconds", "mean"),
        total_std=("total_seconds", "std"), total_median=("total_seconds", "median"),
        extract_mean=("extract_seconds", "mean"), transform_mean=("transform_seconds", "mean"),
        load_mean=("load_seconds", "mean"), executor_cpu_s=("executor_cpu_ns", lambda x: x.mean()/1e9),
        gc_s=("jvm_gc_ms", lambda x: x.mean()/1000),
        shuffle_read_gb=("shuffle_read_bytes", lambda x: x.mean()/1024**3),
        shuffle_write_gb=("shuffle_write_bytes", lambda x: x.mean()/1024**3),
        spill_gb=("disk_spill_bytes", lambda x: x.mean()/1024**3),
        failed_tasks=("failed_tasks", "sum"),
    ).reset_index()
    summary.to_csv(OUT / "validation_summary.csv", index=False)
    paired_contrasts(measured).to_csv(OUT / "validation_paired_contrasts.csv", index=False)

    order = ["local2", "local4", "local8", "standalone1", "standalone2"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), sharey=False)
    for ax, workload in zip(axes, ["compact", "timeseries"]):
        part = measured[measured.workload == workload]
        data = [part.loc[part.scenario == s, "total_seconds"].values for s in order]
        ax.boxplot(data, tick_labels=order, showmeans=True)
        for i, values in enumerate(data, 1):
            rng = np.random.default_rng(20260720 + i)
            ax.scatter(rng.normal(i, .035, len(values)), values, s=15, alpha=.65)
        ax.set_title("Admission-level workload" if workload == "compact" else "Six-hour windowed workload")
        ax.set_ylabel("Total ETL runtime (s)")
        ax.grid(axis="y", linestyle=":", alpha=.45)
        ax.tick_params(axis="x", rotation=25)
    fig.suptitle("Randomized, resource-capped ETL validation experiment (n=12)")
    fig.tight_layout()
    fig.savefig(OUT / "validation_runtime_distribution.png", dpi=200, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
