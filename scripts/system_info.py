#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psutil

from src.reporting import write_json


def run_optional(cmd: list[str]) -> str | None:
    try:
        cp = subprocess.run(cmd, text=True, capture_output=True, check=False)
        text = (cp.stdout or cp.stderr).strip()
        return text if text else None
    except Exception:
        return None


def collect_system_info() -> dict:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(str(Path.cwd()))
    info = {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": sys.version,
        },
        "cpu": {
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_cores": psutil.cpu_count(logical=True),
            "current_frequency_mhz": getattr(psutil.cpu_freq(), "current", None) if psutil.cpu_freq() else None,
        },
        "memory": {
            "total_gb": round(vm.total / (1024**3), 3),
        },
        "disk": {
            "filesystem_path": str(Path.cwd()),
            "total_gb": round(disk.total / (1024**3), 3),
            "used_gb": round(disk.used / (1024**3), 3),
            "free_gb": round(disk.free / (1024**3), 3),
        },
        "software": {
            "docker": run_optional(["docker", "--version"]),
            "docker_compose": run_optional(["docker", "compose", "version"]),
            "java": run_optional(["java", "-version"]),
            "spark_submit": run_optional(["spark-submit", "--version"]),
        },
    }

    # Windows-specific convenience fields.
    info["software"]["wsl"] = run_optional(["wsl", "--version"])
    return info


def main() -> None:
    p = argparse.ArgumentParser(description="Save hardware and software information for the manuscript")
    p.add_argument("--out", default="outputs/reports/system_info.json")
    args = p.parse_args()
    info = collect_system_info()
    write_json(info, args.out)
    print(json.dumps(info, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
