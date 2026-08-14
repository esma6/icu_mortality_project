#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# src.config.load_config sessizce default_config()'e düşmesin diye PyYAML'ın
# import edilebilir olmasını garanti ediyoruz. Apache Spark imajının sistem
# python3'ünde PyYAML yok ve spark-submit'e verilen PYTHONPATH (ör. sadece /app)
# yalnızca proje kökünü içerdiğinden config.yaml OKUNMAZ; bu da feature_set gibi
# ETL anahtarlarının sessizce yok sayılmasına yol açar. Repo köküne (host mount)
# vendor edilmiş PyYAML'ı sys.path'e ekleyerek her koşumun (tek koşu ve deney
# matrisi dahil) config.yaml'ı gerçekten okumasını sağlıyoruz.
for _p in (ROOT / ".pylibs", Path("/tmp/pylibs")):
    if _p.is_dir():
        sys.path.insert(0, str(_p))

from pyspark import StorageLevel
from pyspark.sql import DataFrame, SparkSession, Window, functions as F, types as T

from src.config import load_config, require_mimic_files
from src.reporting import write_json


# Common MIMIC-III item IDs for vital signs. This compact set keeps the feature
# space deliberately small for scalable ETL while preserving clinically useful signals.
VITAL_ITEMIDS = {
    "heart_rate": [211, 220045],
    "map": [456, 52, 6702, 443, 220052, 220181, 225312],
    "resp_rate": [618, 615, 220210, 224690],
    "spo2": [646, 220277],
}
TEMP_C_ITEMIDS = [676, 223762]
TEMP_F_ITEMIDS = [678, 223761]

CLINICAL_RANGES = {
    "heart_rate": (20.0, 250.0),
    "map": (20.0, 200.0),
    "resp_rate": (4.0, 80.0),
    "spo2": (50.0, 100.0),
    "temperature_c": (25.0, 45.0),
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Distributed ETL for MIMIC-III ICU mortality features")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--master", default=None, help="Spark master URL, e.g., local[*] or spark://localhost:7077")
    p.add_argument("--scenario", default="manual")
    p.add_argument("--run-id", default="run_001")
    p.add_argument("--output-suffix", default="", help="Optional suffix for output folders")
    p.add_argument("--no-cache-raw", action="store_true", help="Disable DISK_ONLY cache before stage timing")
    return p.parse_args()


def schema(columns: list[str]) -> T.StructType:
    return T.StructType([T.StructField(c, T.StringType(), True) for c in columns])


SCHEMAS = {
    "ADMISSIONS": schema([
        "ROW_ID", "SUBJECT_ID", "HADM_ID", "ADMITTIME", "DISCHTIME", "DEATHTIME",
        "ADMISSION_TYPE", "ADMISSION_LOCATION", "DISCHARGE_LOCATION", "INSURANCE",
        "LANGUAGE", "RELIGION", "MARITAL_STATUS", "ETHNICITY", "EDREGTIME", "EDOUTTIME",
        "DIAGNOSIS", "HOSPITAL_EXPIRE_FLAG", "HAS_CHARTEVENTS_DATA"
    ]),
    "PATIENTS": schema([
        "ROW_ID", "SUBJECT_ID", "GENDER", "DOB", "DOD", "DOD_HOSP", "DOD_SSN", "EXPIRE_FLAG"
    ]),
    "ICUSTAYS": schema([
        "ROW_ID", "SUBJECT_ID", "HADM_ID", "ICUSTAY_ID", "DBSOURCE", "FIRST_CAREUNIT",
        "LAST_CAREUNIT", "FIRST_WARDID", "LAST_WARDID", "INTIME", "OUTTIME", "LOS"
    ]),
    "CHARTEVENTS": schema([
        "ROW_ID", "SUBJECT_ID", "HADM_ID", "ICUSTAY_ID", "ITEMID", "CHARTTIME", "STORETIME",
        "CGID", "VALUE", "VALUENUM", "VALUEUOM", "WARNING", "ERROR", "RESULTSTATUS", "STOPPED"
    ]),
    "LABEVENTS": schema([
        "ROW_ID", "SUBJECT_ID", "HADM_ID", "ITEMID", "CHARTTIME", "VALUE", "VALUENUM", "VALUEUOM", "FLAG"
    ]),
}


def create_spark(cfg: Dict, master: str | None) -> SparkSession:
    spark_cfg = cfg["spark"]
    builder = (
        SparkSession.builder
        .appName(spark_cfg.get("app_name", "MIMIC_ETL"))
        .config("spark.executor.memory", spark_cfg.get("executor_memory", "2g"))
        .config("spark.driver.memory", spark_cfg.get("driver_memory", "2g"))
        .config("spark.executor.cores", spark_cfg.get("executor_cores", "2"))
        .config("spark.sql.shuffle.partitions", spark_cfg.get("shuffle_partitions", "8"))
        .config("spark.sql.adaptive.enabled", "true")
        .config("spark.sql.session.timeZone", "UTC")
    )
    if master and not master.startswith("local"):
        # Useful when the PySpark driver runs on the host and executors run in Docker.
        # Override with SPARK_DRIVER_HOST when needed.
        builder = (
            builder
            .config("spark.driver.bindAddress", "0.0.0.0")
            .config("spark.driver.host", os.environ.get("SPARK_DRIVER_HOST", "host.docker.internal"))
        )
    if master:
        builder = builder.master(master)
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    return spark


def read_table(spark: SparkSession, mimic_dir: str | Path, name: str,
               input_format: str = "csv", parquet_dir: str | Path | None = None) -> DataFrame:
    """
    MIMIC tablosunu okur. input_format='csv' ise şemayla .csv.gz okur (varsayılan,
    eski davranış). input_format='parquet' ise, convert_mimic_to_parquet.py ile
    üretilmiş, kolonları zaten küçük harfe çevrilmiş Parquet'i okur.

    Parquet, csv.gz'nin bölünememe (non-splittable) darboğazını ortadan kaldırıp
    Spark'ın paralel okumasına izin verdiği için ölçeklenebilirlik testinde
    kontrollü bir karşılaştırma sağlar.
    """
    if input_format == "parquet":
        base = Path(parquet_dir) if parquet_dir is not None else Path(mimic_dir)
        path = str(base / name)
        # Parquet çıktısı zaten küçük harfli kolonlarla yazıldı; tekrar alias'a gerek yok.
        return spark.read.parquet(path)

    path = str(Path(mimic_dir) / f"{name}.csv.gz")
    df = (
        spark.read
        .option("header", "true")
        .option("mode", "PERMISSIVE")
        .schema(SCHEMAS[name])
        .csv(path)
    )
    return df.select([F.col(c).alias(c.lower()) for c in df.columns])


def try_double(col_name: str):
    return F.expr(f"try_cast({col_name} as double)")


def load_raw_tables(spark: SparkSession, mimic_dir: str | Path,
                    input_format: str = "csv", parquet_dir: str | Path | None = None) -> Dict[str, DataFrame]:
    # input_format ('csv' | 'parquet') ve parquet_dir'i her okumaya taşıyan yardımcı.
    def rt(name: str) -> DataFrame:
        return read_table(spark, mimic_dir, name, input_format=input_format, parquet_dir=parquet_dir)

    admissions = rt("ADMISSIONS").select(
        F.col("subject_id").cast("long"),
        F.col("hadm_id").cast("long"),
        F.to_timestamp("admittime").alias("admittime"),
        F.to_timestamp("deathtime").alias("deathtime"),
        F.col("hospital_expire_flag").cast("int").alias("hospital_expire_flag"),
    )
    patients = rt("PATIENTS").select(
        F.col("subject_id").cast("long"),
        F.col("gender"),
        F.to_timestamp("dob").alias("dob"),
    )
    icustays = rt("ICUSTAYS").select(
        F.col("subject_id").cast("long"),
        F.col("hadm_id").cast("long"),
        F.col("icustay_id").cast("long"),
        F.to_timestamp("intime").alias("intime"),
        F.to_timestamp("outtime").alias("outtime"),
        try_double("los").alias("icu_los_days"),
    )
    chartevents = rt("CHARTEVENTS").select(
        F.col("subject_id").cast("long"),
        F.col("hadm_id").cast("long"),
        F.col("itemid").cast("int"),
        F.to_timestamp("charttime").alias("charttime"),
        try_double("valuenum").alias("valuenum"),
    ).where(F.col("hadm_id").isNotNull())
    labevents = rt("LABEVENTS").select(
        F.col("subject_id").cast("long"),
        F.col("hadm_id").cast("long"),
        F.col("itemid").cast("int"),
        F.to_timestamp("charttime").alias("charttime"),
        try_double("valuenum").alias("valuenum"),
        F.col("flag"),
    ).where(F.col("hadm_id").isNotNull())

    return {
        "admissions": admissions,
        "patients": patients,
        "icustays": icustays,
        "chartevents": chartevents,
        "labevents": labevents,
    }


def cache_and_count_raw(tables: Dict[str, DataFrame]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for name, df in tables.items():
        tables[name] = df.persist(StorageLevel.DISK_ONLY)
        counts[name] = tables[name].count()
    return counts


def build_demographics(admissions: DataFrame, patients: DataFrame, cap_age_at_90: bool = True) -> DataFrame:
    base = admissions.join(patients, on="subject_id", how="left")

    if "hospital_expire_flag" in base.columns:
        label = F.coalesce(F.col("hospital_expire_flag"), F.when(F.col("deathtime").isNotNull(), 1).otherwise(0))
    else:
        label = F.when(F.col("deathtime").isNotNull(), 1).otherwise(0)

    age = F.months_between(F.col("admittime"), F.col("dob")) / F.lit(12.0)
    if cap_age_at_90:
        age = F.when(age > 89, F.lit(90.0)).otherwise(age)

    return base.select(
        F.col("subject_id"),
        F.col("hadm_id"),
        label.cast("int").alias("mortality_label"),
        age.cast("double").alias("age"),
        F.when(F.upper(F.col("gender")) == "M", 1.0).otherwise(0.0).alias("gender_male"),
    )


def build_icu_features(icustays: DataFrame) -> DataFrame:
    return icustays.groupBy("hadm_id").agg(
        F.avg("icu_los_days").alias("icu_los_mean"),
        F.sum("icu_los_days").alias("icu_los_total"),
        F.countDistinct("icustay_id").cast("double").alias("icu_stay_count"),
    )


VITAL_FEATURE_NAMES = ["heart_rate", "map", "resp_rate", "spo2", "temperature_c"]


def build_vital_features(chartevents: DataFrame, apply_range_filter: bool = True,
                         feature_set: str = "compact", intime_ref: DataFrame | None = None) -> DataFrame:
    all_vital_ids = []
    for ids in VITAL_ITEMIDS.values():
        all_vital_ids.extend(ids)
    all_vital_ids.extend(TEMP_C_ITEMIDS)
    all_vital_ids.extend(TEMP_F_ITEMIDS)

    ce = chartevents.where(F.col("itemid").isin(all_vital_ids)).where(F.col("valuenum").isNotNull())

    # PySpark'ta otherwise() aynı when zincirinde yalnızca en sonda kullanılabilir.
    # Bu nedenle itemid -> feature eşleştirmesini when().when().otherwise() zinciriyle kuruyoruz.
    feature_expr = None
    for name, ids in VITAL_ITEMIDS.items():
        condition = F.col("itemid").isin(ids)
        if feature_expr is None:
            feature_expr = F.when(condition, F.lit(name))
        else:
            feature_expr = feature_expr.when(condition, F.lit(name))

    temp_condition = F.col("itemid").isin(TEMP_C_ITEMIDS + TEMP_F_ITEMIDS)
    if feature_expr is None:
        feature_expr = F.when(temp_condition, F.lit("temperature_c"))
    else:
        feature_expr = feature_expr.when(temp_condition, F.lit("temperature_c"))

    feature_expr = feature_expr.otherwise(F.lit(None).cast("string"))

    # Fahrenheit sıcaklıklar Celsius'a çevrilir; diğer vital değerler aynen kullanılır.
    value_expr = F.when(
        F.col("itemid").isin(TEMP_F_ITEMIDS),
        (F.col("valuenum") - F.lit(32.0)) * F.lit(5.0 / 9.0),
    ).otherwise(F.col("valuenum"))

    ce = ce.withColumn("feature", feature_expr).withColumn("value", value_expr)
    ce = ce.where(F.col("feature").isNotNull())

    if apply_range_filter:
        cond = None
        for feature, (lo, hi) in CLINICAL_RANGES.items():
            c = (F.col("feature") == feature) & F.col("value").between(lo, hi)
            cond = c if cond is None else (cond | c)
        ce = ce.where(cond)

    wide = str(feature_set).lower() == "wide"

    # Zaman gerektirmeyen (non-temporal) toplamalar. Compact modda yalnızca mean+count;
    # wide modda ek olarak min/max/stddev/median. Bu toplamalar TÜM satırları (null
    # charttime dahil) kullanır — geriye uyumluluk için compact davranışı aynen korunur.
    aggs = []
    for feature in VITAL_FEATURE_NAMES:
        is_feature = F.col("feature") == feature
        aggs.append(F.avg(F.when(is_feature, F.col("value"))).alias(f"{feature}_mean"))
        aggs.append(
            F.sum(F.when(is_feature & F.col("value").isNotNull(), 1).otherwise(0))
            .cast("double")
            .alias(f"{feature}_count")
        )
        if wide:
            aggs.append(F.min(F.when(is_feature, F.col("value"))).alias(f"{feature}_min"))
            aggs.append(F.max(F.when(is_feature, F.col("value"))).alias(f"{feature}_max"))
            aggs.append(F.stddev(F.when(is_feature, F.col("value"))).alias(f"{feature}_std"))
            aggs.append(
                F.expr(
                    f"percentile_approx(CASE WHEN feature = '{feature}' THEN value END, 0.5)"
                ).alias(f"{feature}_median")
            )

    base = ce.groupBy("hadm_id").agg(*aggs)

    if not wide:
        return base

    # --- Wide: zaman-pencereli (time-windowed) ve trend feature'lar ---
    # Bunlar global sort / window fonksiyonu gerektirir; dağıtık koşumun kazandığı
    # shuffle-yoğun iş profili budur. Null charttime / null intime satırları pencere
    # hesabından açıkça düşürülür (sessiz yanlış sonuç riskini önlemek için).
    if intime_ref is None:
        # Zaman referansı verilmediyse yalnızca non-temporal wide feature'ları döndür.
        return base

    ce_t = (
        ce.where(F.col("charttime").isNotNull())
        .join(intime_ref, on="hadm_id", how="inner")
        .where(F.col("intime_ref").isNotNull())
    )
    hours_since = (
        (F.col("charttime").cast("long") - F.col("intime_ref").cast("long")) / F.lit(3600.0)
    )
    ce_t = ce_t.withColumn("hours_since_intime", hours_since)

    # Her (hadm_id, feature) için charttime'a göre sıralı ilk ve son değer.
    w_full = (
        Window.partitionBy("hadm_id", "feature")
        .orderBy("charttime")
        .rowsBetween(Window.unboundedPreceding, Window.unboundedFollowing)
    )
    ce_t = (
        ce_t
        .withColumn("_first_val", F.first("value").over(w_full))
        .withColumn("_last_val", F.last("value").over(w_full))
    )

    time_aggs = []
    for feature in VITAL_FEATURE_NAMES:
        is_feature = F.col("feature") == feature
        time_aggs.append(
            F.avg(F.when(is_feature & (F.col("hours_since_intime") <= 24), F.col("value")))
            .alias(f"{feature}_mean_24h")
        )
        time_aggs.append(
            F.avg(F.when(is_feature & (F.col("hours_since_intime") <= 48), F.col("value")))
            .alias(f"{feature}_mean_48h")
        )
        time_aggs.append(
            F.max(F.when(is_feature, F.col("_last_val") - F.col("_first_val")))
            .alias(f"{feature}_trend")
        )

    windowed = ce_t.groupBy("hadm_id").agg(*time_aggs)
    return base.join(windowed, on="hadm_id", how="left")


def build_vital_timeseries(chartevents: DataFrame, apply_range_filter: bool,
                           stay_ref: DataFrame, window_hours: int = 6,
                           clip_to_stay: bool = True) -> DataFrame:
    """
    Zaman-serisi (timeseries) feature modu: CHARTEVENTS'i ICU ilk-giris (intime_ref)
    referansina gore sabit `window_hours` saatlik pencerelere boler ve
    (hadm_id, window_idx) duzeyinde vital agregasyonlari uretir. Cikti hasta-seviyesinden
    (hadm basina tek satir) pencere-seviyesine buyur (~1-2M satir); groupBy iki-anahtarli
    oldugu icin shuffle agirlasir -- dagitik kosumun kazandigi is profili budur.
    compact/wide yollarini ETKILEMEZ (ayri fonksiyon).
    """
    all_vital_ids = []
    for ids in VITAL_ITEMIDS.values():
        all_vital_ids.extend(ids)
    all_vital_ids.extend(TEMP_C_ITEMIDS)
    all_vital_ids.extend(TEMP_F_ITEMIDS)

    ce = chartevents.where(F.col("itemid").isin(all_vital_ids)).where(F.col("valuenum").isNotNull())

    feature_expr = None
    for name, ids in VITAL_ITEMIDS.items():
        condition = F.col("itemid").isin(ids)
        feature_expr = (F.when(condition, F.lit(name)) if feature_expr is None
                        else feature_expr.when(condition, F.lit(name)))
    temp_condition = F.col("itemid").isin(TEMP_C_ITEMIDS + TEMP_F_ITEMIDS)
    feature_expr = (F.when(temp_condition, F.lit("temperature_c")) if feature_expr is None
                    else feature_expr.when(temp_condition, F.lit("temperature_c")))
    feature_expr = feature_expr.otherwise(F.lit(None).cast("string"))

    value_expr = F.when(
        F.col("itemid").isin(TEMP_F_ITEMIDS),
        (F.col("valuenum") - F.lit(32.0)) * F.lit(5.0 / 9.0),
    ).otherwise(F.col("valuenum"))

    ce = ce.withColumn("feature", feature_expr).withColumn("value", value_expr)
    ce = ce.where(F.col("feature").isNotNull())

    if apply_range_filter:
        cond = None
        for feature, (lo, hi) in CLINICAL_RANGES.items():
            c = (F.col("feature") == feature) & F.col("value").between(lo, hi)
            cond = c if cond is None else (cond | c)
        ce = ce.where(cond)

    # Zaman-penceresi: null charttime/intime dusurulur (sessiz yanlis sonuc onlemi).
    ce = ce.where(F.col("charttime").isNotNull())
    ce = ce.join(stay_ref, on="hadm_id", how="inner").where(F.col("intime_ref").isNotNull())

    # Pencere kirpma (clip_to_stay): yalnizca ICU kalisi icindeki olcumleri tut
    # (intime_ref <= charttime <= outtime_ref). Kalis-disi patolojik charttime'lar
    # (olcum-zamani hatasi) elenir; boylece window_idx gercek kalis suresine baglanir,
    # 263-gun gibi cop pencereler ve bunlarin yarattigi shuffle skew ortadan kalkar.
    # Flag kapaliysa yalnizca window_idx >= 0 (ICU-oncesi) elemesi uygulanir (eski davranis).
    if clip_to_stay:
        ce = ce.where(
            F.col("outtime_ref").isNotNull()
            & (F.col("charttime") >= F.col("intime_ref"))
            & (F.col("charttime") <= F.col("outtime_ref"))
        )
    window_idx = F.floor(
        (F.col("charttime").cast("long") - F.col("intime_ref").cast("long"))
        / F.lit(float(window_hours) * 3600.0)
    ).cast("int")
    ce = ce.withColumn("window_idx", window_idx).where(F.col("window_idx") >= 0)

    aggs = []
    for feature in VITAL_FEATURE_NAMES:
        is_feature = F.col("feature") == feature
        aggs.append(F.avg(F.when(is_feature, F.col("value"))).alias(f"{feature}_mean"))
        aggs.append(F.min(F.when(is_feature, F.col("value"))).alias(f"{feature}_min"))
        aggs.append(F.max(F.when(is_feature, F.col("value"))).alias(f"{feature}_max"))
        aggs.append(F.stddev(F.when(is_feature, F.col("value"))).alias(f"{feature}_std"))
        aggs.append(
            F.sum(F.when(is_feature & F.col("value").isNotNull(), 1).otherwise(0))
            .cast("double").alias(f"{feature}_count")
        )

    return ce.groupBy("hadm_id", "window_idx").agg(*aggs)


def build_lab_features(labevents: DataFrame, feature_set: str = "compact") -> DataFrame:
    lab = labevents.where(F.col("valuenum").isNotNull())
    abnormal = F.when(
        F.col("flag").isNotNull() & (F.trim(F.col("flag")) != "") & (F.lower(F.col("flag")) != "normal"),
        1,
    ).otherwise(0)
    aggs = [
        F.avg("valuenum").alias("lab_value_mean"),
        F.stddev("valuenum").alias("lab_value_std"),
        F.count("valuenum").cast("double").alias("lab_value_count"),
        F.sum(abnormal).cast("double").alias("lab_abnormal_count"),
    ]
    if str(feature_set).lower() == "wide":
        aggs.extend([
            F.min("valuenum").alias("lab_value_min"),
            F.max("valuenum").alias("lab_value_max"),
            F.expr("percentile_approx(valuenum, 0.5)").alias("lab_value_median"),
        ])
    return lab.groupBy("hadm_id").agg(*aggs)


def build_early_window_stay_ref(icustays: DataFrame, min_los_hours: int) -> DataFrame:
    """Per-hadm_id ICU admission reference for the leak-free early-prediction feature set.

    Admissions whose total ICU envelope (min intime .. max outtime across that
    hadm_id's icustays) is shorter than min_los_hours are excluded entirely, following
    the Harutyunyan et al. MIMIC in-hospital-mortality benchmark convention: without a
    full observation window, an "early prediction at hour min_los_hours" is not
    well-defined for that admission. Only hadm_id/intime_ref are returned; the stay
    duration itself is never exposed as a downstream feature (it would leak the ICU
    discharge time / total LOS into an early-prediction model).
    """
    ref = (
        icustays.where(F.col("intime").isNotNull())
        .groupBy("hadm_id")
        .agg(F.min("intime").alias("intime_ref"), F.max("outtime").alias("outtime_ref"))
    )
    duration_hours = (
        F.col("outtime_ref").cast("long") - F.col("intime_ref").cast("long")
    ) / F.lit(3600.0)
    return (
        ref.where(F.col("outtime_ref").isNotNull())
        .where(duration_hours >= F.lit(float(min_los_hours)))
        .select("hadm_id", "intime_ref")
    )


def build_vital_features_early_window(chartevents: DataFrame, apply_range_filter: bool,
                                      stay_ref: DataFrame, window_hours: int) -> DataFrame:
    """Vital features aggregated ONLY from measurements within [intime_ref, intime_ref+window_hours).

    Self-contained (does not call build_vital_features): that function's non-suffixed
    columns aggregate ALL chartevents with no time filter even in "wide" mode, so reusing
    it here would silently reintroduce the same whole-stay leakage this feature set exists
    to remove. The time filter is applied before any aggregation.
    """
    all_vital_ids = []
    for ids in VITAL_ITEMIDS.values():
        all_vital_ids.extend(ids)
    all_vital_ids.extend(TEMP_C_ITEMIDS)
    all_vital_ids.extend(TEMP_F_ITEMIDS)

    ce = chartevents.where(F.col("itemid").isin(all_vital_ids)).where(F.col("valuenum").isNotNull())

    feature_expr = None
    for name, ids in VITAL_ITEMIDS.items():
        condition = F.col("itemid").isin(ids)
        feature_expr = (F.when(condition, F.lit(name)) if feature_expr is None
                        else feature_expr.when(condition, F.lit(name)))
    temp_condition = F.col("itemid").isin(TEMP_C_ITEMIDS + TEMP_F_ITEMIDS)
    feature_expr = (F.when(temp_condition, F.lit("temperature_c")) if feature_expr is None
                    else feature_expr.when(temp_condition, F.lit("temperature_c")))
    feature_expr = feature_expr.otherwise(F.lit(None).cast("string"))

    value_expr = F.when(
        F.col("itemid").isin(TEMP_F_ITEMIDS),
        (F.col("valuenum") - F.lit(32.0)) * F.lit(5.0 / 9.0),
    ).otherwise(F.col("valuenum"))

    ce = ce.withColumn("feature", feature_expr).withColumn("value", value_expr)
    ce = ce.where(F.col("feature").isNotNull())

    if apply_range_filter:
        cond = None
        for feature, (lo, hi) in CLINICAL_RANGES.items():
            c = (F.col("feature") == feature) & F.col("value").between(lo, hi)
            cond = c if cond is None else (cond | c)
        ce = ce.where(cond)

    # Time filter BEFORE aggregation: only measurements in [intime_ref, intime_ref+window_hours).
    ce = ce.where(F.col("charttime").isNotNull()).join(stay_ref, on="hadm_id", how="inner")
    hours_since = (F.col("charttime").cast("long") - F.col("intime_ref").cast("long")) / F.lit(3600.0)
    ce = ce.where((hours_since >= 0) & (hours_since < F.lit(float(window_hours))))

    aggs = []
    for feature in VITAL_FEATURE_NAMES:
        is_feature = F.col("feature") == feature
        aggs.append(F.avg(F.when(is_feature, F.col("value"))).alias(f"{feature}_mean"))
        aggs.append(F.min(F.when(is_feature, F.col("value"))).alias(f"{feature}_min"))
        aggs.append(F.max(F.when(is_feature, F.col("value"))).alias(f"{feature}_max"))
        aggs.append(F.stddev(F.when(is_feature, F.col("value"))).alias(f"{feature}_std"))
        aggs.append(
            F.expr(
                f"percentile_approx(CASE WHEN feature = '{feature}' THEN value END, 0.5)"
            ).alias(f"{feature}_median")
        )
        aggs.append(
            F.sum(F.when(is_feature & F.col("value").isNotNull(), 1).otherwise(0))
            .cast("double").alias(f"{feature}_count")
        )

    return ce.groupBy("hadm_id").agg(*aggs)


def build_lab_features_early_window(labevents: DataFrame, stay_ref: DataFrame,
                                    window_hours: int) -> DataFrame:
    """Lab features aggregated ONLY from measurements within [intime_ref, intime_ref+window_hours).

    Self-contained (does not call build_lab_features): that function aggregates ALL of
    LABEVENTS per hadm_id with no time filter at all (LABEVENTS carries no charttime in
    the other feature_set paths), which is the primary whole-stay leakage source this
    feature set exists to remove.
    """
    lab = labevents.where(F.col("valuenum").isNotNull()).where(F.col("charttime").isNotNull())
    lab = lab.join(stay_ref, on="hadm_id", how="inner")
    hours_since = (F.col("charttime").cast("long") - F.col("intime_ref").cast("long")) / F.lit(3600.0)
    lab = lab.where((hours_since >= 0) & (hours_since < F.lit(float(window_hours))))
    abnormal = F.when(
        F.col("flag").isNotNull() & (F.trim(F.col("flag")) != "") & (F.lower(F.col("flag")) != "normal"),
        1,
    ).otherwise(0)
    return lab.groupBy("hadm_id").agg(
        F.avg("valuenum").alias("lab_value_mean"),
        F.stddev("valuenum").alias("lab_value_std"),
        F.count("valuenum").cast("double").alias("lab_value_count"),
        F.sum(abnormal).cast("double").alias("lab_abnormal_count"),
        F.min("valuenum").alias("lab_value_min"),
        F.max("valuenum").alias("lab_value_max"),
        F.expr("percentile_approx(valuenum, 0.5)").alias("lab_value_median"),
    )


def build_feature_matrix(tables: Dict[str, DataFrame], cfg: Dict) -> DataFrame:
    cap_age = bool(cfg.get("etl", {}).get("cap_age_at_90", True))
    apply_range = bool(cfg.get("etl", {}).get("apply_clinical_range_filter", True))
    feature_set = str(cfg.get("etl", {}).get("feature_set", "compact")).lower()
    wide = feature_set == "wide"
    timeseries = feature_set == "timeseries"
    early_window = feature_set == "early_window"

    demo = build_demographics(tables["admissions"], tables["patients"], cap_age_at_90=cap_age)

    # --- Early-window modu: sizintisiz erken tahmin ozellik seti (hadm_id basina tek satir) ---
    # icu_los_mean/icu_los_total/icu_stay_count HICBIR ZAMAN hesaplanmaz (build_icu_features
    # bu dalda hic cagrilmaz) -- ICU cikis zamanini/toplam kalis suresini feature olarak
    # sizdirmamanin en saglam yolu, "hesapla sonra dislakla" degil "hic hesaplama".
    if early_window:
        window_hours = int(cfg.get("etl", {}).get("early_window_hours", 48))
        min_los_hours = int(cfg.get("etl", {}).get("early_window_min_los_hours", 48))
        stay_ref_ew = build_early_window_stay_ref(tables["icustays"], min_los_hours)
        vital_ew = build_vital_features_early_window(
            tables["chartevents"], apply_range, stay_ref_ew, window_hours,
        )
        lab_ew = build_lab_features_early_window(tables["labevents"], stay_ref_ew, window_hours)
        features = (
            stay_ref_ew
            .join(demo, on="hadm_id", how="inner")
            .join(vital_ew, on="hadm_id", how="left")
            .join(lab_ew, on="hadm_id", how="left")
        )
        exclude = {"hadm_id", "subject_id", "mortality_label", "age", "gender_male", "intime_ref"}
        clinical_cols = [c for c in features.columns if c not in exclude and not c.endswith("_missing")]
        for c in clinical_cols:
            features = features.withColumn(f"{c}_missing", F.when(F.col(c).isNull(), 1.0).otherwise(0.0))
        return features.drop("intime_ref")

    icu = build_icu_features(tables["icustays"])

    # --- Timeseries modu: (hadm_id, window_idx) duzeyinde cok-satirli matris ---
    # Her ICU kalisi `timeseries_window_hours` saatlik pencerelere bolunur; her pencere
    # satirina o hadm_id'nin mortalite etiketi + demografisi + ICU/lab ozetleri join'lenir.
    # Cikti ~1-2M satira buyur; compact/wide yollari degismez.
    if timeseries:
        window_hours = int(cfg.get("etl", {}).get("timeseries_window_hours", 6))
        clip_to_stay = bool(cfg.get("etl", {}).get("timeseries_clip_to_stay", True))
        # Kalis referansi: hadm_id basina ICU ilk-giris (min intime) ve son-cikis (max outtime).
        # Bir hadm_id'de birden cok icustay olabilir -> min/max ile tek referans (row-explosion onlemi).
        stay_ref = (
            tables["icustays"]
            .where(F.col("intime").isNotNull())
            .groupBy("hadm_id")
            .agg(
                F.min("intime").alias("intime_ref"),
                F.max("outtime").alias("outtime_ref"),
            )
        )
        vital_ts = build_vital_timeseries(
            tables["chartevents"], apply_range, stay_ref,
            window_hours=window_hours, clip_to_stay=clip_to_stay,
        )
        lab = build_lab_features(tables["labevents"], feature_set="compact")
        features = (
            vital_ts
            .join(demo, on="hadm_id", how="inner")
            .join(icu, on="hadm_id", how="left")
            .join(lab, on="hadm_id", how="left")
        )
        return features

    # Zaman-pencereli vital feature'lar için hasta başına ICU ilk-giriş (intime)
    # referansı. Bir hadm_id'de birden çok icustay olabileceğinden min(intime) alınır;
    # aksi halde join'de satır patlaması (row explosion) olur.
    intime_ref = None
    if wide:
        intime_ref = (
            tables["icustays"]
            .where(F.col("intime").isNotNull())
            .groupBy("hadm_id")
            .agg(F.min("intime").alias("intime_ref"))
        )

    vital = build_vital_features(
        tables["chartevents"], apply_range_filter=apply_range,
        feature_set=feature_set, intime_ref=intime_ref,
    )
    lab = build_lab_features(tables["labevents"], feature_set=feature_set)

    features = (
        demo
        .join(icu, on="hadm_id", how="left")
        .join(vital, on="hadm_id", how="left")
        .join(lab, on="hadm_id", how="left")
    )

    # Missingness göstergeleri. Compact modda geriye uyumluluk için sabit liste
    # (byte-özdeş çıktı) korunur; wide modda yeni feature'ları da kapsayacak
    # şekilde dinamik olarak türetilir.
    if wide:
        exclude = {"hadm_id", "subject_id", "mortality_label", "age", "gender_male"}
        clinical_cols = [
            c for c in features.columns
            if c not in exclude and not c.endswith("_missing")
        ]
    else:
        clinical_cols = [
            "icu_los_mean", "icu_los_total", "icu_stay_count", "heart_rate_mean", "map_mean",
            "resp_rate_mean", "spo2_mean", "temperature_c_mean", "lab_value_mean", "lab_value_std",
            "lab_value_count", "lab_abnormal_count",
        ]
    for c in clinical_cols:
        if c in features.columns:
            features = features.withColumn(f"{c}_missing", F.when(F.col(c).isNull(), 1.0).otherwise(0.0))

    return features


def write_feature_matrix(df: DataFrame, cfg: Dict, suffix: str = "") -> Dict[str, str]:
    feature_dir = Path(cfg["paths"]["feature_dir"])
    feature_dir.mkdir(parents=True, exist_ok=True)

    outputs: Dict[str, str] = {}
    out_partitions = int(cfg.get("spark", {}).get("output_partitions", 1))
    df_to_write = df.coalesce(out_partitions)

    if cfg.get("etl", {}).get("write_parquet", True):
        parquet_path = feature_dir / f"feature_matrix{suffix}.parquet"
        df_to_write.write.mode("overwrite").parquet(str(parquet_path))
        outputs["parquet"] = str(parquet_path)

    if cfg.get("etl", {}).get("write_csv", True):
        csv_dir = feature_dir / f"feature_matrix{suffix}_csv"
        df_to_write.write.mode("overwrite").option("header", "true").csv(str(csv_dir))
        outputs["csv_dir"] = str(csv_dir)

    return outputs


def run_etl(cfg: Dict, master: str | None, scenario: str, run_id: str, output_suffix: str, cache_raw: bool) -> Dict:
    mimic_dir = Path(cfg["paths"]["mimic_dir"])

    # Girdi formatı: 'csv' (varsayılan, .csv.gz) veya 'parquet'.
    etl_cfg = cfg.get("etl", {})
    input_format = str(etl_cfg.get("input_format", "csv")).lower()
    parquet_dir = etl_cfg.get("parquet_dir") or cfg.get("paths", {}).get("parquet_dir")

    # csv.gz kontrolü yalnızca csv modunda anlamlıdır; parquet modunda dizin
    # yapısı farklı olduğu için bu kontrolü atlıyoruz.
    if input_format == "csv":
        require_mimic_files(mimic_dir)

    spark = create_spark(cfg, master)
    timings = {
        "scenario": scenario,
        "run_id": run_id,
        "spark_master": spark.sparkContext.master,
        "input_format": input_format,
    }
    counts: Dict[str, int] = {}

    try:
        t0 = time.perf_counter()
        tables = load_raw_tables(spark, mimic_dir, input_format=input_format, parquet_dir=parquet_dir)
        if cache_raw:
            counts = cache_and_count_raw(tables)
        else:
            counts = {name: df.count() for name, df in tables.items()}
        timings["extract_seconds"] = time.perf_counter() - t0

        t1 = time.perf_counter()
        feature_df = build_feature_matrix(tables, cfg).persist(StorageLevel.DISK_ONLY)
        feature_rows = feature_df.count()
        timings["transform_seconds"] = time.perf_counter() - t1

        t2 = time.perf_counter()
        outputs = write_feature_matrix(feature_df, cfg, suffix=output_suffix)
        timings["load_seconds"] = time.perf_counter() - t2

        timings["total_seconds"] = timings["extract_seconds"] + timings["transform_seconds"] + timings["load_seconds"]
        timings["feature_rows"] = feature_rows
        timings["raw_counts"] = counts
        timings["outputs"] = outputs

        timing_path = Path(cfg["paths"]["log_dir"]) / f"etl_timing_{scenario}_{run_id}.json"
        write_json(timings, timing_path)
        return timings
    finally:
        spark.stop()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    result = run_etl(
        cfg=cfg,
        master=args.master,
        scenario=args.scenario,
        run_id=args.run_id,
        output_suffix=args.output_suffix,
        cache_raw=not args.no_cache_raw,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
