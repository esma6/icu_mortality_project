from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict


def default_config() -> Dict[str, Any]:
    return {
        "project": {"name": "icu_mortality_project_revised", "random_state": 42},
        "paths": {
            "mimic_dir": "./data/mimic-iii-clinical-database-1.4",
            "output_dir": "./outputs",
            "feature_dir": "./outputs/features",
            "table_dir": "./outputs/tables",
            "figure_dir": "./outputs/figures",
            "log_dir": "./outputs/logs",
            "report_dir": "./outputs/reports",
        },
        "spark": {
            "app_name": "MIMIC_III_Distributed_ETL",
            "master_1node": "local[*]",
            "master_2node": "spark://localhost:7077",
            "master_3node": "spark://localhost:7077",
            "executor_memory": "2g",
            "driver_memory": "2g",
            "executor_cores": "2",
            "shuffle_partitions": "8",
            "output_partitions": 1,
        },
        "etl": {
            "write_csv": True,
            "write_parquet": True,
            "feature_set": "compact",
            "apply_clinical_range_filter": True,
            "cap_age_at_90": True,
            "early_window_hours": 48,
            "early_window_min_los_hours": 48,
        },
        "experiments": {
            "repeats": 5,
            "scenarios": [
                {"name": "1-node", "node_count": 1, "master_key": "master_1node", "active_workers": 0},
                {"name": "2-node", "node_count": 2, "master_key": "master_2node", "active_workers": 1},
                {"name": "3-node", "node_count": 3, "master_key": "master_3node", "active_workers": 2},
            ],
        },
        "monitoring": {"enabled": True, "interval_seconds": 1.0},
        "ml": {
            "label_col": "mortality_label",
            "test_size": 0.20,
            "cv_folds": 5,
            "n_repeats": 30,
            "outlier_clip_quantiles": [0.01, 0.99],
            "use_feature_selection": False,
            "feature_selection_k": "all",
            "probability_threshold": 0.50,
        },
    }


def _load_yaml_config(config_path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        # The apache/spark Docker image may not include PyYAML. The project uses a stable
        # default config so distributed container runs can still execute.
        return default_config()
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(config_path: str | Path) -> Dict[str, Any]:
    config_path = Path(config_path)
    cfg = _load_yaml_config(config_path) if config_path.exists() else default_config()

    mimic_env = os.environ.get("MIMIC_DIR")
    if mimic_env:
        cfg["paths"]["mimic_dir"] = mimic_env

    feature_set_override = os.environ.get("ETL_FEATURE_SET_OVERRIDE")
    if feature_set_override:
        if feature_set_override not in {"compact", "wide", "timeseries", "early_window"}:
            raise ValueError(f"Unsupported ETL_FEATURE_SET_OVERRIDE={feature_set_override!r}")
        cfg.setdefault("etl", {})["feature_set"] = feature_set_override

    spark_overrides = {
        "SPARK_EXECUTOR_CORES_OVERRIDE": "executor_cores",
        "SPARK_EXECUTOR_MEMORY_OVERRIDE": "executor_memory",
        "SPARK_DRIVER_MEMORY_OVERRIDE": "driver_memory",
    }
    for env_key, cfg_key in spark_overrides.items():
        value = os.environ.get(env_key)
        if value:
            cfg.setdefault("spark", {})[cfg_key] = value

    output_root = os.environ.get("ETL_OUTPUT_ROOT")
    if output_root:
        root = Path(output_root)
        cfg["paths"].update({
            "output_dir": str(root),
            "feature_dir": str(root / "features"),
            "table_dir": str(root / "tables"),
            "figure_dir": str(root / "figures"),
            "log_dir": str(root / "logs"),
            "report_dir": str(root / "reports"),
        })

    for key in ["output_dir", "feature_dir", "table_dir", "figure_dir", "log_dir", "report_dir"]:
        p = Path(cfg["paths"][key])
        p.mkdir(parents=True, exist_ok=True)
        cfg["paths"][key] = str(p)

    return cfg


def require_mimic_files(mimic_dir: str | Path) -> None:
    mimic_dir = Path(mimic_dir)
    required = [
        "ADMISSIONS.csv.gz",
        "PATIENTS.csv.gz",
        "ICUSTAYS.csv.gz",
        "CHARTEVENTS.csv.gz",
        "LABEVENTS.csv.gz",
    ]
    missing = [name for name in required if not (mimic_dir / name).exists()]
    if missing:
        raise FileNotFoundError(
            "Missing MIMIC-III files in {}: {}".format(mimic_dir, ", ".join(missing))
        )
