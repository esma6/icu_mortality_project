# Which supervisor note is handled by which code file?

| Supervisor note | Code/output that addresses it |
|---|---|
| Hardware information should be reported | `scripts/system_info.py` → `outputs/reports/system_info.json` |
| How many times was each experiment repeated? | `scripts/run_etl_experiments.py --repeats 5` |
| Report mean ± standard deviation | `outputs/tables/etl_timing_summary_mean_std_for_paper.csv` |
| Strong scaling vs weak scaling should be stated | `outputs/tables/strong_scaling_summary.csv` computes strong-scaling speedup and efficiency |
| Add CPU/resource graph as evidence | `src/monitoring.py` + `scripts/make_figures.py` → `figure_resource_cpu_memory_disk.png` |
| Add preprocessing: missing, outlier, normalization, feature selection | `src/preprocessing.py` and `scripts/train_models.py` |
| Add 5-fold stratified cross-validation | `scripts/train_models.py --cv-folds 5` → `cv_metrics_summary.csv` |
| Give ROC and PR as Figure 5(a) and 5(b) | `figure_5_roc_pr_curves.png` |
| Separate metric definitions from results | Metrics are computed centrally in `src/metrics.py`; the paper can move explanations to Methods |
| Feature engineering is intentionally compact | `scripts/spark_etl_mimic.py` uses a low-dimensional clinical feature set and missingness indicators |

