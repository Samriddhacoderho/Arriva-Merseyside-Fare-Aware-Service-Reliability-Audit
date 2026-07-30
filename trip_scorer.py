from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
PROCESSED_DIR = HERE / "outputs" / "processed"
ANALYTICAL_PATH = PROCESSED_DIR / "analytical_journeys.parquet"
FEATURES_META = HERE / "outputs" / "features" / "feature_metadata.json"
GBT_PATH = HERE / "outputs" / "machine_learning" / "best_model" / "GBT"
PIPELINE_PATH = HERE / "outputs" / "machine_learning" / "feature_pipeline"

LABEL_COL = "label"
FEATURES_COL = "features"

_spark = None
_bundle: dict[str, Any] | None = None


def _read_metadata() -> tuple[list[str], list[str]]:
    meta = json.loads(FEATURES_META.read_text(encoding="utf-8"))
    return meta["cat_cols"], meta["num_cols"]


def get_spark():
    global _spark
    if _spark is None:
        import os
        import sys

        from pyspark.sql import SparkSession

        os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
        os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)
        _spark = (
            SparkSession.builder.appName("Merseyside_Dashboard_Scorer")
            .master("local[1]")
            .config("spark.driver.memory", "2g")
            .config("spark.sql.shuffle.partitions", "4")
            .config("spark.ui.enabled", "false")
            .getOrCreate()
        )
        _spark.sparkContext.setLogLevel("ERROR")
    return _spark


def _read_parquet(spark, path: Path):
    return spark.read.parquet(str(path))


STRUCTURAL_ZERO_COLS = [
    "disruption_severity_score",
    "n_disruption_matches",
    "had_planned_disruption",
    "had_real_merseytravel_disruption",
    "disruption_expected_delay_avg",
    "n_obs",
    "used_near_departure_ping",
    "has_txc_line_match",
    "has_fare_match",
    "has_disruption",
]

LINE_MEDIAN_COLS = [
    "scheduled_trip_minutes",
    "latitude",
    "longitude",
]

CAT_IMPUTE = {
    "direction_ref": "unknown",
    "operator_ref": "UNK",
    "fare_band": "unknown",
    "fare_proxy_source": "unknown",
    "primary_disruption_reason": "none",
    "primary_disruption_severity": "none",
    "line_key": "UNK",
}

HIST_PRIORS = {
    "hist_line_noncompliance_rate": 0.5,
    "hist_line_delay_mean": 0.0,
    "hist_line_delay_std": 0.0,
    "hist_line_n_prior": 0.0,
}


def _line_then_network_median(df, col: str):
    import pyspark.sql.functions as F

    if col not in df.columns:
        return df

    line_stats = df.groupBy("line_key").agg(
        F.expr(f"percentile_approx(`{col}`, 0.5)").alias(f"_{col}_line_med")
    )
    network_med = df.agg(
        F.expr(f"percentile_approx(`{col}`, 0.5)").alias("med")
    ).collect()[0]["med"]

    out = df.join(line_stats, on="line_key", how="left")
    out = out.withColumn(
        col,
        F.coalesce(
            F.col(col),
            F.col(f"_{col}_line_med"),
            F.lit(float(network_med) if network_med is not None else 0.0),
        ),
    ).drop(f"_{col}_line_med")
    return out


def _add_temporal_and_history_features(df):
    """Mirror Phase 4 imputation + history logic from the notebook."""
    import pyspark.sql.functions as F
    from pyspark.sql.window import Window

    out = (
        df.withColumn("hour_of_day", F.col("hour_of_day").cast("double"))
        .withColumn("day_of_week", F.col("day_of_week").cast("double"))
        .withColumn("is_peak", F.col("is_peak").cast("double"))
        .withColumn("is_compliant", F.col("is_compliant").cast("double"))
        .withColumn("label", F.col("is_compliant").cast("double"))
    )

    out = (
        out.withColumn("direction_missing", F.col("direction_ref").isNull().cast("double"))
        .withColumn(
            "fare_imputed",
            F.when(F.col("has_fare_match") == 1, F.lit(0.0)).otherwise(F.lit(1.0)),
        )
        .withColumn(
            "gps_missing",
            (F.col("latitude").isNull() | F.col("longitude").isNull()).cast("double"),
        )
        .withColumn(
            "schedule_imputed",
            F.col("scheduled_trip_minutes").isNull().cast("double"),
        )
    )

    out = out.fillna(CAT_IMPUTE)

    if "fare_proxy" in out.columns:
        network_fare = (
            out.filter(F.col("fare_proxy").isNotNull())
            .agg(F.expr("percentile_approx(`fare_proxy`, 0.5)").alias("med"))
            .collect()[0]["med"]
        )
        out = out.withColumn("fare_proxy", F.col("fare_proxy").cast("double"))
        if network_fare is not None:
            out = out.withColumn(
                "fare_proxy",
                F.coalesce(F.col("fare_proxy"), F.lit(float(network_fare))),
            )

    for c in STRUCTURAL_ZERO_COLS:
        if c in out.columns:
            out = out.withColumn(c, F.col(c).cast("double")).fillna({c: 0.0})

    for c in LINE_MEDIAN_COLS:
        out = _line_then_network_median(out, c)

    w_hist = (
        Window.partitionBy("line_key")
        .orderBy(F.col("origin_aimed_departure_time").asc())
        .rowsBetween(Window.unboundedPreceding, -1)
    )

    out = (
        out.withColumn(
            "hist_line_noncompliance_rate",
            F.lit(1.0) - F.avg("is_compliant").over(w_hist),
        )
        .withColumn("hist_line_delay_mean", F.avg("delay_minutes").over(w_hist))
        .withColumn("hist_line_delay_std", F.stddev("delay_minutes").over(w_hist))
        .withColumn("hist_line_n_prior", F.count("is_compliant").over(w_hist).cast("double"))
    )

    out = out.fillna(HIST_PRIORS)
    return out


def _build_feature_pipeline(cat_cols: list[str], num_cols: list[str]):
    from pyspark.ml import Pipeline
    from pyspark.ml.feature import OneHotEncoder, StringIndexer, VectorAssembler

    indexers = [
        StringIndexer(inputCol=c, outputCol=f"{c}_idx", handleInvalid="keep")
        for c in cat_cols
    ]
    encoders = [
        OneHotEncoder(inputCol=f"{c}_idx", outputCol=f"{c}_ohe", handleInvalid="keep")
        for c in cat_cols
    ]
    ohe_cols = [f"{c}_ohe" for c in cat_cols]
    assembler = VectorAssembler(
        inputCols=num_cols + ohe_cols,
        outputCol=FEATURES_COL,
        handleInvalid="keep",
    )
    return Pipeline(stages=indexers + encoders + [assembler])


def _ensure_pipeline_model(spark, cat_cols: list[str], num_cols: list[str]):
    from pyspark.ml import PipelineModel

    if PIPELINE_PATH.exists():
        return PipelineModel.load(str(PIPELINE_PATH))

    raw = _read_parquet(spark, ANALYTICAL_PATH)
    featured = _add_temporal_and_history_features(raw)
    pipeline = _build_feature_pipeline(cat_cols, num_cols)
    model = pipeline.fit(featured)
    PIPELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    model.write().overwrite().save(str(PIPELINE_PATH))
    return model


def get_scoring_bundle() -> dict[str, Any]:
    """Load (or build) Spark session, feature pipeline, and GBT — cached in-process."""
    global _bundle
    if _bundle is not None:
        return _bundle

    from pyspark.ml.classification import GBTClassificationModel

    cat_cols, num_cols = _read_metadata()
    spark = get_spark()
    pipeline_model = _ensure_pipeline_model(spark, cat_cols, num_cols)
    gbt = GBTClassificationModel.load(str(GBT_PATH))

    options = load_field_options()

    _bundle = {
        "spark": spark,
        "pipeline": pipeline_model,
        "gbt": gbt,
        "cat_cols": cat_cols,
        "num_cols": num_cols,
        "options": options,
    }
    return _bundle


def load_field_options() -> dict[str, list[Any]]:
    """Distinct values per model input field (for dropdowns)."""
    if not ANALYTICAL_PATH.exists():
        return {}

    aj = pd.read_parquet(ANALYTICAL_PATH)
    cat_cols, num_cols = _read_metadata()
    opts: dict[str, list[Any]] = {}

    for c in cat_cols:
        if c in aj.columns:
            vals = sorted(aj[c].dropna().astype(str).unique().tolist())
            opts[c] = vals

    for c in num_cols:
        if c not in aj.columns:
            continue
        if c.startswith("hist_line_"):
            continue
        if aj[c].nunique() <= 12:
            opts[c] = sorted(aj[c].dropna().unique().tolist())

    return opts


def line_history_defaults(trips: pd.DataFrame, line_key: str) -> dict[str, float]:
    """Median line-history features for a line (from scored trips)."""
    sub = trips[trips["line_key"].astype(str) == str(line_key)]
    if sub.empty:
        return {
            "hist_line_noncompliance_rate": 0.5,
            "hist_line_delay_mean": 0.0,
            "hist_line_delay_std": 0.0,
            "hist_line_n_prior": 0.0,
        }
    return {
        "hist_line_noncompliance_rate": float(sub["hist_line_noncompliance_rate"].median()),
        "hist_line_delay_mean": float(sub["hist_line_delay_mean"].median()),
        "hist_line_delay_std": float(sub["hist_line_delay_std"].median()),
        "hist_line_n_prior": float(sub["hist_line_n_prior"].median()),
    }


def scenario_defaults(trips: pd.DataFrame, line_key: str = "79C") -> dict[str, Any]:
    """Typical model inputs for a bus line (dashboard starting values)."""
    cat_cols, num_cols = _read_metadata()
    out: dict[str, Any] = {}

    if ANALYTICAL_PATH.exists():
        aj = pd.read_parquet(ANALYTICAL_PATH)
        sub = aj[aj["line_key"].astype(str) == str(line_key)]
        src = sub if not sub.empty else aj
        for c in cat_cols:
            if c in src.columns:
                out[c] = str(src[c].mode().iloc[0] if not src[c].mode().empty else src[c].iloc[0])
        for c in num_cols:
            if c in src.columns and not c.startswith("hist_line_"):
                out[c] = float(src[c].median())

    out.update(line_history_defaults(trips, line_key))
    out["line_key"] = str(line_key)
    out.setdefault("hour_of_day", 8.0)
    out.setdefault("day_of_week", 3.0)
    out.setdefault("is_peak", 1.0)
    out.setdefault("fare_proxy", 1.81)
    out.setdefault("operator_ref", "ANWE")
    out.setdefault("fare_band", "network_default")
    out.setdefault("fare_proxy_source", "network_default")
    out.setdefault("primary_disruption_reason", "none")
    out.setdefault("primary_disruption_severity", "none")
    out.setdefault("direction_missing", 0.0)
    out.setdefault("fare_imputed", 1.0)
    out.setdefault("gps_missing", 0.0)
    out.setdefault("schedule_imputed", 0.0)
    return out


def row_from_trip(trip_row: pd.Series) -> dict[str, Any]:
    """Build scorer inputs from trip_risk + analytical_journeys (full ML fields)."""
    cat_cols, num_cols = _read_metadata()
    fields = cat_cols + num_cols
    out: dict[str, Any] = {}

    if ANALYTICAL_PATH.exists() and "journey_key" in trip_row.index:
        aj = pd.read_parquet(ANALYTICAL_PATH)
        match = aj[aj["journey_key"] == trip_row["journey_key"]]
        if not match.empty:
            base = match.iloc[0]
            for c in fields:
                if c in base.index and pd.notna(base[c]):
                    out[c] = base[c]

    for c in fields:
        if c not in out and c in trip_row.index and pd.notna(trip_row[c]):
            out[c] = trip_row[c]

    for c in fields:
        if c.startswith("hist_line_") and c in trip_row.index and pd.notna(trip_row[c]):
            out[c] = trip_row[c]

    if "origin_aimed_departure_time" in trip_row.index:
        out["origin_aimed_departure_time"] = str(trip_row["origin_aimed_departure_time"])

    return out


def _departure_timestamp(inputs: dict[str, Any]) -> str:
    """Build a departure timestamp from day-of-week and hour widgets."""
    from datetime import date, timedelta

    if "origin_aimed_departure_time" in inputs and inputs["origin_aimed_departure_time"]:
        return str(inputs["origin_aimed_departure_time"])

    hour = int(float(inputs.get("hour_of_day", 12)))
    hour = max(0, min(23, hour))
    dow = int(float(inputs.get("day_of_week", 3)))
    # 1=Sunday … 7=Saturday; 2026-07-12 is a Sunday in the audit week.
    base_sunday = date(2026, 7, 12)
    depart_date = base_sunday + timedelta(days=dow - 1)
    return f"{depart_date.isoformat()} {hour:02d}:00:00"


def _prepare_scenario_row(inputs: dict[str, Any]) -> pd.DataFrame:
    """Single-row frame with columns required before feature pipeline transform."""
    cat_cols, num_cols = _read_metadata()
    defaults: dict[str, Any] = {}
    if ANALYTICAL_PATH.exists():
        aj = pd.read_parquet(ANALYTICAL_PATH)
        for c in cat_cols:
            if c in aj.columns:
                defaults[c] = str(aj[c].dropna().iloc[0])
        for c in num_cols:
            if c in aj.columns:
                defaults[c] = float(aj[c].dropna().iloc[0])

    row = {**defaults, **inputs}
    row["origin_aimed_departure_time"] = _departure_timestamp(row)

    data: dict[str, Any] = {"origin_aimed_departure_time": row["origin_aimed_departure_time"]}
    for c in cat_cols:
        data[c] = str(row.get(c, defaults.get(c, "unknown")))
    for c in num_cols:
        val = row.get(c, defaults.get(c, 0.0))
        data[c] = float(val) if val is not None and pd.notna(val) else 0.0

    return pd.DataFrame([data])


def score_scenario(inputs: dict[str, Any]) -> dict[str, Any]:
    """
    Score a hypothetical trip with the saved GBT model.

    Returns probability_compliant, prob_noncompliant, prediction, fare_exposure.
    """
    import pyspark.sql.functions as F
    from pyspark.ml.functions import vector_to_array

    bundle = get_scoring_bundle()
    spark = bundle["spark"]
    pipeline = bundle["pipeline"]
    gbt = bundle["gbt"]

    pdf = _prepare_scenario_row(inputs)
    sdf = spark.createDataFrame(pdf.to_dict(orient="records"))

    fe = pipeline.transform(sdf)
    pred = gbt.transform(fe)

    row = pred.select(
        "prediction",
        vector_to_array("probability")[1].alias("probability_compliant"),
        F.col("fare_proxy"),
    ).collect()[0]

    p_compliant = float(row["probability_compliant"])
    p_noncompliant = 1.0 - p_compliant
    fare = float(inputs.get("fare_proxy", row["fare_proxy"] or 0.0))
    exposure = p_noncompliant * fare

    if p_noncompliant >= 0.75:
        band = "High"
    elif p_noncompliant >= 0.45:
        band = "Medium"
    else:
        band = "Low"

    return {
        "prediction": int(row["prediction"]),
        "prediction_label": "Compliant (on time)" if int(row["prediction"]) == 1 else "Non-compliant (late)",
        "probability_compliant": p_compliant,
        "prob_noncompliant": p_noncompliant,
        "fare_proxy": fare,
        "fare_exposure": exposure,
        "risk_band": band,
        "model_name": "GBT",
    }


def shutdown_spark() -> None:
    global _spark, _bundle
    if _spark is not None:
        _spark.stop()
        _spark = None
    _bundle = None
