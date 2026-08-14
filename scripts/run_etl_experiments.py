#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from src.config import load_config
from src.monitoring import ResourceMonitor
from src.reporting import ensure_dirs, summarize_mean_std


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run repeated ETL strong-scaling experiments")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--repeats", type=int, default=None)
    p.add_argument("--skip-docker-control", action="store_true", help="Do not start/stop Docker workers automatically")
    p.add_argument("--distributed-in-container", action="store_true", default=True,
                   help="Run 2-node/3-node Spark driver inside spark-master container so Docker workers can access /data/mimic and /app/outputs")
    return p.parse_args()


def run_cmd(cmd: list[str], cwd: Path, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd), flush=True)
    cp = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    # Always print child output. This prevents hidden Spark/Docker errors.
    if (cp.stdout or "").strip():
        print(cp.stdout)
    if (cp.stderr or "").strip():
        print(cp.stderr)
    if check and cp.returncode != 0:
        raise subprocess.CalledProcessError(
            cp.returncode,
            cmd,
            output=cp.stdout or "",
            stderr=cp.stderr or "",
        )
    return cp


def docker_compose(args: list[str], root: Path) -> None:
    try:
        run_cmd(["docker", "compose", *args], cwd=root, check=True)
    except Exception as exc:
        print(f"[WARN] Docker command failed: {exc}")


def set_active_workers(active_workers: int, root: Path) -> None:
    # Baseline (active_workers=0) dahil tüm senaryolar spark-master
    # container'ının içinde çalıştığı için, master her durumda ayakta olmalıdır.
    # Yalnızca aktif worker sayısı senaryoya göre değişir.
    docker_compose(["up", "-d", "spark-master"], root)

    if active_workers <= 0:
        # Baseline: master içinde local[*], hiçbir worker aktif değil.
        docker_compose(["stop", "spark-worker-1", "spark-worker-2"], root)
        time.sleep(8)
        return

    services = []
    if active_workers >= 1:
        services.append("spark-worker-1")
    if active_workers >= 2:
        services.append("spark-worker-2")
    docker_compose(["up", "-d", *services], root)
    if active_workers == 1:
        docker_compose(["stop", "spark-worker-2"], root)
    time.sleep(8)


def extract_json_from_stdout(stdout: str) -> Dict:
    """Parse the JSON object printed by spark_etl_mimic.py from noisy Spark output."""
    stdout = stdout or ""
    # Prefer the last complete JSON-looking block.
    starts = [i for i, ch in enumerate(stdout) if ch == "{"]
    for start in reversed(starts):
        candidate = stdout[start:].strip()
        try:
            return json.loads(candidate)
        except Exception:
            continue
    raise ValueError("Could not parse JSON result from ETL stdout")


def make_local_cmd(master: str, scenario: Dict, run_id: str, output_suffix: str) -> list[str]:
    return [
        sys.executable,
        "scripts/spark_etl_mimic.py",
        "--config", "config.yaml",
        "--master", master,
        "--scenario", scenario["name"],
        "--run-id", run_id,
        "--output-suffix", output_suffix,
    ]


def make_container_cmd(master: str, scenario: Dict, run_id: str, output_suffix: str) -> list[str]:
    # Driver spark-master container içinde çalışır.
    # Bu nedenle executor'lar driver'a host.docker.internal ile değil,
    # spark-master container adıyla bağlanmalıdır.
    #
    # master, local[*] veya spark://spark-master:7077 olabilir. Her iki durumda da
    # driver spark-master container'ının içinde çalışır; böylece baseline (local[*])
    # ve dağıtık senaryolar AYNI yürütme ortamını (Linux, Docker G/Ç, /data ve /app
    # mount'ları) paylaşır. Tek fark aktif worker sayısıdır.
    return [
        "docker", "compose", "exec", "-T", "-w", "/app",
        "-e", "MIMIC_DIR=/data/mimic",
        "-e", "PYTHONPATH=/app",
        "-e", "SPARK_DRIVER_HOST=spark-master",
        "spark-master",
        "/opt/spark/bin/spark-submit",
        "--master", master,
        "--conf", "spark.driver.host=spark-master",
        "--conf", "spark.driver.bindAddress=0.0.0.0",
        "--conf", "spark.executorEnv.PYTHONPATH=/app",
        "--conf", "spark.pyspark.python=python3",
        "--conf", "spark.pyspark.driver.python=python3",
        "/app/scripts/spark_etl_mimic.py",
        "--config", "/app/config.yaml",
        "--master", master,
        "--scenario", scenario["name"],
        "--run-id", run_id,
        "--output-suffix", output_suffix,
    ]

def run_one_etl(cfg: Dict, scenario: Dict, run_id: str, output_suffix: str, distributed_in_container: bool) -> Dict:
    master = cfg["spark"][scenario["master_key"]]
    # Artık local[*] baseline dahil TÜM senaryolar spark-master container'ının
    # içinde çalışır. Böylece yürütme ortamı üç yapılandırmada da aynıdır ve
    # yalnızca aktif worker sayısı değişir. make_local_cmd (host üzerinde
    # çalıştırma) yalnızca distributed_in_container=False ile açıkça istenirse
    # kullanılır; varsayılan davranış container-içi çalıştırmadır.
    if distributed_in_container:
        cmd = make_container_cmd(master, scenario, run_id, output_suffix)
    else:
        cmd = make_local_cmd(master, scenario, run_id, output_suffix)

    log_dir = Path(cfg["paths"]["log_dir"])
    monitor_path = log_dir / f"resource_usage_{scenario['name']}_{run_id}.csv"
    monitor = None
    if cfg.get("monitoring", {}).get("enabled", True):
        monitor = ResourceMonitor(monitor_path, cfg.get("monitoring", {}).get("interval_seconds", 1.0))
        monitor.start()
    try:
        env = os.environ.copy()
        env.setdefault("SPARK_LOCAL_IP", "127.0.0.1")
        env.setdefault("SPARK_DRIVER_HOST", "host.docker.internal")
        cp = run_cmd(cmd, cwd=ROOT, env=env, check=True)
        try:
            result = extract_json_from_stdout(cp.stdout or "")
        except Exception:
            timing_path = log_dir / f"etl_timing_{scenario['name']}_{run_id}.json"
            result = json.loads(timing_path.read_text(encoding="utf-8"))
        result["resource_log"] = str(monitor_path)
        return result
    finally:
        if monitor is not None:
            monitor.stop()


def summarize_results(results: List[Dict], cfg: Dict) -> None:
    table_dir = Path(cfg["paths"]["table_dir"])
    ensure_dirs(table_dir)

    rows = []
    for r in results:
        rows.append({
            "scenario": r["scenario"],
            "run_id": r["run_id"],
            "node_count": next(s["node_count"] for s in cfg["experiments"]["scenarios"] if s["name"] == r["scenario"]),
            "spark_master": r["spark_master"],
            "input_format": r.get("input_format", "csv"),
            "extract_seconds": r["extract_seconds"],
            "transform_seconds": r["transform_seconds"],
            "load_seconds": r["load_seconds"],
            "total_seconds": r["total_seconds"],
            "feature_rows": r.get("feature_rows"),
            "resource_log": r.get("resource_log"),
        })

    runs = pd.DataFrame(rows).sort_values(["node_count", "run_id"])
    runs.to_csv(table_dir / "etl_timing_runs.csv", index=False)

    summary = summarize_mean_std(
        runs,
        group_cols=["scenario", "node_count"],
        value_cols=["extract_seconds", "transform_seconds", "load_seconds", "total_seconds", "feature_rows"],
        digits=3,
    ).sort_values("node_count")
    summary.to_csv(table_dir / "etl_timing_summary.csv", index=False)

    baseline = summary.loc[summary["node_count"] == 1, "total_seconds_mean"].iloc[0]
    scaling = summary[["scenario", "node_count", "total_seconds_mean", "total_seconds_std"]].copy()
    scaling["speedup_mean"] = baseline / scaling["total_seconds_mean"]
    scaling["efficiency_mean"] = scaling["speedup_mean"] / scaling["node_count"]
    scaling["speedup_mean"] = scaling["speedup_mean"].round(3)
    scaling["efficiency_mean"] = scaling["efficiency_mean"].round(3)
    scaling.to_csv(table_dir / "strong_scaling_summary.csv", index=False)

    formatted = summary.copy()
    for metric in ["extract_seconds", "transform_seconds", "load_seconds", "total_seconds"]:
        formatted[f"{metric}_mean_std"] = formatted.apply(
            lambda row: f"{row[f'{metric}_mean']:.2f} ± {row[f'{metric}_std']:.2f}", axis=1
        )
    formatted[[
        "scenario", "node_count", "extract_seconds_mean_std", "transform_seconds_mean_std",
        "load_seconds_mean_std", "total_seconds_mean_std"
    ]].to_csv(table_dir / "etl_timing_summary_mean_std_for_paper.csv", index=False)


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    repeats = args.repeats or int(cfg["experiments"].get("repeats", 5))
    scenarios = cfg["experiments"]["scenarios"]

    results: list[Dict] = []
    for scenario_idx, scenario in enumerate(scenarios):
        if not args.skip_docker_control:
            set_active_workers(int(scenario.get("active_workers", 0)), ROOT)

        for rep in range(1, repeats + 1):
            run_id = f"r{rep:02d}"
            is_last_run = (scenario_idx == len(scenarios) - 1 and rep == repeats)
            output_suffix = "" if is_last_run else f"_{scenario['name'].replace('-', '')}_{run_id}"
            print(f"\n=== {scenario['name']} / repeat {rep}/{repeats} ===")
            result = run_one_etl(cfg, scenario, run_id, output_suffix, args.distributed_in_container)
            results.append(result)
            summarize_results(results, cfg)

    summarize_results(results, cfg)
    print("\n[OK] ETL experiment tables written to", cfg["paths"]["table_dir"])


if __name__ == "__main__":
    main()
