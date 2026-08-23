#!/usr/bin/env python3

"""
AIRGRID — PRODUCTION FAILSAFE TEST

Verifies that the production predictor always returns the existing
60/40 baseline when the residual correction layer fails.

Tests:
1. Normal prediction.
2. Residual model unavailable.
3. Residual prediction failure.
4. Missing optional residual inputs.
5. Invalid residual output.

The original XGBoost and 60/40 baseline must remain usable in every case.
"""

from pathlib import Path
import sys

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ml_pipeline.production_spatial_predictor import (
    ProductionSpatialPredictor,
)


DATA_PATH = (
    ROOT
    / "data"
    / "residual_spatial_experiment_results.csv"
)


def get_test_rows():

    df = pd.read_csv(DATA_PATH)

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        errors="coerce",
    )

    df = df[
        df["timestamp"] >= pd.Timestamp(
            "2026-08-16 13:00:00"
        )
    ].copy()

    if df.empty:
        raise RuntimeError(
            "No test rows available."
        )

    return df.reset_index(
        drop=True
    )


def assert_valid_baseline(result):

    if "baseline_aqi" not in result:
        raise RuntimeError(
            "baseline_aqi missing from predictor output."
        )

    baseline = float(
        result["baseline_aqi"]
    )

    if not np.isfinite(baseline):
        raise RuntimeError(
            "Baseline AQI is invalid."
        )

    if baseline < 0 or baseline > 500:
        raise RuntimeError(
            f"Baseline AQI outside [0,500]: {baseline}"
        )


def run_normal_test(row):

    print(
        "\n" + "=" * 70
    )

    print(
        "TEST 1 — NORMAL PRODUCTION PATH"
    )

    print(
        "=" * 70
    )

    predictor = ProductionSpatialPredictor()

    result = predictor.predict_row(
        row.to_dict()
    )

    assert_valid_baseline(
        result
    )

    final = float(
        result["final_aqi"]
    )

    if not np.isfinite(final):
        raise RuntimeError(
            "Normal final AQI is invalid."
        )

    print(
        f"Baseline AQI : "
        f"{result['baseline_aqi']:.3f}"
    )

    print(
        f"Final AQI    : "
        f"{result['final_aqi']:.3f}"
    )

    print(
        f"Status       : "
        f"{result.get('correction_status')}"
    )

    print(
        "PASS — normal production path works."
    )


def run_missing_residual_model_test(row):

    print(
        "\n" + "=" * 70
    )

    print(
        "TEST 2 — RESIDUAL MODEL FAILURE"
    )

    print(
        "=" * 70
    )

    predictor = ProductionSpatialPredictor()

    baseline_result = predictor.predict_row(
        row.to_dict()
    )

    baseline = float(
        baseline_result["baseline_aqi"]
    )

    # Deliberately break the residual model.
    predictor.residual_model = None

    try:

        result = predictor.predict_row(
            row.to_dict()
        )

    except Exception as exc:

        raise RuntimeError(
            "FAIL: predictor raised an exception "
            "instead of falling back to baseline."
        ) from exc

    final = float(
        result["final_aqi"]
    )

    if not np.isclose(
        final,
        baseline,
        atol=1e-8,
    ):

        raise RuntimeError(
            "FAIL: residual-model failure did not "
            "return the 60/40 baseline."
        )

    print(
        f"Baseline AQI : {baseline:.3f}"
    )

    print(
        f"Fallback AQI : {final:.3f}"
    )

    print(
        f"Status       : "
        f"{result.get('correction_status')}"
    )

    print(
        "PASS — residual model failure falls back "
        "to the 60/40 baseline."
    )


def run_invalid_residual_test(row):

    print(
        "\n" + "=" * 70
    )

    print(
        "TEST 3 — INVALID RESIDUAL OUTPUT"
    )

    print(
        "=" * 70
    )

    predictor = ProductionSpatialPredictor()

    baseline_result = predictor.predict_row(
        row.to_dict()
    )

    baseline = float(
        baseline_result["baseline_aqi"]
    )

    class BrokenResidualModel:

        def predict(self, X):

            return np.array(
                [np.nan]
            )

    predictor.residual_model = (
        BrokenResidualModel()
    )

    try:

        result = predictor.predict_row(
            row.to_dict()
        )

    except Exception as exc:

        raise RuntimeError(
            "FAIL: invalid residual prediction "
            "caused the production predictor to crash."
        ) from exc

    final = float(
        result["final_aqi"]
    )

    if not np.isfinite(final):

        raise RuntimeError(
            "FAIL: invalid residual output produced "
            "invalid final AQI."
        )

    if not np.isclose(
        final,
        baseline,
        atol=1e-8,
    ):

        raise RuntimeError(
            "FAIL: invalid residual output did not "
            "fall back to baseline."
        )

    print(
        f"Baseline AQI : {baseline:.3f}"
    )

    print(
        f"Fallback AQI : {final:.3f}"
    )

    print(
        "PASS — invalid residual output safely "
        "falls back to baseline."
    )


def run_high_aqi_test():

    print(
        "\n" + "=" * 70
    )

    print(
        "TEST 4 — HIGH AQI SAFETY"
    )

    print(
        "=" * 70
    )

    df = get_test_rows()

    high_row = None

    for _, row in df.iterrows():

        predictor = ProductionSpatialPredictor()

        result = predictor.predict_row(
            row.to_dict()
        )

        if float(
            result["baseline_aqi"]
        ) >= 100:

            high_row = row
            break

    if high_row is None:

        print(
            "WARNING — no baseline AQI >=100 row found."
        )

        return

    predictor = ProductionSpatialPredictor()

    result = predictor.predict_row(
        high_row.to_dict()
    )

    baseline = float(
        result["baseline_aqi"]
    )

    final = float(
        result["final_aqi"]
    )

    print(
        f"Baseline AQI : {baseline:.3f}"
    )

    print(
        f"Final AQI    : {final:.3f}"
    )

    if not np.isclose(
        baseline,
        final,
        atol=1e-8,
    ):

        raise RuntimeError(
            "FAIL: high-AQI prediction was modified."
        )

    print(
        "PASS — AQI >=100 remains baseline-only."
    )


def run_cap_test(row):

    print(
        "\n" + "=" * 70
    )

    print(
        "TEST 5 — CORRECTION CAP"
    )

    print(
        "=" * 70
    )

    predictor = ProductionSpatialPredictor()

    df = get_test_rows()

    max_correction = 0.0

    for _, current_row in df.head(
        1000
    ).iterrows():

        result = predictor.predict_row(
            current_row.to_dict()
        )

        correction = abs(
            float(
                result.get(
                    "applied_correction",
                    0.0,
                )
            )
        )

        max_correction = max(
            max_correction,
            correction,
        )

    print(
        f"Maximum observed correction: "
        f"{max_correction:.3f}"
    )

    if max_correction > 75.000001:

        raise RuntimeError(
            "FAIL: correction exceeded 75 AQI cap."
        )

    print(
        "PASS — correction cap respected."
    )


def main():

    print(
        "\n" + "=" * 70
    )

    print(
        "AIRGRID — PRODUCTION FAILSAFE TEST"
    )

    print(
        "=" * 70
    )

    df = get_test_rows()

    print(
        f"Replay rows available: "
        f"{len(df):,}"
    )

    # Use a normal low/mid-AQI row for failure tests.
    row = None

    for _, candidate in df.iterrows():

        predictor = ProductionSpatialPredictor()

        result = predictor.predict_row(
            candidate.to_dict()
        )

        baseline = float(
            result["baseline_aqi"]
        )

        if baseline < 100:

            row = candidate
            break

    if row is None:

        raise RuntimeError(
            "Could not find a baseline AQI <100 row."
        )

    run_normal_test(
        row
    )

    run_missing_residual_model_test(
        row
    )

    run_invalid_residual_test(
        row
    )

    run_high_aqi_test()

    run_cap_test(
        row
    )

    print(
        "\n" + "=" * 70
    )

    print(
        "ALL PRODUCTION FAILSAFE TESTS PASSED"
    )

    print(
        "=" * 70
    )

    print(
        "\nThe residual layer can fail without "
        "taking down the baseline predictor."
    )

    print(
        "The 60/40 baseline remains the recovery path."
    )


if __name__ == "__main__":
    main()
