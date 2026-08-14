from __future__ import annotations

import argparse
import time
from pathlib import Path

import psutil


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--stop-file", required=True)
    p.add_argument("--interval", type=float, default=1.0)
    args = p.parse_args()
    output = Path(args.output)
    stop_file = Path(args.stop_file)
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with output.open("w", encoding="utf-8", newline="") as f:
        f.write("timestamp,elapsed_seconds,cpu_percent,memory_percent,memory_used_gb,memory_available_gb\n")
        while not stop_file.exists():
            try:
                mem = psutil.virtual_memory()
                f.write(f"{time.time():.6f},{time.time()-started:.3f},{psutil.cpu_percent(interval=None):.3f},"
                        f"{mem.percent:.3f},{mem.used/1024**3:.6f},{mem.available/1024**3:.6f}\n")
                f.flush()
            except OSError as exc:
                f.write(f"{time.time():.6f},{time.time()-started:.3f},,,,\n")
                f.flush()
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
