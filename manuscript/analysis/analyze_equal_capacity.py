from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
VAL = ROOT / "outputs" / "validation"
OUT = VAL / "analysis"


def read_manifest(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["total_seconds"] = pd.to_numeric(
        df["total_seconds"].astype(str).str.replace(",", ".", regex=False), errors="coerce"
    )
    return df


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    df = read_manifest(VAL / "local6_standalone2_manifest.csv")
    measured = df[(df.status == "ok") & (df.phase == "measured")].copy()
    if len(measured) != 48:
        raise RuntimeError(f"Expected 48 measured runs (2 workloads x 2 scenarios x 12 blocks), got {len(measured)}")

    rows = []
    for workload, part in measured.groupby("workload"):
        wide = part.pivot(index="block", columns="scenario", values="total_seconds")
        paired = wide[["local6", "standalone2"]].dropna()
        diff = paired["local6"] - paired["standalone2"]
        log_ratio = np.log(paired["local6"] / paired["standalone2"])
        t = stats.ttest_rel(paired["local6"], paired["standalone2"])
        ci = stats.t.interval(0.95, len(diff) - 1, loc=diff.mean(), scale=stats.sem(diff))
        rows.append({
            "workload": workload,
            "local6_n": len(paired), "local6_mean_s": paired["local6"].mean(), "local6_std_s": paired["local6"].std(ddof=1),
            "standalone2_n": len(paired), "standalone2_mean_s": paired["standalone2"].mean(),
            "standalone2_std_s": paired["standalone2"].std(ddof=1),
            "mean_difference_s": diff.mean(), "difference_ci_low": ci[0], "difference_ci_high": ci[1],
            "geometric_ratio_local6_over_standalone2": float(np.exp(log_ratio.mean())),
            "paired_t": t.statistic, "p_value": t.pvalue,
        })

    out = pd.DataFrame(rows)
    out.to_csv(OUT / "local6_vs_standalone2.csv", index=False)
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
