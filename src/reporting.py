from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def write_json(obj: Dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def read_json(path: str | Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def summarize_mean_std(
    df: "pd.DataFrame",
    group_cols: Iterable[str],
    value_cols: Iterable[str],
    digits: int = 2,
) -> "pd.DataFrame":
    # Lazy import: Docker Spark containers used for ETL do not need pandas.
    # Host-side reporting/training still imports pandas only when this function is called.
    import pandas as pd

    group_cols = list(group_cols)
    value_cols = list(value_cols)
    agg = df.groupby(group_cols)[value_cols].agg(["mean", "std", "min", "max"]).reset_index()
    agg.columns = ["_".join([c for c in tup if c]) for tup in agg.columns.to_flat_index()]
    for c in agg.columns:
        if c not in group_cols and pd.api.types.is_numeric_dtype(agg[c]):
            agg[c] = agg[c].round(digits)
    return agg


def ensure_dirs(*paths: str | Path) -> None:
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)
