from __future__ import annotations

import csv
import threading
import time
from pathlib import Path
from typing import Optional

import psutil


class ResourceMonitor:
    """Lightweight host-level resource monitor.

    It samples CPU, RAM, and disk I/O while an ETL or ML process runs. This creates
    evidence for I/O-bound vs CPU-bound interpretation in the manuscript.
    """

    def __init__(self, output_csv: str | Path, interval_seconds: float = 1.0):
        self.output_csv = Path(output_csv)
        self.interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_disk = None
        self._last_time = None

    def start(self) -> None:
        self.output_csv.parent.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        with self.output_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "timestamp",
                    "cpu_percent",
                    "memory_percent",
                    "memory_used_gb",
                    "memory_total_gb",
                    "disk_read_mb_s",
                    "disk_write_mb_s",
                ],
            )
            writer.writeheader()
            psutil.cpu_percent(interval=None)
            self._last_disk = psutil.disk_io_counters()
            self._last_time = time.time()
            while not self._stop.is_set():
                now = time.time()
                mem = psutil.virtual_memory()
                disk = psutil.disk_io_counters()
                elapsed = max(now - (self._last_time or now), 1e-6)
                read_mb_s = (disk.read_bytes - self._last_disk.read_bytes) / elapsed / (1024**2)
                write_mb_s = (disk.write_bytes - self._last_disk.write_bytes) / elapsed / (1024**2)
                self._last_disk = disk
                self._last_time = now
                writer.writerow(
                    {
                        "timestamp": now,
                        "cpu_percent": psutil.cpu_percent(interval=None),
                        "memory_percent": mem.percent,
                        "memory_used_gb": round(mem.used / (1024**3), 3),
                        "memory_total_gb": round(mem.total / (1024**3), 3),
                        "disk_read_mb_s": round(read_mb_s, 3),
                        "disk_write_mb_s": round(write_mb_s, 3),
                    }
                )
                f.flush()
                time.sleep(self.interval_seconds)
