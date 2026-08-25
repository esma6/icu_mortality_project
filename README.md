# Resource-Constrained Single-Host Clinical Spark ETL on MIMIC-III

Reproducible code for a study that (1) characterizes Apache Spark execution topology
(`local[K]` thread parallelism vs. co-located standalone workers) under a **fixed physical
resource budget on a single host**, across two MIMIC-III feature-engineering regimes, and
(2) validates a separate, **leak-free 48-hour early-window feature set** derived from the
same pipeline on an in-hospital mortality prediction task with patient-grouped
cross-validation.

Manuscript draft (target: IEEE Journal of Biomedical and Health Informatics):
`paper_revision/ieee_jbhi/IEEE_JBHI_Submission_Manuscript.docx`, built from this
repository's aggregate outputs by `paper_revision/ieee_jbhi/build_ieee_jbhi_manuscript.py`.

> **MIMIC-III data are not included and must not be committed here.** You need
> credentialed PhysioNet access to `mimic-iii-clinical-database-1.4`. See
> [Data access & sharing policy](#data-access--sharing-policy) below.

---

## 1. What this repository contains

| Path | What it is |
|---|---|
| `scripts/spark_etl_mimic.py` | Core Spark ETL job. Four feature-engineering modes selected by `etl.feature_set` in `config.yaml` (or `ETL_FEATURE_SET_OVERRIDE` env var): `compact`, `wide`, `timeseries`, `early_window`. |
| `scripts/run_etl_experiments.py` | Runs the ETL job repeatedly across execution-topology scenarios (`local[2]`, `local[4]`, `local[8]`, 1-worker standalone, 2-worker standalone) and writes timing tables. |
| `scripts/run_validation_pilot.ps1` / `run_validation_matrix.ps1` / `make_validation_schedule.py` | The confirmatory, randomized, resource-quota-controlled validation series (see below): fixes the resource budget with `docker update`, verifies it with `docker inspect`, enables Spark event logging, and runs a randomized complete-block design. |
| `scripts/train_models.py` | Trains classifiers on the whole-stay `compact`/`timeseries` feature products (used for the ETL benchmark, **not** leak-free — see [Two feature families](#two-feature-families-etl-benchmark-vs-clinical-validation)). |
| `scripts/train_leakfree_model.py` | Trains classifiers on the leak-free `early_window` feature product with `subject_id`-grouped `StratifiedGroupKFold`, plus calibration metrics. This produces the numbers reported in the manuscript's clinical validation section. |
| `src/` | Shared library code: config loading (`config.py`), preprocessing pipeline (`preprocessing.py`), classification + calibration metrics (`metrics.py`), plotting (`plotting.py`), summary tables (`reporting.py`), resource monitoring (`monitoring.py`). |
| `paper_revision/` | The manuscript build script/draft (`ieee_jbhi/`) and the analysis scripts that turn `outputs/` into the manuscript's tables/text (`analyze_validation_v2.py`, `analyze_local6_vs_standalone2.py`). |
| `outputs/` | Generated tables, figures, and logs (aggregate only — see below). |
| `docker-compose.yml` | One Spark master + up to two Spark workers (`apache/spark:3.5.1`), used both for the ETL benchmark topologies and for `spark://` runs of the leak-free ETL job. |

### Two feature families: ETL benchmark vs. clinical validation

This project deliberately keeps two feature products separate, because they answer
different questions and one of them is **not** safe to use for clinical prediction:

- **`compact` / `timeseries`** (and `wide`): whole-admission and 6-hour-window feature
  matrices used purely to benchmark ETL execution time across Spark topologies. Their lab
  and ICU-length-of-stay aggregates are computed over the *entire* admission with no
  prediction-time cutoff, so they are not appropriate for an early-prediction clinical
  model (see the manuscript's Section III.A/VII for the full discussion). `scripts/train_models.py`
  trains on these purely to demonstrate the pipeline's output is ML-consumable, not as a
  clinical claim.
- **`early_window`**: a separate feature product limited to measurements in the first 48
  hours after each admission's *first* ICU stay (ICU stays shorter than 48h are excluded,
  following the Harutyunyan et al. MIMIC benchmark convention), with no ICU-discharge-adjacent
  feature (no length-of-stay, no ICU-stay-count). Lab results are restricted to a count and
  an abnormal-flag count — not mean/min/max/SD, which would silently average chemically
  unrelated LABEVENTS itemids (sodium, creatinine, pH, ...) without a `D_LABITEMS` join.
  `scripts/train_leakfree_model.py` trains on this with `subject_id`-grouped
  `StratifiedGroupKFold` splits (a patient's admissions never span train/test), recalibrates
  each model's class-weighted probabilities within each training fold (sigmoid/Platt scaling),
  and reports AUROC/AUPRC/Brier/Brier-skill-score/calibration. This is the leakage-aware
  cohort behind the manuscript's clinical validation numbers (Section III.D/IV.B).

---

## 2. Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Place credentialed MIMIC-III files (or a Parquet conversion — see
`scripts/convert_mimic_to_parquet.py`) locally and point `config.yaml` /
`MIMIC_DIR` at them:

```bash
export MIMIC_DIR="/path/to/mimic-iii-clinical-database-1.4"
```

Required source tables: `ADMISSIONS`, `PATIENTS`, `ICUSTAYS`, `CHARTEVENTS`, `LABEVENTS`
(`.csv.gz` or Parquet, per `etl.input_format` in `config.yaml`).

Start the Spark cluster used by the ETL/validation scripts:

```bash
docker compose up -d spark-master spark-worker-1 spark-worker-2
```

> **`.pylibs/` (vendored PyYAML) is intentional, not an accident.** The
> `apache/spark:3.5.1` image's system Python has no PyYAML and no network access inside
> the container, so `config.yaml` would otherwise be silently ignored and the ETL job
> would silently fall back to defaults. `src/config.py` adds `.pylibs/` (mounted into the
> container at `/app/.pylibs`) to `sys.path` before importing `yaml`. If you change the
> Spark base image, re-verify this still works.

---

## 3. Reproducing the results

### 3.1. ETL execution-topology benchmark (primary series, n=5)

```bash
python scripts/system_info.py --out outputs/reports/system_info.json
python scripts/run_etl_experiments.py --config config.yaml --repeats 5
python scripts/make_figures.py --config config.yaml
```

Set `etl.feature_set` in `config.yaml` to `compact` or `timeseries` and re-run for each
workload. Produces `outputs/tables/etl_timing_*.csv`, `outputs/tables/strong_scaling_summary.csv`,
and the corresponding figures.

### 3.2. Randomized, resource-quota-controlled confirmatory series (n=12)

```powershell
scripts\run_validation_matrix.ps1 -Repeats 12 -Seed 20260720
python paper_revision/analyze_validation_v2.py
```

This is a randomized complete-block design over the same 5 topologies x 2 workloads,
with `docker update`-enforced (and `docker inspect`-verified) 8-CPU/8-GiB budgets per
cell and Spark event-log telemetry enabled, addressing the fixed-order and
unverified-container-quota limitations of the primary series. Outputs land in
`outputs/validation/analysis/`.

To additionally control for local[8]'s 8 task threads vs. standalone's 6 declared
executor cores, an equal-capacity `local[6]` arm is compared against the two-worker
standalone arrangement in the same session (both interleaved within each block so
host-load drift affects them equally):

```powershell
scripts\run_local6_standalone2_matrix.ps1 -Repeats 12 -Seed 20260817
python paper_revision/analyze_local6_vs_standalone2.py
```

### 3.3. Leak-free clinical validation

```bash
ETL_FEATURE_SET_OVERRIDE=early_window python scripts/spark_etl_mimic.py \
    --config config.yaml --master local[8] --scenario early_window --run-id r01 \
    --output-suffix _early_window

python scripts/train_leakfree_model.py --config config.yaml
```

Produces `outputs/tables_ml_leakfree/*.csv` (cohort flow, split diagnostics, holdout and
cross-validated AUROC/AUPRC/Brier/calibration) and `outputs/figures_ml_leakfree/*.png`
(ROC/PR, calibration curve, feature importance).

### 3.4. One-command run (ETL benchmark + whole-stay ML demo)

```bash
python scripts/run_full_pipeline.py --config config.yaml --repeats 5 --cv-folds 5
```

---

## 4. Headline results

- **local[8] had the lowest mean ETL runtime** for both the admission-level (212.5±11.1 s)
  and six-hour-window (234.8±7.3 s) workloads in the randomized, resource-quota-controlled
  series (n=12); all eight prespecified contrasts against local[8] were significant after
  Holm adjustment.
- **Equal-capacity control**: because standalone declared 6 executor cores against
  local[8]'s 8 task threads, a same-session, block-randomized `local[6]` (task-slot-matched)
  vs. two-worker-standalone comparison (n=12 paired blocks) still favored local execution
  for both workloads, indicating the advantage is not solely a task-slot-count artifact.
- **Leakage-aware 48h cohort** (29,886 admissions with a real single ICU stay >= 48h,
  25,271 patients, 12.4% mortality — landmarked on each admission's *first* `icustay_id`,
  not a multi-stay envelope): HistGradientBoosting reached AUROC 0.815±0.005 (5-fold grouped
  CV) / 0.815±0.005 (repeated holdout), Brier skill score 0.169, and a
  within-training-fold-recalibrated calibration intercept/slope of 0.09/1.07 (vs. a large
  negative intercept before recalibration).

These are **fixed-budget, single-host, single-center** findings (see the manuscript's
Section VII for the full validity-threat discussion) — not evidence of multi-host strong
scaling, and not an externally validated clinical model.

---

## 5. Data access & sharing policy

- **MIMIC-III** is controlled-access, de-identified data distributed under a PhysioNet
  Data Use Agreement that prohibits redistribution. This repository contains **no** raw
  or patient-level derived MIMIC-III data (`.gitignore` excludes `data/`,
  `outputs/features/`, and `outputs/validation/features/`, which hold the
  admission/window-level feature matrices).
- What *is* included: source code, configuration, aggregate/summary tables (timing
  statistics, cross-validation summaries, feature-importance rankings — no per-patient
  rows), figures, and execution/resource logs.
- To reproduce the row-level feature matrices and the reported numbers, obtain your own
  credentialed MIMIC-III access and run the scripts in [Section 3](#3-reproducing-the-results).

---

## 6. Repository layout

```text
.
├── config.yaml
├── docker-compose.yml
├── requirements.txt
├── .pylibs/              # vendored PyYAML (see Setup notes above)
├── src/                  # shared library code
├── scripts/               # ETL, experiment orchestration, ML training, figures
├── outputs/                # aggregate tables/figures/logs (see .gitignore for exclusions)
└── paper_revision/         # manuscript drafts + analysis/build scripts
```
