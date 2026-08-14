#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.plotting import plot_etl_stage_times, plot_resource_usage, plot_strong_scaling


def parse_args():
    p = argparse.ArgumentParser(description="Generate publication-ready figures from experiment outputs")
    p.add_argument("--config", default="config.yaml")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    table_dir = Path(cfg["paths"]["table_dir"])
    figure_dir = Path(cfg["paths"]["figure_dir"])
    log_dir = Path(cfg["paths"]["log_dir"])
    figure_dir.mkdir(parents=True, exist_ok=True)

    etl_summary = table_dir / "etl_timing_summary.csv"
    scaling_summary = table_dir / "strong_scaling_summary.csv"

    if etl_summary.exists():
        plot_etl_stage_times(etl_summary, figure_dir / "figure_3_etl_stage_times.png")
    else:
        print(f"[WARN] Missing {etl_summary}")

    if scaling_summary.exists():
        plot_strong_scaling(scaling_summary, figure_dir / "figure_4_strong_scaling.png")
    else:
        print(f"[WARN] Missing {scaling_summary}")

    resource_csvs = sorted(log_dir.glob("resource_usage_*.csv"))
    if resource_csvs:
        plot_resource_usage(resource_csvs, figure_dir / "figure_resource_cpu_memory_disk.png")
    else:
        print("[WARN] No resource usage logs found")

    print("[OK] Figures written to", figure_dir)


if __name__ == "__main__":
    main()
