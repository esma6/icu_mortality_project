#!/usr/bin/env python
"""
Bir kerelik yardımcı: MIMIC-III .csv.gz tablolarını, ETL hattının kullandığı
şemalarla BİREBİR aynı olacak şekilde Parquet formatına dönüştürür.

Amaç: gzip dosyaları bölünemediği (non-splittable) için Spark bunları tek
task ile okur ve worker paralelizmi devreye giremez. Parquet hem bölünebilir
hem sütunsaldır; bu script ile csv.gz vs Parquet karşılaştırması yaparak
ETL darboğazının veri formatı kaynaklı olup olmadığını KONTROLLÜ biçimde
test ederiz.

Bu script spark_etl_mimic.py'yi DEĞİŞTİRMEZ; yalnızca yeni bir girdi üretir.
Container içinde spark-master üzerinde çalıştırılmak üzere tasarlanmıştır,
böylece ETL deneyleriyle aynı ortamı kullanır.

Kullanım (container içinde):
  docker compose exec -T -w /app spark-master \
    /opt/spark/bin/spark-submit \
    --master local[*] \
    /app/scripts/convert_mimic_to_parquet.py \
    --src /data/mimic --dst /data/mimic_parquet
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from pyspark.sql import SparkSession, functions as F, types as T


def schema(columns: list[str]) -> T.StructType:
    return T.StructType([T.StructField(c, T.StringType(), True) for c in columns])


# spark_etl_mimic.py içindeki SCHEMAS ile BİREBİR aynı olmalıdır.
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

# Parquet yazarken tek dev dosya yerine makul boyutlu parçalar üretmek için
# dosya başına maksimum satır sınırı. repartition (tam shuffle, bellek yoğun)
# yerine bunu kullanıyoruz; böylece büyük tablolarda OutOfMemory riski olmadan
# çıktı yine de bölünebilir (splittable) olur.
MAX_RECORDS_PER_FILE = {
    "CHARTEVENTS": 5_000_000,
    "LABEVENTS": 5_000_000,
    "ADMISSIONS": 0,   # 0 = sınır yok (küçük tablo)
    "PATIENTS": 0,
    "ICUSTAYS": 0,
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Convert MIMIC-III csv.gz to Parquet (schema-identical to ETL)")
    p.add_argument("--src", default="/data/mimic", help="csv.gz dosyalarının bulunduğu dizin")
    p.add_argument("--dst", default="/data/mimic_parquet", help="Parquet çıktısının yazılacağı dizin")
    p.add_argument("--tables", nargs="*", default=list(SCHEMAS.keys()),
                   help="Dönüştürülecek tablolar (varsayılan: hepsi)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    src = Path(args.src)
    dst = Path(args.dst)

    spark = (
        SparkSession.builder
        .appName("MIMIC_csv_to_parquet")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")

    print(f"[INFO] Kaynak: {src}")
    print(f"[INFO] Hedef: {dst}")

    for name in args.tables:
        if name not in SCHEMAS:
            print(f"[WARN] {name} şemada yok, atlanıyor.")
            continue

        in_path = str(src / f"{name}.csv.gz")
        out_path = str(dst / name)  # her tablo kendi klasörüne
        max_records = MAX_RECORDS_PER_FILE.get(name, 0)

        print(f"\n=== {name} dönüştürülüyor -> {out_path} (max_records_per_file={max_records or 'sınırsız'}) ===")
        t0 = time.perf_counter()

        # read_table ile BİREBİR aynı okuma: şema + küçük harfe çevirme.
        df = (
            spark.read
            .option("header", "true")
            .option("mode", "PERMISSIVE")
            .schema(SCHEMAS[name])
            .csv(in_path)
        )
        df = df.select([F.col(c).alias(c.lower()) for c in df.columns])

        writer = df.write.mode("overwrite")
        # Büyük tablolarda tam shuffle (repartition) yerine, dosya başına satır
        # sınırıyla bölünmüş çıktı üretiyoruz. Bu, bellek dostudur ve Spark'ın
        # okurken paralelleşmesine izin veren splittable bir Parquet üretir.
        if max_records and max_records > 0:
            writer = writer.option("maxRecordsPerFile", max_records)
        writer.parquet(out_path)

        elapsed = time.perf_counter() - t0
        print(f"[OK] {name} tamam ({elapsed:.1f} s)")

    spark.stop()
    print("\n[DONE] Tüm tablolar Parquet olarak yazıldı:", dst)


if __name__ == "__main__":
    main()