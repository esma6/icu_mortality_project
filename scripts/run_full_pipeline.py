#!/usr/bin/env python
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str]) -> None:
    print("\n$", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Run the full revised ICU mortality pipeline")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--cv-folds", type=int, default=5)
    p.add_argument("--ml-repeats", type=int, default=30)
    p.add_argument("--skip-etl", action="store_true")
    p.add_argument("--skip-ml", action="store_true")
    args = p.parse_args()

    run([sys.executable, "scripts/system_info.py", "--out", "outputs/reports/system_info.json"])

    if not args.skip_etl:
        run([sys.executable, "scripts/run_etl_experiments.py", "--config", args.config, "--repeats", str(args.repeats)])
        run([sys.executable, "scripts/make_figures.py", "--config", args.config])

    if not args.skip_ml:
        run([
            sys.executable,
            "scripts/train_models.py",
            "--config", args.config,
            "--cv-folds", str(args.cv_folds),
            "--n-repeats", str(args.ml_repeats),
        ])

    print("\n[OK] Full pipeline completed.")


if __name__ == "__main__":
    main()
