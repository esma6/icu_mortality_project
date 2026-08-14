from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "paper_revision" / "generated"
OUT.mkdir(parents=True, exist_ok=True)

SCENARIO_ORDER = ["1-node", "1-node-4c", "1-node-8c", "2-node", "3-node"]
DISPLAY = {
    "1-node": "local[2]",
    "1-node-4c": "local[4]",
    "1-node-8c": "local[8]",
    "2-node": "2 düğüm",
    "3-node": "3 düğüm",
}
PHASES = ["extract_seconds", "transform_seconds", "load_seconds", "total_seconds"]


def load_workload(name: str, path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df[df["run_id"] != "r01"].copy()
    df["workload"] = name
    df["scenario"] = pd.Categorical(df["scenario"], SCENARIO_ORDER, ordered=True)
    return df


def hedges_g(x: np.ndarray, y: np.ndarray) -> float:
    nx, ny = len(x), len(y)
    vx, vy = np.var(x, ddof=1), np.var(y, ddof=1)
    sp = np.sqrt(((nx - 1) * vx + (ny - 1) * vy) / (nx + ny - 2))
    if sp == 0:
        return 0.0
    d = (np.mean(x) - np.mean(y)) / sp
    correction = 1 - 3 / (4 * (nx + ny) - 9)
    return float(correction * d)


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (workload, scenario), g in df.groupby(["workload", "scenario"], observed=True):
        row = {"workload": workload, "scenario": str(scenario), "n": len(g)}
        for col in PHASES:
            q1, med, q3 = g[col].quantile([0.25, 0.5, 0.75])
            row.update(
                {
                    f"{col}_mean": g[col].mean(),
                    f"{col}_std": g[col].std(ddof=1),
                    f"{col}_median": med,
                    f"{col}_q1": q1,
                    f"{col}_q3": q3,
                }
            )
        rows.append(row)
    out = pd.DataFrame(rows)
    out["scenario"] = pd.Categorical(out["scenario"], SCENARIO_ORDER, ordered=True)
    return out.sort_values(["workload", "scenario"])


def inferential(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    omnibus = []
    pairwise = []
    for workload, wd in df.groupby("workload"):
        groups = [wd.loc[wd["scenario"] == s, "total_seconds"].to_numpy() for s in SCENARIO_ORDER]
        f, p = stats.f_oneway(*groups)
        h, pk = stats.kruskal(*groups)
        lev_f, lev_p = stats.levene(*groups, center="median")

        # Welch's heteroscedastic one-way ANOVA (no equal-variance assumption).
        n = np.asarray([len(g) for g in groups], dtype=float)
        means = np.asarray([np.mean(g) for g in groups], dtype=float)
        variances = np.asarray([np.var(g, ddof=1) for g in groups], dtype=float)
        weights = n / variances
        w_sum = weights.sum()
        weighted_mean = np.sum(weights * means) / w_sum
        k = len(groups)
        correction_sum = np.sum(((1.0 - weights / w_sum) ** 2) / (n - 1.0))
        welch_f = np.sum(weights * (means - weighted_mean) ** 2) / (k - 1.0)
        welch_f /= 1.0 + (2.0 * (k - 2.0) / (k**2 - 1.0)) * correction_sum
        welch_df1 = k - 1.0
        welch_df2 = (k**2 - 1.0) / (3.0 * correction_sum)
        welch_p = stats.f.sf(welch_f, welch_df1, welch_df2)

        omnibus.append({
            "workload": workload,
            "anova_F": f,
            "anova_p": p,
            "brown_forsythe_F": lev_f,
            "brown_forsythe_p": lev_p,
            "welch_F": welch_f,
            "welch_df1": welch_df1,
            "welch_df2": welch_df2,
            "welch_p": welch_p,
            "kruskal_H": h,
            "kruskal_p": pk,
        })

        local8 = wd.loc[wd["scenario"] == "1-node-8c", "total_seconds"].to_numpy()
        for other in ["1-node", "1-node-4c", "2-node", "3-node"]:
            y = wd.loc[wd["scenario"] == other, "total_seconds"].to_numpy()
            t, pval = stats.ttest_ind(local8, y, equal_var=False)
            pairwise.append(
                {
                    "workload": workload,
                    "comparison": f"local[8] vs {DISPLAY[other]}",
                    "mean_difference_s": float(np.mean(local8) - np.mean(y)),
                    "percent_difference_vs_other": float((np.mean(local8) / np.mean(y) - 1) * 100),
                    "welch_t": t,
                    "p_raw": pval,
                    "hedges_g": hedges_g(local8, y),
                }
            )
    pair = pd.DataFrame(pairwise)
    pair["p_holm"] = np.nan
    for workload, idx in pair.groupby("workload").groups.items():
        idx = list(idx)
        pvals = pair.loc[idx, "p_raw"].to_numpy(dtype=float)
        order = np.argsort(pvals)
        adjusted_sorted = np.maximum.accumulate((len(pvals) - np.arange(len(pvals))) * pvals[order])
        adjusted = np.empty_like(adjusted_sorted)
        adjusted[order] = np.minimum(adjusted_sorted, 1.0)
        pair.loc[idx, "p_holm"] = adjusted
    return pd.DataFrame(omnibus), pair


def cpu_summary() -> pd.DataFrame:
    ts_runs = pd.read_csv(ROOT / "outputs/tables/timeseries_backup/etl_timing_runs.csv")
    ts_runs = ts_runs[ts_runs["run_id"] != "r01"].copy()
    rows = []
    for _, run in ts_runs.iterrows():
        path = ROOT / str(run["resource_log"]).replace("\\", "/")
        d = pd.read_csv(path)
        rows.append(
            {
                "scenario": run["scenario"],
                "run_id": run["run_id"],
                "mean_cpu_percent": d["cpu_percent"].mean(),
                "median_cpu_percent": d["cpu_percent"].median(),
                "p95_cpu_percent": d["cpu_percent"].quantile(0.95),
                "mean_memory_percent": d["memory_percent"].mean(),
                "peak_memory_percent": d["memory_percent"].max(),
            }
        )
    raw = pd.DataFrame(rows)
    summary = raw.groupby("scenario", as_index=False).agg(
        n=("run_id", "count"),
        cpu_mean=("mean_cpu_percent", "mean"),
        cpu_std=("mean_cpu_percent", "std"),
        cpu_p95_mean=("p95_cpu_percent", "mean"),
        memory_mean=("mean_memory_percent", "mean"),
        memory_peak_max=("peak_memory_percent", "max"),
    )
    raw.to_csv(OUT / "cpu_by_run.csv", index=False)
    return summary


def main() -> None:
    compact = load_workload("Compact", ROOT / "outputs/tables/compact_backup/etl_timing_runs.csv")
    timeseries = load_workload("Timeseries", ROOT / "outputs/tables/timeseries_backup/etl_timing_runs.csv")
    all_runs = pd.concat([compact, timeseries], ignore_index=True)
    all_runs.to_csv(OUT / "analysis_runs_r02_r06.csv", index=False)

    summary = summarize(all_runs)
    summary.to_csv(OUT / "runtime_summary.csv", index=False)
    omnibus, pairwise = inferential(all_runs)
    omnibus.to_csv(OUT / "omnibus_tests.csv", index=False)
    pairwise.to_csv(OUT / "pairwise_tests_local8.csv", index=False)
    cpu = cpu_summary()
    cpu.to_csv(OUT / "cpu_summary.csv", index=False)

    clip = json.loads((ROOT / "outputs/logs/timeseries_clip_stats.json").read_text(encoding="utf-8"))
    evidence = {
        "compact_feature_rows": sorted(compact["feature_rows"].unique().tolist()),
        "timeseries_feature_rows": sorted(timeseries["feature_rows"].unique().tolist()),
        "input_formats": sorted(all_runs["input_format"].dropna().unique().tolist()),
        "runs_per_workload_scenario": all_runs.groupby(["workload", "scenario"], observed=True).size().to_dict().__str__(),
        "clip_stats": clip,
    }
    (OUT / "evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")

    print(summary.to_string(index=False))
    print("\nOMNIBUS\n", omnibus.to_string(index=False))
    print("\nPAIRWISE\n", pairwise.to_string(index=False))
    print("\nCPU\n", cpu.to_string(index=False))


if __name__ == "__main__":
    main()
