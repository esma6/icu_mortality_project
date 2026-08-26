"""Regenerate IEEE Fig. 3 (five-fold patient-grouped CV results bar chart)
from the current, patient-grouped-calibration outputs/tables_ml_leakfree/
cv_metrics_summary.csv. The embedded image in the manuscript was stale --
it showed pre-recalibration Brier scores (~0.157/0.196/0.093) instead of
the current, correct ones (~0.090/0.096/0.092).
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

c = pd.read_csv("../../outputs/tables_ml_leakfree/cv_metrics_summary.csv")
order = ["Gradient Boosting", "Logistic Regression", "Random Forest"]
c = c.set_index("model").loc[order].reset_index()

metrics = [
    ("auroc_mean", "auroc_std", "AUROC", "#1f77b4"),
    ("auprc_mean", "auprc_std", "AUPRC", "#ff7f0e"),
    ("brier_score_mean", "brier_score_std", "Brier score", "#2ca02c"),
]

x = np.arange(len(order))
width = 0.25

fig, ax = plt.subplots(figsize=(12.0, 4.0), dpi=239)
for i, (mean_col, std_col, label, color) in enumerate(metrics):
    offset = (i - 1) * width
    ax.bar(
        x + offset, c[mean_col], width, yerr=c[std_col], color=color,
        label=label, capsize=3, error_kw={"elinewidth": 1.2, "ecolor": "black"},
    )

ax.set_xticks(x)
ax.set_xticklabels(order)
ax.set_ylabel("Mean across patient-grouped folds")
ax.set_ylim(0, 0.9)
ax.legend(loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.08))
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.yaxis.grid(True, color="0.9", linewidth=0.8)
ax.set_axisbelow(True)
fig.tight_layout()

out = "image3_new.png"
fig.savefig(out, dpi=239)
print(f"Saved {out}")
print(c[["model", "auroc_mean", "auroc_std", "auprc_mean", "auprc_std", "brier_score_mean", "brier_score_std"]])
