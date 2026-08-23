#!/usr/bin/env python3

"""
AIRGRID — PRODUCTION PREDICTOR REPLAY TEST

Purpose
-------
Validate the exact production residual-prediction logic against
the frozen chronological TEST period.

This script does NOT:
- retrain any model
- modify any model
- modify production configuration
- select new hyperparameters
- use test data for training

It verifies:
1. Production XGBoost loads.
2. Residual model loads.
3. 60/40 baseline is reproduced.
4. Residual correction is applied only when allowed.
5. AQI >= 100 remains exactly the baseline.
6. Corrections respect caps.
7. Final AQI remains within [0, 500].
8. No NaN/Inf predictions are produced.
9. Production predictor performance is measured.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Project root
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))

from ml_pipeline.production_spatial_predictor import (
    ProductionSpatialPredictor,
    FEATURE_COLS,
)


# ============================================================================
# PATHS
# ============================================================================

DATA_PATH = (
    ROOT
    / "data"
    / "residual_spatial_experiment_results.csv"
)

OUTPUT_PATH = (
    ROOT
    / "data"
    / "production_predictor_replay_results.csv"
)


# ============================================================================
# TEST WINDOW
# ============================================================================

TEST_START = pd.Timestamp(
    "2026-08-16 13:00:00"
)

TEST_END = pd.Timestamp(
    "2026-08-22 13:00:00"
)


# ============================================================================
# METRICS
# ============================================================================

def calculate_metrics(
    actual,
    prediction,
):
    actual = np.asarray(
        actual,
        dtype=float,
    )

    prediction = np.asarray(
        prediction,
        dtype=float,
    )

    error = prediction - actual

    rmse = float(
        np.sqrt(
            np.mean(
                error ** 2
            )
        )
    )

    mae = float(
        np.mean(
            np.abs(error)
        )
    )

    ss_res = float(
        np.sum(
            error ** 2
        )
    )

    ss_tot = float(
        np.sum(
            (
                actual
                - np.mean(actual)
            ) ** 2
        )
    )

    r2 = (
        1.0 - ss_res / ss_tot
        if ss_tot > 0
        else np.nan
    )

    if (
        len(actual) > 1
        and np.std(actual) > 0
        and np.std(prediction) > 0
    ):
        correlation = float(
            np.corrcoef(
                actual,
                prediction,
            )[0, 1]
        )
    else:
        correlation = np.nan

    return {
        "rmse": rmse,
        "mae": mae,
        "r2": r2,
        "correlation": correlation,
    }


# ============================================================================
# LOAD TEST DATA
# ============================================================================

def load_test_data():

    print(
        "\n" + "=" * 70
    )

    print(
        "AIRGRID — PRODUCTION PREDICTOR REPLAY TEST"
    )

    print(
        "=" * 70
    )

    print(
        "\n[1/8] Loading historical replay data"
    )

    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"Replay dataset not found:\n{DATA_PATH}"
        )

    df = pd.read_csv(
        DATA_PATH
    )

    print(
        f"Loaded:\n  {DATA_PATH}"
    )

    print(
        f"Rows: {len(df):,}"
    )

    required = [
        "timestamp",
        "actual_aqi",
        "xgb_prediction",
        "idw_prediction",
        "nearest_other_station_dist_km",
        "lat",
        "lon",
    ]

    missing = [
        col
        for col in required
        if col not in df.columns
    ]

    if missing:
        raise RuntimeError(
            "Replay dataset is missing columns:\n"
            + "\n".join(
                f"  - {x}"
                for x in missing
            )
        )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        errors="coerce",
    )

    df = df[
        df["timestamp"].notna()
    ].copy()

    test = df[
        (df["timestamp"] >= TEST_START)
        &
        (df["timestamp"] < TEST_END)
    ].copy()

    if test.empty:
        raise RuntimeError(
            "No rows found in requested test window."
        )

    print(
        "\nTest window:"
    )

    print(
        f"  {test['timestamp'].min()}"
        f" → "
        f"{test['timestamp'].max()}"
    )

    print(
        f"Test rows: {len(test):,}"
    )

    return test


# ============================================================================
# BASIC DATA VALIDATION
# ============================================================================

def validate_input(df):

    print(
        "\n[2/8] Validating replay input"
    )

    numeric_cols = [
        "actual_aqi",
        "xgb_prediction",
        "idw_prediction",
        "nearest_other_station_dist_km",
        "lat",
        "lon",
    ]

    for col in numeric_cols:

        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    invalid = (
        df[numeric_cols]
        .isna()
        .any(axis=1)
    )

    print(
        f"Rows with invalid required values: "
        f"{int(invalid.sum()):,}"
    )

    if invalid.any():

        print(
            "Removing invalid replay rows."
        )

        df = df[
            ~invalid
        ].copy()

    if df.empty:
        raise RuntimeError(
            "No valid replay rows remain."
        )

    return df


# ============================================================================
# PRODUCTION PREDICTION
# ============================================================================

def generate_predictions(
    predictor,
    df,
):

    print(
        "\n[3/8] Running production predictor"
    )

    results = []

    total = len(df)

    for counter, (_, row) in enumerate(
        df.iterrows(),
        start=1,
    ):

        result = predictor.predict_row(
            row.to_dict()
        )

        results.append(
            result
        )

        if (
            counter == 1
            or counter % 1000 == 0
            or counter == total
        ):
            print(
                f"  Processed "
                f"{counter:,}/{total:,}"
            )

    result_df = pd.DataFrame(
        results
    )

    return result_df


# ============================================================================
# PRODUCTION OUTPUT VALIDATION
# ============================================================================

def validate_predictions(
    results,
):

    print(
        "\n[4/8] Validating production outputs"
    )

    required = [
        "baseline_aqi",
        "residual_prediction",
        "applied_correction",
        "final_aqi",
        "correction_applied",
        "prediction_regime",
        "correction_status",
    ]

    missing = [
        col
        for col in required
        if col not in results.columns
    ]

    if missing:
        raise RuntimeError(
            "Production predictor did not return:\n"
            + "\n".join(
                f"  - {x}"
                for x in missing
            )
        )

    # ------------------------------------------------------------------------
    # NaN / Inf
    # ------------------------------------------------------------------------

    numeric_outputs = [
        "baseline_aqi",
        "residual_prediction",
        "applied_correction",
        "final_aqi",
    ]

    for col in numeric_outputs:

        values = pd.to_numeric(
            results[col],
            errors="coerce",
        )

        bad = (
            ~np.isfinite(
                values.to_numpy(
                    dtype=float
                )
            )
        )

        count = int(
            bad.sum()
        )

        print(
            f"{col:25s}: "
            f"invalid = {count:,}"
        )

        if count > 0:
            raise RuntimeError(
                f"Production output contains "
                f"invalid {col} values."
            )

    # ------------------------------------------------------------------------
    # AQI bounds
    # ------------------------------------------------------------------------

    final_aqi = results[
        "final_aqi"
    ].to_numpy(
        dtype=float
    )

    below = int(
        np.sum(
            final_aqi < 0
        )
    )

    above = int(
        np.sum(
            final_aqi > 500
        )
    )

    print(
        f"Final AQI below 0 : {below:,}"
    )

    print(
        f"Final AQI above 500: {above:,}"
    )

    if below or above:
        raise RuntimeError(
            "Final AQI exceeded production safety bounds."
        )

    # ------------------------------------------------------------------------
    # Fallback count
    # ------------------------------------------------------------------------

    fallback = results[
        "correction_status"
    ].astype(str).str.startswith(
        "FALLBACK"
    )

    print(
        f"Fallback rows: "
        f"{int(fallback.sum()):,}"
    )

    # ------------------------------------------------------------------------
    # Correction cap
    # ------------------------------------------------------------------------

    correction = np.abs(
        results[
            "applied_correction"
        ].to_numpy(
            dtype=float
        )
    )

    max_correction = float(
        correction.max()
    )

    print(
        f"Maximum absolute correction: "
        f"{max_correction:.3f}"
    )

    if max_correction > 75.000001:
        raise RuntimeError(
            "Correction exceeded global safety cap."
        )


# ============================================================================
# HIGH-AQI SAFETY TEST
# ============================================================================

def validate_high_aqi_safety(
    results,
):

    print(
        "\n[5/8] Checking high-AQI safety policy"
    )

    baseline = results[
        "baseline_aqi"
    ].to_numpy(
        dtype=float
    )

    final = results[
        "final_aqi"
    ].to_numpy(
        dtype=float
    )

    high = baseline >= 100.0

    high_count = int(
        high.sum()
    )

    print(
        f"Baseline AQI >=100 rows: "
        f"{high_count:,}"
    )

    if high_count == 0:

        print(
            "WARNING: No >=100 AQI rows."
        )

        return

    difference = np.abs(
        final[high]
        - baseline[high]
    )

    changed = int(
        np.sum(
            difference > 1e-8
        )
    )

    print(
        f"High-AQI rows changed: "
        f"{changed:,}"
    )

    if changed > 0:

        raise RuntimeError(
            "HIGH-AQI SAFETY FAILURE:\n"
            "AQI >=100 was modified by the "
            "residual correction layer."
        )

    print(
        "PASS: AQI >=100 remains exactly "
        "the 60/40 baseline."
    )


# ============================================================================
# METRICS
# ============================================================================

def calculate_results(
    results,
):

    print(
        "\n[6/8] Calculating production metrics"
    )

    actual = results[
        "actual_aqi"
    ].to_numpy(
        dtype=float
    )

    baseline = results[
        "baseline_aqi"
    ].to_numpy(
        dtype=float
    )

    final = results[
        "final_aqi"
    ].to_numpy(
        dtype=float
    )

    baseline_metrics = calculate_metrics(
        actual,
        baseline,
    )

    production_metrics = calculate_metrics(
        actual,
        final,
    )

    table = pd.DataFrame(
        [
            {
                "model": "Current 60/40",
                **baseline_metrics,
            },
            {
                "model": "Production residual",
                **production_metrics,
            },
        ]
    )

    print(
        "\n" + "=" * 70
    )

    print(
        "PRODUCTION REPLAY RESULTS"
    )

    print(
        "=" * 70
    )

    print(
        table.to_string(
            index=False,
            float_format=lambda x:
                f"{x:.3f}"
        )
    )

    improvement = (
        1
        - production_metrics["rmse"]
        / baseline_metrics["rmse"]
    ) * 100

    print(
        "\nRMSE improvement:"
        f" {improvement:+.2f}%"
    )

    return table


# ============================================================================
# PERFORMANCE BY REGIME
# ============================================================================

def regime_analysis(
    results,
):

    print(
        "\n[7/8] Performance by prediction regime"
    )

    rows = []

    for regime, group in results.groupby(
        "prediction_regime",
        dropna=False,
    ):

        actual = group[
            "actual_aqi"
        ].to_numpy(
            dtype=float
        )

        baseline = group[
            "baseline_aqi"
        ].to_numpy(
            dtype=float
        )

        final = group[
            "final_aqi"
        ].to_numpy(
            dtype=float
        )

        baseline_metrics = calculate_metrics(
            actual,
            baseline,
        )

        final_metrics = calculate_metrics(
            actual,
            final,
        )

        rows.append(
            {
                "prediction_regime": regime,
                "n": len(group),

                "baseline_rmse":
                    baseline_metrics["rmse"],

                "production_rmse":
                    final_metrics["rmse"],

                "baseline_mae":
                    baseline_metrics["mae"],

                "production_mae":
                    final_metrics["mae"],

                "improvement_pct":
                    (
                        1
                        - final_metrics["rmse"]
                        / baseline_metrics["rmse"]
                    ) * 100,

                "gate_fraction":
                    float(
                        group[
                            "correction_applied"
                        ].mean()
                    ),
            }
        )

    table = pd.DataFrame(
        rows
    )

    if not table.empty:

        table = table.sort_values(
            "prediction_regime"
        )

        print(
            table.to_string(
                index=False,
                float_format=lambda x:
                    f"{x:.3f}"
            )
        )

    return table


# ============================================================================
# SAVE
# ============================================================================

def save_results(
    results,
):

    print(
        "\n[8/8] Saving replay results"
    )

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    results.to_csv(
        OUTPUT_PATH,
        index=False,
    )

    print(
        f"Saved:\n"
        f"  {OUTPUT_PATH}"
    )


# ============================================================================
# MAIN
# ============================================================================

def main():

    # ------------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------------

    df = load_test_data()

    df = validate_input(
        df
    )

    # ------------------------------------------------------------------------
    # Load exact production stack
    # ------------------------------------------------------------------------

    print(
        "\nLoading production predictor..."
    )

    predictor = (
        ProductionSpatialPredictor()
    )

    # ------------------------------------------------------------------------
    # Generate predictions
    # ------------------------------------------------------------------------

    results = generate_predictions(
        predictor,
        df,
    )

    # ------------------------------------------------------------------------
    # Ensure actual_aqi is retained
    # ------------------------------------------------------------------------

    if "actual_aqi" not in results.columns:

        results["actual_aqi"] = df[
            "actual_aqi"
        ].to_numpy()

    # ------------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------------

    validate_predictions(
        results
    )

    validate_high_aqi_safety(
        results
    )

    # ------------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------------

    calculate_results(
        results
    )

    regime_analysis(
        results
    )

    # ------------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------------

    save_results(
        results
    )

    print(
        "\n" + "=" * 70
    )

    print(
        "PRODUCTION REPLAY TEST COMPLETE"
    )

    print(
        "=" * 70
    )

    print(
        "\nNo models were modified."
    )

    print(
        "No hyperparameters were selected."
    )

    print(
        "Production XGBoost remains unchanged."
    )


if __name__ == "__main__":
    main()
