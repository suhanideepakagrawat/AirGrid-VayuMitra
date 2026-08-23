#!/usr/bin/env python3

"""
AIRGRID — PRODUCTION SPATIAL AQI PREDICTOR

Production architecture:

    XGBoost
       +
      IDW
       ↓
  60/40 baseline
       ↓
 Residual correction
       ↓
 Regime safety gate
       ↓
 Final AQI

IMPORTANT:

1. Production XGBoost is never modified.
2. Residual model is optional.
3. Any residual-model failure falls back to baseline.
4. Residual correction is currently enabled only for:
       AQI < 50
       AQI 50-100
5. AQI >= 100 uses the original 60/40 baseline.
6. Corrections are hard capped.
7. Output is clipped to [0, 500].
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
import xgboost as xgb


# ============================================================================
# PATHS
# ============================================================================

ROOT_DIR = Path(__file__).resolve().parents[1]

MODEL_DIR = ROOT_DIR / "models"
CONFIG_PATH = ROOT_DIR / "ml_pipeline" / "production_residual_config.json"

PRODUCTION_XGB_PATH = MODEL_DIR / "spatial_estimator.json"
RESIDUAL_MODEL_PATH = MODEL_DIR / "residual_spatial_model.json"


# ============================================================================
# CONSTANTS
# ============================================================================

BASELINE_XGB_WEIGHT = 0.60
BASELINE_IDW_WEIGHT = 0.40

MIN_AQI = 0.0
MAX_AQI = 500.0

FEATURE_COLS = [
    "idw_prediction",
    "xgb_prediction",
    "xgb_idw_gap",
    "nearest_other_station_dist_km",
    "lat",
    "lon",
    "hour",
    "weekday",
    "month",
    "day_of_year",
    "hour_sin",
    "hour_cos",
    "weekday_sin",
    "weekday_cos",
    "distance_lt_1_km",
    "distance_1_2_km",
    "distance_2_3_km",
    "distance_3_5_km",
    "distance_5_10_km",
    "distance_10_plus_km",
]


# ============================================================================
# MODEL LOADING
# ============================================================================

class ProductionSpatialPredictor:

    def __init__(self):

        self.production_model = None
        self.residual_model = None
        self.config = None

        self.residual_enabled = False

        self._load_config()
        self._load_production_model()
        self._load_residual_model()

    # ----------------------------------------------------------------------

    def _load_config(self):

        if not CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"Production configuration not found:\n"
                f"{CONFIG_PATH}"
            )

        with open(CONFIG_PATH, "r") as f:
            self.config = json.load(f)

        if not self.config.get("enabled", True):
            print(
                "Production residual correction is DISABLED."
            )

    # ----------------------------------------------------------------------

    def _load_production_model(self):

        if not PRODUCTION_XGB_PATH.exists():
            raise FileNotFoundError(
                f"Production XGBoost model not found:\n"
                f"{PRODUCTION_XGB_PATH}"
            )

        self.production_model = xgb.XGBRegressor()

        self.production_model.load_model(
            str(PRODUCTION_XGB_PATH)
        )

        print(
            f"Loaded production XGBoost:\n"
            f"  {PRODUCTION_XGB_PATH}"
        )

    # ----------------------------------------------------------------------

    def _load_residual_model(self):

        if not self.config.get(
            "residual",
            {}
        ).get(
            "enabled",
            False
        ):
            print(
                "Residual correction disabled by configuration."
            )
            return

        if not RESIDUAL_MODEL_PATH.exists():

            print(
                "WARNING: residual model not found."
            )

            print(
                "Falling back to baseline 60/40."
            )

            return

        try:

            self.residual_model = xgb.XGBRegressor()

            self.residual_model.load_model(
                str(RESIDUAL_MODEL_PATH)
            )

            self.residual_enabled = True

            print(
                f"Loaded residual model:\n"
                f"  {RESIDUAL_MODEL_PATH}"
            )

        except Exception as exc:

            print(
                "WARNING: failed to load residual model:"
            )

            print(exc)

            print(
                "Falling back to baseline 60/40."
            )

            self.residual_model = None
            self.residual_enabled = False

    # =========================================================================
    # FEATURE ENGINEERING
    # =========================================================================

    @staticmethod
    def build_residual_features(
        df: pd.DataFrame
    ) -> pd.DataFrame:

        result = df.copy()

        timestamp = pd.to_datetime(
            result["timestamp"],
            errors="coerce"
        )

        result["timestamp"] = timestamp

        result["hour"] = timestamp.dt.hour
        result["weekday"] = timestamp.dt.weekday
        result["month"] = timestamp.dt.month
        result["day_of_year"] = timestamp.dt.dayofyear

        result["hour_sin"] = np.sin(
            2 * np.pi * result["hour"] / 24.0
        )

        result["hour_cos"] = np.cos(
            2 * np.pi * result["hour"] / 24.0
        )

        result["weekday_sin"] = np.sin(
            2 * np.pi * result["weekday"] / 7.0
        )

        result["weekday_cos"] = np.cos(
            2 * np.pi * result["weekday"] / 7.0
        )

        result["xgb_idw_gap"] = (
            result["xgb_prediction"]
            - result["idw_prediction"]
        )

        # --------------------------------------------------------------
        # Distance regime
        # --------------------------------------------------------------

        if "nearest_other_station_dist_km" not in result.columns:
            result[
                "nearest_other_station_dist_km"
            ] = np.nan

        distance = pd.to_numeric(
            result[
                "nearest_other_station_dist_km"
            ],
            errors="coerce"
        )

        result["distance_lt_1_km"] = (
            (distance < 1.0)
            .astype(float)
        )

        result["distance_1_2_km"] = (
            ((distance >= 1.0) & (distance < 2.0))
            .astype(float)
        )

        result["distance_2_3_km"] = (
            ((distance >= 2.0) & (distance < 3.0))
            .astype(float)
        )

        result["distance_3_5_km"] = (
            ((distance >= 3.0) & (distance < 5.0))
            .astype(float)
        )

        result["distance_5_10_km"] = (
            ((distance >= 5.0) & (distance < 10.0))
            .astype(float)
        )

        result["distance_10_plus_km"] = (
            (distance >= 10.0)
            .astype(float)
        )

        return result

    # =========================================================================
    # BASELINE
    # =========================================================================

    @staticmethod
    def build_baseline(
        xgb_prediction,
        idw_prediction
    ):

        xgb_prediction = float(xgb_prediction)
        idw_prediction = float(idw_prediction)

        baseline = (
            BASELINE_XGB_WEIGHT * xgb_prediction
            +
            BASELINE_IDW_WEIGHT * idw_prediction
        )

        return float(
            np.clip(
                baseline,
                MIN_AQI,
                MAX_AQI
            )
        )

    # =========================================================================
    # REGIME
    # =========================================================================

    @staticmethod
    def get_regime(
        baseline: float
    ) -> str:

        if baseline < 50:
            return "lt50"

        if baseline < 100:
            return "50_100"

        return "100_plus"

    # =========================================================================
    # RESIDUAL CORRECTION
    # =========================================================================

    def apply_residual_correction(
        self,
        baseline: float,
        residual_prediction: float
    ):

        regime = self.get_regime(
            baseline
        )

        regime_config = (
            self.config
            .get("residual", {})
            .get("regimes", {})
            .get(regime)
        )

        # --------------------------------------------------------------
        # Safety: regime not configured
        # --------------------------------------------------------------

        if regime_config is None:
            return (
                baseline,
                0.0,
                False,
                regime,
                "regime_not_configured"
            )

        # --------------------------------------------------------------
        # Safety: explicitly disabled
        # --------------------------------------------------------------

        if not regime_config.get(
            "enabled",
            False
        ):

            return (
                baseline,
                0.0,
                False,
                regime,
                "regime_disabled"
            )

        threshold = float(
            regime_config.get(
                "threshold",
                0.0
            )
        )

        lam = float(
            regime_config.get(
                "lambda",
                0.0
            )
        )

        cap = float(
            regime_config.get(
                "cap",
                0.0
            )
        )

        # --------------------------------------------------------------
        # Invalid residual
        # --------------------------------------------------------------

        if not np.isfinite(
            residual_prediction
        ):

            return (
                baseline,
                0.0,
                False,
                regime,
                "invalid_residual"
            )

        # --------------------------------------------------------------
        # Gate
        # --------------------------------------------------------------

        if abs(residual_prediction) < threshold:

            return (
                baseline,
                0.0,
                False,
                regime,
                "below_threshold"
            )

        # --------------------------------------------------------------
        # Apply correction
        # --------------------------------------------------------------

        correction = (
            lam * residual_prediction
        )

        # Hard cap
        correction = float(
            np.clip(
                correction,
                -cap,
                cap
            )
        )

        corrected = (
            baseline
            + correction
        )

        # --------------------------------------------------------------
        # Final AQI safety bounds
        # --------------------------------------------------------------

        corrected = float(
            np.clip(
                corrected,
                MIN_AQI,
                MAX_AQI
            )
        )

        applied = (
            corrected
            - baseline
        )

        return (
            corrected,
            float(applied),
            True,
            regime,
            "correction_applied"
        )

    # =========================================================================
    # PREDICT ONE
    # =========================================================================

    def predict_row(
        self,
        row: Dict
    ) -> Dict:

        """
        Input must contain:

        xgb_prediction
        idw_prediction
        timestamp
        lat
        lon
        nearest_other_station_dist_km

        The remaining residual features are generated automatically.
        """

        try:

            row_df = pd.DataFrame(
                [row]
            )

            # ----------------------------------------------------------
            # Baseline
            # ----------------------------------------------------------

            xgb_prediction = float(
                row_df.iloc[0][
                    "xgb_prediction"
                ]
            )

            idw_prediction = float(
                row_df.iloc[0][
                    "idw_prediction"
                ]
            )

            baseline = self.build_baseline(
                xgb_prediction,
                idw_prediction
            )

            # ----------------------------------------------------------
            # Residual disabled
            # ----------------------------------------------------------

            if not self.residual_enabled:

                return {
                    **row,
                    "baseline_aqi": baseline,
                    "residual_prediction": 0.0,
                    "applied_correction": 0.0,
                    "final_aqi": baseline,
                    "correction_applied": False,
                    "prediction_regime": self.get_regime(
                        baseline
                    ),
                    "correction_status": (
                        "residual_model_disabled"
                    ),
                    "model_version": "production_60_40"
                }

            # ----------------------------------------------------------
            # Feature construction
            # ----------------------------------------------------------

            features = (
                self.build_residual_features(
                    row_df
                )
            )

            missing = [
                col
                for col in FEATURE_COLS
                if col not in features.columns
            ]

            if missing:

                raise RuntimeError(
                    "Missing residual features: "
                    + ", ".join(missing)
                )

            X = features[
                FEATURE_COLS
            ].copy()

            # ----------------------------------------------------------
            # Numeric conversion
            # ----------------------------------------------------------

            for col in FEATURE_COLS:

                X[col] = pd.to_numeric(
                    X[col],
                    errors="coerce"
                )

            # ----------------------------------------------------------
            # Reject NaN / inf
            # ----------------------------------------------------------

            X = X.replace(
                [np.inf, -np.inf],
                np.nan
            )

            if X.isna().any().any():

                raise RuntimeError(
                    "Residual feature vector contains "
                    "NaN values."
                )

            # ----------------------------------------------------------
            # Residual prediction
            # ----------------------------------------------------------

            residual_prediction = float(
                self.residual_model.predict(
                    X
                )[0]
            )

            # ----------------------------------------------------------
            # Apply production safety policy
            # ----------------------------------------------------------

            (
                final_aqi,
                applied_correction,
                correction_applied,
                regime,
                status
            ) = self.apply_residual_correction(
                baseline,
                residual_prediction
            )

            return {
                **row,

                "baseline_aqi": baseline,

                "residual_prediction":
                    residual_prediction,

                "applied_correction":
                    applied_correction,

                "final_aqi":
                    final_aqi,

                "correction_applied":
                    correction_applied,

                "prediction_regime":
                    regime,

                "correction_status":
                    status,

                "model_version":
                    "production_60_40_plus_safe_residual_v1"
            }

        except Exception as exc:

            # ==========================================================
            # PRODUCTION FAILSAFE
            # ==========================================================

            try:

                baseline = self.build_baseline(
                    row["xgb_prediction"],
                    row["idw_prediction"]
                )

            except Exception:

                baseline = np.nan

            return {
                **row,

                "baseline_aqi":
                    baseline,

                "residual_prediction":
                    np.nan,

                "applied_correction":
                    0.0,

                "final_aqi":
                    baseline,

                "correction_applied":
                    False,

                "prediction_regime":
                    (
                        self.get_regime(baseline)
                        if np.isfinite(baseline)
                        else "unknown"
                    ),

                "correction_status":
                    f"FALLBACK: {exc}",

                "model_version":
                    "production_60_40_fallback"
            }

    # =========================================================================
    # BATCH PREDICTION
    # =========================================================================

    def predict_dataframe(
        self,
        df: pd.DataFrame
    ) -> pd.DataFrame:

        required = [
            "xgb_prediction",
            "idw_prediction",
            "timestamp",
        ]

        missing = [
            col
            for col in required
            if col not in df.columns
        ]

        if missing:

            raise ValueError(
                "Missing required columns: "
                + ", ".join(missing)
            )

        outputs = []

        for _, row in df.iterrows():

            outputs.append(
                self.predict_row(
                    row.to_dict()
                )
            )

        return pd.DataFrame(
            outputs
        )


# ============================================================================
# CLI TEST
# ============================================================================

def main():

    print(
        "\n" + "=" * 70
    )

    print(
        "AIRGRID — PRODUCTION RESIDUAL PREDICTOR"
    )

    print(
        "=" * 70
    )

    predictor = (
        ProductionSpatialPredictor()
    )

    print(
        "\nProduction configuration loaded."
    )

    print(
        "\nIMPORTANT:"
    )

    print(
        "  Production XGBoost: UNCHANGED"
    )

    print(
        "  Baseline: 60% XGB + 40% IDW"
    )

    print(
        "  Residual correction: <100 AQI only"
    )

    print(
        "  AQI >=100: baseline only"
    )

    print(
        "  Failsafe: baseline fallback"
    )

    print(
        "\nReady for integration."
    )


if __name__ == "__main__":
    main()
