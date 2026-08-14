from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "paper_revision" / "generated"
GEN.mkdir(parents=True, exist_ok=True)

ORDER = ["1-node", "1-node-4c", "1-node-8c", "2-node", "3-node"]
LABEL = {
    "1-node": "local[2]",
    "1-node-4c": "local[4]",
    "1-node-8c": "local[8]",
    "2-node": "2 düğüm",
    "3-node": "3 düğüm",
}
COLORS = {
    "1-node": "#0072B2",
    "1-node-4c": "#56B4E9",
    "1-node-8c": "#009E73",
    "2-node": "#E69F00",
    "3-node": "#CC79A7",
}


def style(ax):
    ax.grid(True, axis="y", linestyle=":", linewidth=0.7, alpha=0.45)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def runtime_distribution(runs: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12.4, 5.2), sharey=False)
    rng = np.random.default_rng(42)
    for ax, workload in zip(axes, ["Compact", "Timeseries"]):
        d = runs[runs["workload"] == workload]
        data = [d.loc[d["scenario"] == s, "total_seconds"].to_numpy() for s in ORDER]
        bp = ax.boxplot(data, positions=np.arange(len(ORDER)), widths=0.52, patch_artist=True,
                        showfliers=False, medianprops={"color": "#222222", "linewidth": 1.5})
        for patch, s in zip(bp["boxes"], ORDER):
            patch.set_facecolor(COLORS[s]); patch.set_alpha(0.25); patch.set_edgecolor(COLORS[s])
        for i, (vals, s) in enumerate(zip(data, ORDER)):
            jitter = rng.normal(0, 0.045, size=len(vals))
            ax.scatter(np.full(len(vals), i) + jitter, vals, s=34, color=COLORS[s],
                       edgecolor="white", linewidth=0.6, zorder=3)
            ax.scatter(i, vals.mean(), marker="D", s=48, color=COLORS[s], edgecolor="#222222", linewidth=0.5, zorder=4)
        ax.set_xticks(np.arange(len(ORDER)), [LABEL[s] for s in ORDER], rotation=20, ha="right")
        ax.set_title("Compact iş yükü" if workload == "Compact" else "Timeseries iş yükü", fontweight="bold")
        ax.set_ylabel("Toplam ETL süresi (s)")
        style(ax)
    fig.suptitle("İş yükü ve yürütme senaryosuna göre ETL çalışma süresi", fontweight="bold", y=1.02)
    fig.text(0.5, -0.02, "Noktalar ölçüm koşularını (r02-r06), elmaslar ortalamayı gösterir; r01 warmup dışlanmıştır.",
             ha="center", fontsize=9)
    fig.tight_layout()
    fig.savefig(GEN / "sekil_2_calisma_suresi.png", dpi=240, bbox_inches="tight")
    plt.close(fig)


def phase_breakdown(summary: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12.4, 5.0), sharey=False)
    phase_cols = ["extract_seconds_mean", "transform_seconds_mean", "load_seconds_mean"]
    phase_labels = ["Çıkarma", "Dönüşüm", "Yazma"]
    phase_colors = ["#0072B2", "#E69F00", "#009E73"]
    for ax, workload in zip(axes, ["Compact", "Timeseries"]):
        d = summary[summary["workload"] == workload].set_index("scenario").loc[ORDER]
        bottom = np.zeros(len(ORDER))
        for col, lab, color in zip(phase_cols, phase_labels, phase_colors):
            vals = d[col].to_numpy()
            ax.bar(np.arange(len(ORDER)), vals, bottom=bottom, label=lab, color=color, alpha=0.9)
            bottom += vals
        ax.errorbar(np.arange(len(ORDER)), d["total_seconds_mean"], yerr=d["total_seconds_std"],
                    fmt="none", ecolor="#333333", capsize=3, linewidth=1)
        ax.set_xticks(np.arange(len(ORDER)), [LABEL[s] for s in ORDER], rotation=20, ha="right")
        ax.set_title("Compact" if workload == "Compact" else "Timeseries", fontweight="bold")
        ax.set_ylabel("Ortalama süre (s)")
        style(ax)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, frameon=False, bbox_to_anchor=(0.5, -0.03))
    fig.suptitle("ETL aşamalarının toplam süreye katkısı (ortalama ± SS, n=5)", fontweight="bold", y=1.02)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(GEN / "sekil_3_asama_kirilimi.png", dpi=240, bbox_inches="tight")
    plt.close(fig)


def cpu_figure(ts_runs: pd.DataFrame) -> None:
    reps = []
    for s in ORDER:
        d = ts_runs[ts_runs["scenario"] == s].copy()
        med = d["total_seconds"].median()
        reps.append(d.iloc[(d["total_seconds"] - med).abs().argsort().iloc[0]])

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.8, 5.2), gridspec_kw={"width_ratios": [2.15, 1]})
    cpu_rows = []
    for run in reps:
        s = run["scenario"]
        p = ROOT / str(run["resource_log"]).replace("\\", "/")
        d = pd.read_csv(p)
        progress = np.linspace(0, 100, len(d))
        y = d["cpu_percent"].rolling(5, center=True, min_periods=1).mean()
        ax1.plot(progress, y, label=f"{LABEL[s]} ({run['run_id']})", color=COLORS[s], linewidth=1.45)

    for s in ORDER:
        d_runs = ts_runs[ts_runs["scenario"] == s]
        vals = []
        for _, run in d_runs.iterrows():
            p = ROOT / str(run["resource_log"]).replace("\\", "/")
            vals.append(pd.read_csv(p)["cpu_percent"].mean())
        cpu_rows.append((np.mean(vals), np.std(vals, ddof=1)))

    ax1.set_xlabel("Koşu ilerlemesi (%)")
    ax1.set_ylabel("Host CPU kullanımı (%)")
    ax1.set_xlim(0, 100); ax1.set_ylim(0, 105)
    ax1.legend(frameon=False, ncol=2, fontsize=8.5)
    style(ax1)

    means = [x[0] for x in cpu_rows]; stds = [x[1] for x in cpu_rows]
    ax2.bar(np.arange(len(ORDER)), means, yerr=stds, capsize=3,
            color=[COLORS[s] for s in ORDER], alpha=0.85)
    ax2.set_xticks(np.arange(len(ORDER)), [LABEL[s] for s in ORDER], rotation=25, ha="right")
    ax2.set_ylabel("Ortalama host CPU kullanımı (%)")
    ax2.set_ylim(0, 100)
    style(ax2)
    for i, m in enumerate(means):
        ax2.text(i, m + stds[i] + 2, f"{m:.1f}", ha="center", va="bottom", fontsize=8)

    fig.suptitle("ETL koşusu boyunca host CPU kullanımı", fontweight="bold", y=1.02)
    fig.text(0.5, -0.02, "Sol: senaryo başına medyan süreye en yakın temsili koşu. Sağ: ölçüm koşularının ortalaması ± SS (n=5).",
             ha="center", fontsize=9)
    fig.tight_layout()
    fig.savefig(GEN / "sekil_4_cpu_kullanimi.png", dpi=240, bbox_inches="tight")
    plt.close(fig)


def architecture() -> None:
    fig, ax = plt.subplots(figsize=(12, 4.6))
    ax.axis("off")
    stages = [
        (0.04, 0.58, 0.18, 0.23, "Parquet girdileri\n330,7 M CHARTEVENTS\n22,2 M LABEVENTS", "#DCEAF7"),
        (0.29, 0.58, 0.18, 0.23, "Doğrulama ve\nklinik aralık filtresi", "#E8E8E8"),
        (0.54, 0.58, 0.18, 0.23, "Compact: hadm_id\nTimeseries: hadm_id + 6 saat", "#F8E6C4"),
        (0.79, 0.58, 0.17, 0.23, "Kalıcı özellik ürünü\n58.976 / 1.180.395 satır", "#DDF1E5"),
    ]
    for x, y, w, h, text, color in stages:
        rect = plt.Rectangle((x, y), w, h, facecolor=color, edgecolor="#555555", linewidth=1.0)
        ax.add_patch(rect); ax.text(x+w/2, y+h/2, text, ha="center", va="center", fontsize=9.5)
    for x in [0.23, 0.48, 0.73]:
        ax.annotate("", xy=(x+0.05, 0.695), xytext=(x, 0.695), arrowprops=dict(arrowstyle="->", color="#555555", lw=1.5))

    ax.text(0.04, 0.40, "Yürütme senaryoları", fontweight="bold", fontsize=10)
    scenarios = ["local[2]", "local[4]", "local[8]", "Master + 1 worker", "Master + 2 worker"]
    for i, s in enumerate(scenarios):
        x = 0.04 + i * 0.19
        ax.add_patch(plt.Rectangle((x, 0.16), 0.16, 0.13, facecolor=COLORS[ORDER[i]], alpha=0.22,
                                   edgecolor=COLORS[ORDER[i]], linewidth=1.2))
        ax.text(x+0.08, 0.225, s, ha="center", va="center", fontsize=9)
    ax.text(0.5, 0.04, "Tüm senaryolar aynı Windows hostu, SSD'yi ve 12 mantıksal çekirdeği paylaşır.",
            ha="center", fontsize=9, style="italic", color="#444444")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    fig.savefig(GEN / "sekil_1_mimari.png", dpi=240, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    runs = pd.read_csv(GEN / "analysis_runs_r02_r06.csv")
    summary = pd.read_csv(GEN / "runtime_summary.csv")
    ts = runs[runs["workload"] == "Timeseries"].copy()
    runtime_distribution(runs)
    phase_breakdown(summary)
    cpu_figure(ts)
    architecture()
    print("generated", sorted(p.name for p in GEN.glob("sekil_*.png")))


if __name__ == "__main__":
    main()
