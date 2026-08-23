#!/usr/bin/env python3

"""
AIRGRID — PRODUCTION LIVE AQI PIPELINE

Pipeline:

OpenAQ/CPCB
    ↓
live_station_data.csv
    ↓
fresh station observations
    ↓
1600-cell XGBoost + IDW spatial estimation
    ↓
60/40 baseline
    ↓
production residual correction
    ↓
safety/fallback
    ↓
live_cell_aqi.csv

This script orchestrates the existing production components.
It does NOT retrain or modify any model.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "data"

LIVE_STATION_PATH = DATA_DIR / "live_station_data.csv"
LIVE_CELL_PATH = DATA_DIR / "live_cell_aqi.csv"

UPDATE_SCRIPT = ROOT / "ml_pipeline" / "update_live_cell_aqi.py"


# ============================================================
# CONFIG
# ============================================================

DEFAULT_MAX_AGE = 120
DEFAULT_IDW_POWER = 2.0
DEFAULT_IDW_STATIONS = 5

EXPECTED_CELLS = 1600


# ============================================================
# LOGGING
# ============================================================

def banner(title: str):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ============================================================
# RUN EXISTING SPATIAL PIPELINE
# ============================================================

def run_spatial_update(
    max_age: int,
    idw_power: float,
    idw_stations: int,
):
    banner("[1/4] RUNNING LIVE SPATIAL ESTIMATOR")

    if not UPDATE_SCRIPT.exists():
        raise FileNotFoundError(
            f"Missing spatial update script:\n{UPDATE_SCRIPT}"
        )

    command = [
        sys.executable,
        str(UPDATE_SCRIPT),
        "--max-age-minutes",
        str(max_age),
        "--output",
        str(LIVE_CELL_PATH),
        "--idw-power",
        str(idw_power),
        "--idw-max-stations",
        str(idw_stations),
        "--xgb-weight",
        "0.60",
        "--idw-weight",
        "0.40",
    ]

    print("Running:")
    print(" ".join(command))
    print()

    result = subprocess.run(
        command,
        cwd=str(ROOT),
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Live spatial AQI update failed."
        )

    if not LIVE_CELL_PATH.exists():
        raise RuntimeError(
            f"Spatial estimator completed but output was not created:\n"
            f"{LIVE_CELL_PATH}"
        )


# ============================================================
# VALIDATE SPATIAL OUTPUT
# ============================================================

def validate_spatial_output():

    banner("[2/4] VALIDATING SPATIAL OUTPUT")

    df = pd.read_csv(LIVE_CELL_PATH)

    required = [
        "cell_id",
        "lat",
        "lon",
        "estimated_current_aqi",
        "source",
        "live_reference_timestamp",
    ]

    missing = [
        c for c in required
        if c not in df.columns
    ]

    if missing:
        raise RuntimeError(
            f"Live cell output is missing columns: {missing}"
        )

    print(f"Rows: {len(df)}")
    print(f"Columns: {len(df.columns)}")

    if len(df) != EXPECTED_CELLS:
        raise RuntimeError(
            f"Expected {EXPECTED_CELLS} cells, "
            f"got {len(df)}."
        )

    invalid = (
        pd.to_numeric(
            df["estimated_current_aqi"],
            errors="coerce",
        ).isna()
    )

    invalid_count = int(invalid.sum())

    print(
        f"Invalid AQI rows: {invalid_count}"
    )

    if invalid_count:
        raise RuntimeError(
            "Live cell output contains invalid AQI values."
        )

    aqi = pd.to_numeric(
        df["estimated_current_aqi"],
        errors="coerce",
    )

    if (aqi < 0).any():
        raise RuntimeError(
            "Negative AQI detected."
        )

    if (aqi > 500).any():
        raise RuntimeError(
            "AQI above 500 detected."
        )

    print(
        f"AQI range: {aqi.min():.2f} → {aqi.max():.2f}"
    )

    print()
    print("Source distribution:")
    print(
        df["source"]
        .value_counts()
        .to_string()
    )

    return df


# ============================================================
# APPLY PRODUCTION RESIDUAL MODEL
# ============================================================

def apply_production_predictor(df):

    banner("[3/4] APPLYING PRODUCTION RESIDUAL LAYER")

    # Import here so the runner can still validate the
    # spatial estimator independently.
    from production_spatial_predictor import (
        ProductionSpatialPredictor,
        FEATURE_COLS,
    )

    predictor = ProductionSpatialPredictor()

    # --------------------------------------------------------
    # IMPORTANT
    #
    # update_live_cell_aqi.py already produced:
    #
    #     xgb_spatial_aqi
    #     idw_aqi
    #     estimated_current_aqi
    #
    # The production residual predictor expects:
    #
    #     xgb_prediction
    #     idw_prediction
    #
    # --------------------------------------------------------

    if "xgb_spatial_aqi" not in df.columns:
        raise RuntimeError(
            "Missing xgb_spatial_aqi from live spatial output."
        )

    if "idw_aqi" not in df.columns:
        raise RuntimeError(
            "Missing idw_aqi from live spatial output."
        )

    work = df.copy()

    work["xgb_prediction"] = pd.to_numeric(
        work["xgb_spatial_aqi"],
        errors="coerce",
    )

    work["idw_prediction"] = pd.to_numeric(
        work["idw_aqi"],
        errors="coerce",
    )

    # --------------------------------------------------------
    # Timestamp
    # --------------------------------------------------------

    if "live_reference_timestamp" in work.columns:
        work["timestamp"] = pd.to_datetime(
            work["live_reference_timestamp"],
            errors="coerce",
        )

    if work["timestamp"].isna().any():
        raise RuntimeError(
            "Invalid live reference timestamp."
        )

    # --------------------------------------------------------
    # Residual predictor expects these spatial features.
    #
    # If live output does not contain them, use safe defaults.
    # These features were not used by the spatial estimator itself
    # but are required by the residual model.
    # --------------------------------------------------------

    if "nearest_other_station_dist_km" not in work.columns:

        if "nearest_dist_km" in work.columns:
            work[
                "nearest_other_station_dist_km"
            ] = pd.to_numeric(
                work["nearest_dist_km"],
                errors="coerce",
            )
        else:
            work[
                "nearest_other_station_dist_km"
            ] = 10.0

    # --------------------------------------------------------
    # Required residual feature engineering
    # --------------------------------------------------------

    work = predictor.build_residual_features(work)

    # --------------------------------------------------------
    # Calculate baseline
    # --------------------------------------------------------

    work["baseline_aqi"] = [
        predictor.build_baseline(
            xgb_prediction=xgb,
            idw_prediction=idw,
        )
        for xgb, idw in zip(
            work["xgb_prediction"],
            work["idw_prediction"],
        )
    ]

    # --------------------------------------------------------
    # Residual prediction
    # --------------------------------------------------------

    residual_prediction = np.zeros(
        len(work),
        dtype=float,
    )

    if predictor.residual_enabled:

        feature_frame = work[
            FEATURE_COLS
        ].copy()

        feature_frame = feature_frame.replace(
            [np.inf, -np.inf],
            np.nan,
        )

        feature_frame = feature_frame.fillna(0.0)

        try:

            residual_prediction = (
                predictor.residual_model
                .predict(feature_frame)
            )

            residual_prediction = np.asarray(
                residual_prediction,
                dtype=float,
            )

        except Exception as exc:

            print(
                "WARNING: residual prediction failed."
            )

            print(exc)

            print(
                "Using baseline for all cells."
            )

            residual_prediction = np.zeros(
                len(work),
                dtype=float,
            )

    work["residual_prediction"] = (
        residual_prediction
    )

    # --------------------------------------------------------
    # Production configuration
    # --------------------------------------------------------

    config = predictor.config

    residual_config = config.get(
        "residual",
        {},
    )

    regimes = residual_config.get(
        "regimes",
        {},
    )

    safety = config.get(
        "safety",
        {},
    )

    max_correction = float(
        safety.get(
            "max_correction_absolute",
            75.0,
        )
    )

    # --------------------------------------------------------
    # Apply regime policy
    # --------------------------------------------------------

    final_prediction = work[
        "baseline_aqi"
    ].to_numpy(
        dtype=float
    ).copy()

    applied_correction = np.zeros(
        len(work),
        dtype=float,
    )

    gate_applied = np.zeros(
        len(work),
        dtype=bool,
    )

    baseline = work[
        "baseline_aqi"
    ].to_numpy(
        dtype=float
    )

    residual = work[
        "residual_prediction"
    ].to_numpy(
        dtype=float
    )

    for i, baseline_value in enumerate(baseline):

        regime = None

        if baseline_value < 50:
            regime = "lt50"

        elif baseline_value < 100:
            regime = "50_100"

        else:
            regime = "100_plus"

        cfg = regimes.get(
            regime,
            {},
        )

        if not cfg.get(
            "enabled",
            False,
        ):
            continue

        threshold = float(
            cfg.get(
                "threshold",
                0.0,
            )
        )

        lam = float(
            cfg.get(
                "lambda",
                0.0,
            )
        )

        cap = min(
            float(
                cfg.get(
                    "cap",
                    max_correction,
                )
            ),
            max_correction,
        )

        correction = (
            lam * residual[i]
        )

        # Gate based on residual magnitude.
        if abs(correction) < threshold:
            continue

        correction = float(
            np.clip(
                correction,
                -cap,
                cap,
            )
        )

        final_prediction[i] = (
            baseline_value
            + correction
        )

        applied_correction[i] = correction
        gate_applied[i] = True

    # --------------------------------------------------------
    # Safety
    # --------------------------------------------------------

    final_prediction = np.clip(
        final_prediction,
        float(
            safety.get(
                "min_absolute_aqi",
                0.0,
            )
        ),
        float(
            safety.get(
                "max_absolute_aqi",
                500.0,
            )
        ),
    )

    # --------------------------------------------------------
    # CRITICAL:
    #
    # Direct live station observations remain authoritative.
    #
    # If source == live_station, do NOT alter that measured AQI
    # with the residual model.
    # --------------------------------------------------------

    direct_station = (
        work["source"]
        .astype(str)
        .eq("live_station")
    )

    if direct_station.any():

        station_values = pd.to_numeric(
            work.loc[
                direct_station,
                "estimated_current_aqi"
            ],
            errors="coerce",
        ).to_numpy()

        final_prediction[
            direct_station.to_numpy()
        ] = station_values

        applied_correction[
            direct_station.to_numpy()
        ] = 0.0

        gate_applied[
            direct_station.to_numpy()
        ] = False

    # --------------------------------------------------------
    # Save production fields
    # --------------------------------------------------------

    work[
        "baseline_aqi"
    ] = baseline

    work[
        "residual_prediction"
    ] = residual

    work[
        "applied_correction"
    ] = applied_correction

    work[
        "residual_gate_applied"
    ] = gate_applied

    work[
        "estimated_current_aqi"
    ] = final_prediction

    work[
        "prediction_method"
    ] = np.where(
        direct_station,
        "live_station",
        np.where(
            gate_applied,
            "spatial_blend_residual",
            "spatial_blend",
        ),
    )

    return work


# ============================================================
# FINAL VALIDATION
# ============================================================

def validate_final_output(df):

    banner("[4/4] FINAL PRODUCTION VALIDATION")

    required = [
        "cell_id",
        "lat",
        "lon",
        "estimated_current_aqi",
        "source",
        "prediction_method",
        "live_reference_timestamp",
    ]

    missing = [
        c for c in required
        if c not in df.columns
    ]

    if missing:
        raise RuntimeError(
            f"Final output missing columns: {missing}"
        )

    aqi = pd.to_numeric(
        df["estimated_current_aqi"],
        errors="coerce",
    )

    print(
        f"Cells: {len(df)}"
    )

    print(
        f"Invalid AQI: {int(aqi.isna().sum())}"
    )

    print(
        f"AQI < 0: {int((aqi < 0).sum())}"
    )

    print(
        f"AQI > 500: {int((aqi > 500).sum())}"
    )

    if aqi.isna().any():
        raise RuntimeError(
            "Final output contains NaN AQI."
        )

    if (aqi < 0).any() or (aqi > 500).any():
        raise RuntimeError(
            "Final AQI safety bounds violated."
        )

    # --------------------------------------------------------
    # Direct station integrity
    # --------------------------------------------------------

    direct = (
        df["source"]
        .astype(str)
        .eq("live_station")
    )

    direct_changed = 0

    if direct.any():

        original = pd.to_numeric(
            df.loc[
                direct,
                "xgb_spatial_aqi"
            ],
            errors="coerce",
        )

        # For direct cells, estimated_current_aqi is the
        # measured station AQI, not the XGB value.
        # Therefore simply verify correction is zero.
        direct_changed = int(
            (
                pd.to_numeric(
                    df.loc[
                        direct,
                        "applied_correction"
                    ],
                    errors="coerce",
                ).abs() > 1e-9
            ).sum()
        )

    print(
        f"Direct live-station cells: {int(direct.sum())}"
    )

    print(
        f"Direct cells with residual correction: "
        f"{direct_changed}"
    )

    if direct_changed:
        raise RuntimeError(
            "Measured station cells were modified by residual layer."
        )

    # --------------------------------------------------------
    # Correction cap
    # --------------------------------------------------------

    max_correction = pd.to_numeric(
        df["applied_correction"],
        errors="coerce",
    ).abs().max()

    print(
        f"Maximum correction: "
        f"{max_correction:.3f}"
    )

    if max_correction > 75.000001:
        raise RuntimeError(
            "Residual correction cap violated."
        )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()
    print("Prediction method:")
    print(
        df["prediction_method"]
        .value_counts()
        .to_string()
    )

    print()
    print("AQI statistics:")
    print(
        aqi.describe().to_string()
    )

    print()
    print(
        "Reference timestamp:",
        df["live_reference_timestamp"].iloc[0],
    )


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "AirGrid production live AQI pipeline"
        )
    )

    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=DEFAULT_MAX_AGE,
    )

    parser.add_argument(
        "--idw-power",
        type=float,
        default=DEFAULT_IDW_POWER,
    )

    parser.add_argument(
        "--idw-max-stations",
        type=int,
        default=DEFAULT_IDW_STATIONS,
    )

    args = parser.parse_args()

    banner(
        "AIRGRID — PRODUCTION LIVE AQI PIPELINE"
    )

    print(
        "Configuration:"
    )

    print(
        f"  Maximum station age : "
        f"{args.max_age_minutes} min"
    )

    print(
        f"  IDW power           : "
        f"{args.idw_power}"
    )

    print(
        f"  IDW stations        : "
        f"{args.idw_max_stations}"
    )

    # --------------------------------------------------------
    # Run spatial estimator
    # --------------------------------------------------------

    run_spatial_update(
        max_age=args.max_age_minutes,
        idw_power=args.idw_power,
        idw_stations=args.idw_max_stations,
    )

    # --------------------------------------------------------
    # Validate spatial output
    # --------------------------------------------------------

    df = validate_spatial_output()

    # --------------------------------------------------------
    # Apply production residual model
    # --------------------------------------------------------

    df = apply_production_predictor(df)

    # --------------------------------------------------------
    # Final validation
    # --------------------------------------------------------

    validate_final_output(df)

    # --------------------------------------------------------
    # Select clean frontend columns
    # --------------------------------------------------------

    frontend_columns = [
        "cell_id",
        "lat",
        "lon",
        "nearest_station",
        "nearest_dist_km",
        "estimated_current_aqi",
        "source",
        "prediction_method",
        "baseline_aqi",
        "residual_prediction",
        "applied_correction",
        "residual_gate_applied",
        "live_reference_timestamp",
    ]

    frontend_columns = [
        c for c in frontend_columns
        if c in df.columns
    ]

    output = df[
        frontend_columns
    ].copy()

    output.to_csv(
        LIVE_CELL_PATH,
        index=False,
    )

    banner(
        "LIVE AQI UPDATE COMPLETE"
    )

    print(
        f"Saved:\n{LIVE_CELL_PATH}"
    )

    print()
    print(
        "Frontend-ready rows:",
        len(output),
    )

    print()
    print(
        "Source distribution:"
    )

    print(
        output["source"]
        .value_counts()
        .to_string()
    )


if __name__ == "__main__":
    main()
