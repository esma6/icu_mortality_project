#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.monitoring import ResourceMonitor

OUT = ROOT / "outputs" / "validation"
LOG = OUT / "logs"
EVENTS = OUT / "spark-events"
MANIFEST = OUT / "validation_manifest.csv"
SCHEDULE = OUT / "randomization_schedule.csv"

SCENARIOS = {
    "local2": {"master": "local[2]", "workers": 0, "master_cpu": 8, "master_mem": "8g"},
    "local4": {"master": "local[4]", "workers": 0, "master_cpu": 8, "master_mem": "8g"},
    "local8": {"master": "local[8]", "workers": 0, "master_cpu": 8, "master_mem": "8g"},
    "standalone1": {"master": "spark://spark-master:7077", "workers": 1,
                    "master_cpu": 2, "master_mem": "3g", "worker_cpu": 6,
                    "worker_mem": "5g", "advertised_cores": 6, "advertised_mem": "4g"},
    "standalone2": {"master": "spark://spark-master:7077", "workers": 2,
                    "master_cpu": 2, "master_mem": "3g", "worker_cpu": 3,
                    "worker_mem": "2560m", "advertised_cores": 3, "advertised_mem": "2g"},
}
EXPECTED_ROWS = {"compact": 58976, "timeseries": 1180395}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Randomized, resource-controlled Spark ETL validation")
    p.add_argument("--repeats", type=int, default=12)
    p.add_argument("--seed", type=int, default=20260720)
    p.add_argument("--pilot", action="store_true", help="Run one randomized measured block only")
    p.add_argument("--skip-warmup", action="store_true")
    return p.parse_args()


def run(cmd: list[str], *, env: dict | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    # On Windows, docker compose descendants can keep anonymous pipe handles open
    # after the CLI exits. File-backed capture avoids that deadlock and also handles
    # long spark-submit logs without filling a pipe buffer.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout_file, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr_file:
        raw = subprocess.run(cmd, cwd=ROOT, env=env, text=True, stdout=stdout_file,
                             stderr=stderr_file, timeout=timeout or 120, check=False)
        stdout_file.seek(0)
        stderr_file.seek(0)
        cp = subprocess.CompletedProcess(cmd, raw.returncode, stdout_file.read(), stderr_file.read())
    if cp.returncode:
        raise RuntimeError(f"Command failed ({cp.returncode}): {' '.join(cmd)}\n{cp.stdout}\n{cp.stderr}")
    return cp


def compose(args: list[str], env: dict | None = None) -> None:
    run(["docker", "compose", *args], env=env)


def inspect_limit(container: str) -> dict:
    cp = run(["docker", "inspect", container, "--format", "{{json .HostConfig}}"])
    host = json.loads(cp.stdout)
    return {"nano_cpus": int(host.get("NanoCpus") or 0), "memory_bytes": int(host.get("Memory") or 0)}


def master_state() -> dict:
    cp = run(["docker", "compose", "exec", "-T", "spark-master", "sh", "-lc",
              "wget -T 5 -qO- http://localhost:8080/json/"])
    return json.loads(cp.stdout)


def wait_for_master(expected_workers: int, timeout_seconds: int = 90) -> dict:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            state = master_state()
            if int(state.get("aliveworkers", 0)) == expected_workers:
                return state
        except Exception as exc:
            last_error = exc
        time.sleep(3)
    raise RuntimeError(f"Spark Master did not reach {expected_workers} workers: {last_error!r}")


def configure_topology(name: str) -> dict:
    s = SCENARIOS[name]
    print(f"[TOPOLOGY] configuring {name}", flush=True)
    env = os.environ.copy()
    if s["workers"]:
        env["SPARK_WORKER_CORES"] = str(s["advertised_cores"])
        env["SPARK_WORKER_MEMORY"] = str(s["advertised_mem"])

    # Apply limits while containers are stopped. Live memory-limit changes can be
    # rejected because of stale memory-swap settings and are therefore not used.
    print("[TOPOLOGY] stopping containers", flush=True)
    compose(["stop", "spark-worker-1", "spark-worker-2", "spark-master"], env)
    print("[TOPOLOGY] creating/updating master", flush=True)
    compose(["create", "spark-master"], env)
    run(["docker", "update", "--cpus", str(s["master_cpu"]), "--memory", s["master_mem"],
         "--memory-swap", s["master_mem"], "spark-master"])
    compose(["start", "spark-master"], env)

    services = ["spark-worker-1"] + (["spark-worker-2"] if s["workers"] == 2 else [])
    if services and s["workers"]:
        print(f"[TOPOLOGY] creating workers: {services}", flush=True)
        compose(["create", "--force-recreate", *services], env)
    for idx in range(1, s["workers"] + 1):
        run(["docker", "update", "--cpus", str(s["worker_cpu"]), "--memory", s["worker_mem"],
             "--memory-swap", s["worker_mem"], f"spark-worker-{idx}"])
    if s["workers"]:
        compose(["start", *services], env)
    print("[TOPOLOGY] waiting for Spark Master", flush=True)
    state = wait_for_master(s["workers"])
    if state.get("activeapps"):
        raise RuntimeError(f"Spark cluster is not clean: {state['activeapps']}")
    if int(state.get("aliveworkers", 0)) != s["workers"]:
        raise RuntimeError(f"Expected {s['workers']} workers, got {state.get('aliveworkers')}")
    if s["workers"] and sum(int(w["cores"]) for w in state["workers"]) != 6:
        raise RuntimeError(f"Expected six advertised executor cores: {state['workers']}")

    limits = {"master": inspect_limit("spark-master")}
    for idx in range(1, s["workers"] + 1):
        limits[f"worker{idx}"] = inspect_limit(f"spark-worker-{idx}")
    total_cpu = sum(x["nano_cpus"] for x in limits.values()) / 1e9
    total_mem = sum(x["memory_bytes"] for x in limits.values()) / 1024**3
    if abs(total_cpu - 8) > 0.01 or abs(total_mem - 8) > 0.05:
        raise RuntimeError(f"Resource budget mismatch: cpu={total_cpu}, memory={total_mem}, {limits}")
    print(f"[TOPOLOGY] ready {name}: cpu={total_cpu}, memory={total_mem:.3f} GiB", flush=True)
    return {"limits": limits, "spark_workers": state.get("workers", [])}


def build_schedule(repeats: int, seed: int, pilot: bool, skip_warmup: bool) -> pd.DataFrame:
    blocks = [1] if pilot else list(range(1, repeats + 1))
    phases = [] if skip_warmup else [("warmup", 0)]
    phases += [("measured", b) for b in blocks]
    rng = random.Random(seed)
    rows = []
    order = 0
    cells = [(w, s) for w in ("compact", "timeseries") for s in SCENARIOS]
    for phase, block in phases:
        shuffled = cells.copy()
        rng.shuffle(shuffled)
        for within, (workload, scenario) in enumerate(shuffled, 1):
            order += 1
            rows.append({"global_order": order, "phase": phase, "block": block,
                         "within_block_order": within, "workload": workload, "scenario": scenario,
                         "seed": seed})
    return pd.DataFrame(rows)


def load_existing() -> pd.DataFrame:
    return pd.read_csv(MANIFEST) if MANIFEST.exists() else pd.DataFrame()


def persist(rows: list[dict]) -> None:
    pd.DataFrame(rows).to_csv(MANIFEST, index=False)


def run_cell(cell: dict) -> dict:
    workload, scenario = cell["workload"], cell["scenario"]
    phase, block = cell["phase"], int(cell["block"])
    run_id = f"v2_{phase[0]}{block:02d}_{workload}_{scenario}"
    cluster = configure_topology(scenario)
    event_before = {p.name for p in EVENTS.glob("*")}

    env = os.environ.copy()
    env.update({
        "MIMIC_DIR": "/data/mimic",
        "PYTHONPATH": "/app",
        "SPARK_DRIVER_HOST": "spark-master",
        "ETL_FEATURE_SET_OVERRIDE": workload,
        "ETL_OUTPUT_ROOT": "/app/outputs/validation",
        "SPARK_EXECUTOR_CORES_OVERRIDE": "1",
        "SPARK_EXECUTOR_MEMORY_OVERRIDE": "512m",
        "SPARK_DRIVER_MEMORY_OVERRIDE": "2g",
    })
    monitor_path = LOG / f"resource_usage_{run_id}.csv"
    monitor = ResourceMonitor(monitor_path, 1.0)
    cmd = [
        "docker", "compose", "exec", "-T", "-w", "/app",
        "-e", "MIMIC_DIR=/data/mimic", "-e", "PYTHONPATH=/app",
        "-e", "SPARK_DRIVER_HOST=spark-master",
        "-e", f"ETL_FEATURE_SET_OVERRIDE={workload}",
        "-e", "ETL_OUTPUT_ROOT=/app/outputs/validation",
        "-e", "SPARK_EXECUTOR_CORES_OVERRIDE=1",
        "-e", "SPARK_EXECUTOR_MEMORY_OVERRIDE=512m",
        "-e", "SPARK_DRIVER_MEMORY_OVERRIDE=2g",
        "spark-master", "/opt/spark/bin/spark-submit",
        "--master", SCENARIOS[scenario]["master"],
        "--driver-memory", "2g",
        "--executor-memory", "512m", "--executor-cores", "1",
        "--conf", "spark.driver.host=spark-master",
        "--conf", "spark.driver.bindAddress=0.0.0.0",
        "--conf", "spark.eventLog.enabled=true",
        "--conf", "spark.eventLog.compress=false",
        "--conf", "spark.eventLog.dir=file:///app/outputs/validation/spark-events",
        "--conf", "spark.cores.max=6",
        "/app/scripts/spark_etl_mimic.py", "--config", "/app/config.yaml",
        "--master", SCENARIOS[scenario]["master"], "--scenario", scenario,
        "--run-id", run_id, "--output-suffix", f"_validation_{workload}_{scenario}",
    ]
    log_path = LOG / f"spark_submit_{run_id}.log"
    monitor.start()
    started = time.time()
    try:
        cp = run(cmd, env=env, timeout=1800)
        log_path.write_text((cp.stdout or "") + "\n" + (cp.stderr or ""), encoding="utf-8")
    finally:
        monitor.stop()
    timing_path = LOG / f"etl_timing_{scenario}_{run_id}.json"
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    event_after = [p for p in EVENTS.glob("*") if p.name not in event_before]
    if not event_after:
        raise RuntimeError(f"No Spark event log produced for {run_id}")
    event_path = max(event_after, key=lambda p: p.stat().st_mtime)
    if timing.get("input_format") != "parquet" or int(timing.get("feature_rows", -1)) != EXPECTED_ROWS[workload]:
        raise RuntimeError(f"Output validation failed for {run_id}: {timing}")
    return {**cell, "run_id": run_id, "status": "ok", "wall_started": started,
            **{k: timing.get(k) for k in ("input_format", "extract_seconds", "transform_seconds",
                                          "load_seconds", "total_seconds", "feature_rows")},
            "resource_log": str(monitor_path), "event_log": str(event_path),
            "submit_log": str(log_path), "resource_limits_json": json.dumps(cluster, ensure_ascii=False)}


def main() -> None:
    args = parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    LOG.mkdir(parents=True, exist_ok=True)
    EVENTS.mkdir(parents=True, exist_ok=True)
    schedule = build_schedule(args.repeats, args.seed, args.pilot, args.skip_warmup)
    if SCHEDULE.exists():
        existing_schedule = pd.read_csv(SCHEDULE)
        if not existing_schedule.equals(schedule):
            raise RuntimeError("Existing randomization schedule differs; use a clean validation output directory")
    else:
        schedule.to_csv(SCHEDULE, index=False)

    prior = load_existing()
    rows = prior.to_dict("records") if not prior.empty else []
    done = {str(r.get("run_id")) for r in rows if r.get("status") == "ok"}
    for cell in schedule.to_dict("records"):
        run_id = f"v2_{cell['phase'][0]}{int(cell['block']):02d}_{cell['workload']}_{cell['scenario']}"
        if run_id in done:
            continue
        print(f"[RUN] {cell}", flush=True)
        try:
            row = run_cell(cell)
        except Exception as exc:
            row = {**cell, "run_id": run_id, "status": "failed", "error": repr(exc)}
            rows.append(row)
            persist(rows)
            raise
        rows.append(row)
        persist(rows)
        print(f"[OK] {run_id}: {row['total_seconds']:.3f}s", flush=True)
    print(MANIFEST)


if __name__ == "__main__":
    main()
