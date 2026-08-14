import json
from pathlib import Path

from pyspark.sql import SparkSession, functions as F

s = SparkSession.builder.master("local[2]").appName("paper-audit").getOrCreate()
d = s.read.parquet("/app/outputs/features/feature_matrix.parquet")
r = d.agg(
    F.count("*").alias("rows"),
    F.countDistinct("hadm_id").alias("hadm"),
    F.min("window_idx").alias("window_min"),
    F.max("window_idx").alias("window_max"),
    F.countDistinct("window_idx").alias("distinct_window_indices"),
).collect()[0]
print("PAPER_AUDIT", r.asDict(), flush=True)
print("PAPER_AUDIT_COLUMNS", len(d.columns), d.columns, flush=True)
Path("/app/paper_revision/generated/timeseries_output_audit.json").write_text(
    json.dumps({**r.asDict(), "column_count": len(d.columns), "columns": d.columns}, indent=2),
    encoding="utf-8",
)
s.stop()
