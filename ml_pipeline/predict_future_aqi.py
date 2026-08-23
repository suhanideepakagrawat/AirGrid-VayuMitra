"""
predict_future_aqi.py
---------------------

AirGrid live AQI forecasting.

Produces:

    +24h
    +48h
    +72h

AQI forecasts for all 1600 cells.

PIPELINE
--------

historical AQI
      +
live 1600-cell AQI
      +
current weather
      +
static features
      +
forecast weather
      ↓
XGBoost forecaster
      ↓
ML prediction
      +
persistence
      ↓
ensemble AQI
      ↓
confidence classification
      ↓
future_aqi_forecast.csv

IMPORTANT
---------

Land-use features are intentionally empty in the training data:

    industrial_pct
    construction_pct
    green_cover_pct
    residential_pct
    water_pct

They remain NaN during inference.

XGBoost handles these missing values natively.

LIVE GAP
--------

Historical AQI ends at 13:00.

Live AQI arrives at 22:30.

The model source hour is the floor of the live timestamp:

    22:30 -> 22:00

The live AQI snapshot is used as the current source-state AQI.

No intermediate AQI rows are fabricated.

SOURCE WEATHER
---------------

Source weather comes from the current Open-Meteo weather endpoint.

Target weather comes from the hourly Open-Meteo forecast.

CONFIDENCE
----------

Confidence is empirically calibrated from historical backtesting:

    source AQI < 200     -> HIGH
    source AQI 200-299   -> MEDIUM
    source AQI >= 300    -> LOW

The validation showed substantially higher forecast error in the
200+ and especially 300+ source-AQI ranges.
"""

import os
import argparse
import json

import numpy as np
import pandas as pd

try:
    from xgboost import XGBRegressor
    HAVE_XGB = True
except ImportError:
    HAVE_XGB = False


# ============================================================================
# PROJECT PATHS
# ============================================================================

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.abspath(__file__)
    )
)

DATA_DIR = os.path.join(
    PROJECT_ROOT,
    "data"
)

MODELS_DIR = os.path.join(
    PROJECT_ROOT,
    "models"
)


# ============================================================================
# FILE PATHS
# ============================================================================

LIVE_HISTORY_PATH = os.path.join(
    DATA_DIR,
    "live_aqi_history.csv"
)

LIVE_CELL_AQI_PATH = os.path.join(
    DATA_DIR,
    "live_cell_aqi.csv"
)

TRAINING_DATASET_PATH = os.path.join(
    MODELS_DIR,
    "training_dataset.csv"
)

FORECAST_WEATHER_PATH = os.path.join(
    DATA_DIR,
    "forecast_weather.csv"
)

OUTPUT_FORECAST_PATH = os.path.join(
    DATA_DIR,
    "future_aqi_forecast.csv"
)


# ============================================================================
# FEATURES
# ============================================================================

FEATURE_COLS = [

    "aqi_lag_1h",
    "aqi_lag_6h",
    "aqi_lag_12h",
    "aqi_lag_24h",
    "aqi_lag_48h",

    "aqi_roll_mean_24h",
    "aqi_roll_mean_7d",
    "aqi_prev_day_max",

    "wind_speed",
    "wind_dir",
    "temp",
    "humidity",

    "target_wind_speed",
    "target_wind_dir",
    "target_temp",
    "target_humidity",

    "industrial_pct",
    "construction_pct",
    "green_cover_pct",
    "residential_pct",
    "water_pct",

    "nearest_dist_km",

    "is_estimated",

    "target_month",
    "target_hour",
    "target_weekday",

    "target_is_winter",
    "target_is_summer",
    "target_is_crop_burn",
    "target_is_festival",
]


# ============================================================================
# HORIZONS
# ============================================================================

HORIZONS = [
    24,
    48,
    72,
]


# ============================================================================
# CALENDAR
# ============================================================================

FESTIVAL_MONTHS_DAYS = {
    (10, 24),
    (10, 25),
    (11, 1),
    (11, 12),
    (11, 13),
}

WINTER_MONTHS = {
    11,
    12,
    1,
    2,
}

SUMMER_MONTHS = {
    4,
    5,
    6,
}

CROP_BURNING_MONTHS = {
    10,
    11,
}


# ============================================================================
# CONFIDENCE THRESHOLDS
# ============================================================================

# These thresholds were validated against historical forecast behavior.
#
# Historical validation:
#
# 24h:
#   HIGH   MAE ≈ 8.37
#   MEDIUM MAE ≈ 41.51
#   LOW    MAE ≈ 66.89
#
# 48h:
#   HIGH   MAE ≈ 8.60
#   MEDIUM MAE ≈ 34.12
#   LOW    MAE ≈ 64.66
#
# 72h:
#   HIGH   MAE ≈ 9.04
#   MEDIUM MAE ≈ 33.63
#   LOW    MAE ≈ 69.81

HIGH_CONFIDENCE_MAX_AQI = 200.0
MEDIUM_CONFIDENCE_MAX_AQI = 300.0


def classify_confidence(
    source_aqi: np.ndarray,
):
    """
    Classify forecast confidence using the current/source AQI.

    < 200       -> HIGH
    200-299.99  -> MEDIUM
    >= 300      -> LOW
    """

    source_aqi = np.asarray(
        source_aqi,
        dtype=float,
    )

    confidence = np.full(
        source_aqi.shape,
        "HIGH",
        dtype=object,
    )

    confidence[
        source_aqi >= HIGH_CONFIDENCE_MAX_AQI
    ] = "MEDIUM"

    confidence[
        source_aqi >= MEDIUM_CONFIDENCE_MAX_AQI
    ] = "LOW"

    return confidence


# ============================================================================
# LOAD LIVE HISTORY
# ============================================================================

def load_live_history():

    print(
        "\n[1/5] Loading continuous AQI history"
    )

    if not os.path.exists(
        LIVE_HISTORY_PATH
    ):
        raise FileNotFoundError(
            f"\nLive AQI history not found:\n"
            f"{LIVE_HISTORY_PATH}\n\n"
            f"Run:\n"
            f"python ml_pipeline/build_live_aqi_history.py"
        )

    print(
        f"  Loading live AQI history:\n"
        f"  {LIVE_HISTORY_PATH}"
    )

    df = pd.read_csv(
        LIVE_HISTORY_PATH,
        parse_dates=["timestamp"],
    )

    required = {
        "cell_id",
        "timestamp",
        "aqi",
    }

    missing = (
        required
        - set(df.columns)
    )

    if missing:
        raise RuntimeError(
            f"live_aqi_history.csv is missing "
            f"columns: {sorted(missing)}"
        )

    df["cell_id"] = pd.to_numeric(
        df["cell_id"],
        errors="coerce",
    )

    df["aqi"] = pd.to_numeric(
        df["aqi"],
        errors="coerce",
    )

    df = df[
        df["cell_id"].notna()
        & df["timestamp"].notna()
        & df["aqi"].notna()
    ].copy()

    df["cell_id"] = (
        df["cell_id"]
        .astype(int)
    )

    df["aqi"] = (
        df["aqi"]
        .clip(0, 500)
    )

    df = (
        df
        .drop_duplicates(
            subset=[
                "cell_id",
                "timestamp",
            ],
            keep="last",
        )
        .sort_values(
            [
                "cell_id",
                "timestamp",
            ]
        )
        .reset_index(drop=True)
    )

    print(
        f"  → {len(df):,} cell-hour rows | "
        f"{df['cell_id'].nunique():,} unique cells"
    )

    print(
        f"  → history: "
        f"{df['timestamp'].min()} → "
        f"{df['timestamp'].max()}"
    )

    if "source" in df.columns:

        print(
            "\n  Source distribution:"
        )

        print(
            df["source"]
            .value_counts()
            .to_string()
        )

    return df


# ============================================================================
# LOAD CURRENT LIVE AQI
# ============================================================================

def load_live_cell_aqi():

    print(
        "\n  Loading current live "
        "1600-cell AQI:"
    )

    if not os.path.exists(
        LIVE_CELL_AQI_PATH
    ):
        raise FileNotFoundError(
            f"Live AQI file not found:\n"
            f"{LIVE_CELL_AQI_PATH}\n\n"
            f"Run:\n"
            f"python ml_pipeline/update_live_cell_aqi.py"
        )

    df = pd.read_csv(
        LIVE_CELL_AQI_PATH
    )

    required = {
        "cell_id",
        "estimated_current_aqi",
        "live_reference_timestamp",
    }

    missing = (
        required
        - set(df.columns)
    )

    if missing:
        raise RuntimeError(
            f"live_cell_aqi.csv is missing "
            f"columns: {sorted(missing)}"
        )

    df = df[
        [
            "cell_id",
            "estimated_current_aqi",
            "live_reference_timestamp",
        ]
    ].copy()

    df["cell_id"] = pd.to_numeric(
        df["cell_id"],
        errors="coerce",
    )

    df["estimated_current_aqi"] = pd.to_numeric(
        df["estimated_current_aqi"],
        errors="coerce",
    )

    df["live_reference_timestamp"] = pd.to_datetime(
        df["live_reference_timestamp"],
        errors="coerce",
    )

    df = df[
        df["cell_id"].notna()
        & df["estimated_current_aqi"].notna()
        & df["live_reference_timestamp"].notna()
    ].copy()

    df["cell_id"] = (
        df["cell_id"]
        .astype(int)
    )

    df["estimated_current_aqi"] = (
        df["estimated_current_aqi"]
        .clip(0, 500)
    )

    df = df.drop_duplicates(
        subset=[
            "cell_id",
            "live_reference_timestamp",
        ],
        keep="last",
    )

    if df["cell_id"].nunique() != 1600:
        raise RuntimeError(
            "Expected 1600 live cells, "
            f"found {df['cell_id'].nunique()}."
        )

    if df[
        "live_reference_timestamp"
    ].nunique() != 1:
        raise RuntimeError(
            "Live AQI contains multiple "
            "reference timestamps."
        )

    print(
        f"  → {len(df):,} live cell rows"
    )

    print(
        f"  → {df['cell_id'].nunique():,} cells"
    )

    print(
        f"  → live timestamp: "
        f"{df['live_reference_timestamp'].iloc[0]}"
    )

    print(
        f"  → AQI range: "
        f"{df['estimated_current_aqi'].min():.1f} → "
        f"{df['estimated_current_aqi'].max():.1f}"
    )

    print(
        f"  → AQI mean: "
        f"{df['estimated_current_aqi'].mean():.1f}"
    )

    return df


# ============================================================================
# LOAD STATIC FEATURES
# ============================================================================

def load_static_features():

    print(
        "\n[2/5] Loading source-time "
        "static features"
    )

    if not os.path.exists(
        TRAINING_DATASET_PATH
    ):
        raise FileNotFoundError(
            f"Training dataset not found:\n"
            f"{TRAINING_DATASET_PATH}"
        )

    needed = [
        "cell_id",
        "timestamp",

        "industrial_pct",
        "construction_pct",
        "green_cover_pct",
        "residential_pct",
        "water_pct",

        "nearest_dist_km",

        "wind_speed",
        "wind_dir",
        "temp",
        "humidity",
    ]

    header = pd.read_csv(
        TRAINING_DATASET_PATH,
        nrows=0,
    ).columns.tolist()

    usecols = [
        c
        for c in needed
        if c in header
    ]

    df = pd.read_csv(
        TRAINING_DATASET_PATH,
        usecols=usecols,
        parse_dates=["timestamp"],
    )

    df = (
        df
        .drop_duplicates(
            subset=[
                "cell_id",
                "timestamp",
            ]
        )
        .reset_index(drop=True)
    )

    print(
        f"  → {len(df):,} rows loaded "
        f"from training_dataset.csv"
    )

    return df


# ============================================================================
# COMPUTE TEMPORAL FEATURES
# ============================================================================

def compute_lag_features(
    aqi_df: pd.DataFrame,
):

    print(
        "\n[3/5] Computing lag + "
        "rolling features"
    )

    print(
        "  Computing time-based lag "
        "features ..."
    )

    df = (
        aqi_df
        .drop_duplicates(
            subset=[
                "cell_id",
                "timestamp",
            ],
            keep="last",
        )
        .copy()
    )

    df = df.sort_values(
        [
            "cell_id",
            "timestamp",
        ]
    ).reset_index(drop=True)

    # ------------------------------------------------------------------
    # Exact timestamp lags
    # ------------------------------------------------------------------

    lookup = (
        df
        .set_index(
            [
                "cell_id",
                "timestamp",
            ]
        )["aqi"]
        .sort_index()
    )

    for hours in [
        1,
        6,
        12,
        24,
        48,
    ]:

        keys = pd.MultiIndex.from_arrays(
            [
                df["cell_id"],
                df["timestamp"]
                - pd.Timedelta(
                    hours=hours
                ),
            ]
        )

        df[
            f"aqi_lag_{hours}h"
        ] = (
            lookup
            .reindex(keys)
            .to_numpy(
                dtype="float32"
            )
        )

    # ------------------------------------------------------------------
    # Rolling features
    # ------------------------------------------------------------------

    roll_frames = []

    for cell_id, group in df.groupby(
        "cell_id"
    ):

        series = (
            group
            .set_index("timestamp")["aqi"]
            .sort_index()
        )

        full_index = pd.date_range(
            series.index.min(),
            series.index.max(),
            freq="h",
        )

        full_series = (
            series
            .reindex(full_index)
        )

        roll_frames.append(
            pd.DataFrame(
                {
                    "cell_id": cell_id,
                    "timestamp": full_index,

                    "aqi_roll_mean_24h":
                        full_series
                        .rolling(
                            24,
                            min_periods=6,
                        )
                        .mean()
                        .values,

                    "aqi_roll_mean_7d":
                        full_series
                        .rolling(
                            168,
                            min_periods=24,
                        )
                        .mean()
                        .values,

                    "aqi_prev_day_max":
                        full_series
                        .rolling(
                            24,
                            min_periods=6,
                        )
                        .max()
                        .shift(24)
                        .values,
                }
            )
        )

    roll_df = pd.concat(
        roll_frames,
        ignore_index=True,
    )

    roll_lookup = (
        roll_df
        .set_index(
            [
                "cell_id",
                "timestamp",
            ]
        )
        .sort_index()
    )

    row_keys = pd.MultiIndex.from_arrays(
        [
            df["cell_id"],
            df["timestamp"],
        ]
    )

    for column in [
        "aqi_roll_mean_24h",
        "aqi_roll_mean_7d",
        "aqi_prev_day_max",
    ]:

        df[column] = (
            roll_lookup[column]
            .reindex(row_keys)
            .to_numpy(
                dtype="float32"
            )
        )

    return df


# ============================================================================
# LIVE SOURCE STATE
# ============================================================================

def inject_live_source_state(
    history: pd.DataFrame,
    live: pd.DataFrame,
):

    live_ts = pd.Timestamp(
        live[
            "live_reference_timestamp"
        ].iloc[0]
    )

    source_ts = live_ts.floor(
        "h"
    )

    print(
        f"\nLive observation time : "
        f"{live_ts}"
    )

    print(
        f"Forecast source hour  : "
        f"{source_ts}"
    )

    live_state = live[
        [
            "cell_id",
            "estimated_current_aqi",
        ]
    ].copy()

    live_state = live_state.rename(
        columns={
            "estimated_current_aqi":
                "aqi",
        }
    )

    # ------------------------------------------------------------------
    # Remove any existing source-hour rows.
    #
    # We intentionally do not create intermediate 14:00...21:00 rows.
    # Only the live source state is injected.
    # ------------------------------------------------------------------

    history = history[
        history["timestamp"] != source_ts
    ].copy()

    source_rows = live_state.copy()

    source_rows["timestamp"] = (
        source_ts
    )

    source_rows["source"] = "live_source"

    history = pd.concat(
        [
            history,
            source_rows,
        ],
        ignore_index=True,
    )

    history = (
        history
        .drop_duplicates(
            subset=[
                "cell_id",
                "timestamp",
            ],
            keep="last",
        )
        .sort_values(
            [
                "cell_id",
                "timestamp",
            ]
        )
        .reset_index(drop=True)
    )

    print(
        "\n  Injecting live AQI into "
        "hourly source history ..."
    )

    print(
        f"  → {len(source_rows):,} "
        f"live source cells"
    )

    print(
        f"  → source AQI range: "
        f"{source_rows['aqi'].min():.1f} → "
        f"{source_rows['aqi'].max():.1f}"
    )

    print(
        f"  → source AQI mean: "
        f"{source_rows['aqi'].mean():.1f}"
    )

    return (
        history,
        source_ts,
        live_ts,
    )


# ============================================================================
# LIVE LAG BRIDGE
# ============================================================================

def bridge_live_lags(
    feature_df: pd.DataFrame,
    source_ts: pd.Timestamp,
    live: pd.DataFrame,
):

    """
    Bridge missing temporal features at the live source state.

    IMPORTANT:

    We do not fabricate intermediate timestamps.

    The live observation is the latest known AQI state.

    Where exact historical lags do not exist because the historical
    dataset ends at 13:00, inference uses the latest available real
    historical AQI value.

    This is an inference-time bridge only.
    """

    source_mask = (
        feature_df["timestamp"]
        == source_ts
    )

    source = feature_df[
        source_mask
    ].copy()

    source["aqi_lag_1h"] = source[
        "aqi_lag_1h"
    ].astype(float)

    source["aqi_lag_6h"] = source[
        "aqi_lag_6h"
    ].astype(float)

    if len(source) != 1600:
        raise RuntimeError(
            f"Expected 1600 source rows "
            f"at {source_ts}, found "
            f"{len(source)}."
        )

    # ------------------------------------------------------------------
    # Exact 1h lag
    # ------------------------------------------------------------------

    missing_1h = source[
        "aqi_lag_1h"
    ].isna()

    # ------------------------------------------------------------------
    # Exact 6h lag
    # ------------------------------------------------------------------

    missing_6h = source[
        "aqi_lag_6h"
    ].isna()

    # ------------------------------------------------------------------
    # Latest real historical AQI
    #
    # We deliberately do NOT create 14:00, 15:00 ... 21:00 rows.
    # ------------------------------------------------------------------

    historical_only = feature_df[
        feature_df["timestamp"] < source_ts
    ].copy()

    latest_real_ts = (
        historical_only["timestamp"]
        .max()
    )

    latest_real = historical_only[
        historical_only["timestamp"]
        == latest_real_ts
    ][
        [
            "cell_id",
            "aqi",
        ]
    ].copy()

    latest_real = latest_real.rename(
        columns={
            "aqi":
                "_latest_real_aqi",
        }
    )

    source = source.merge(
        latest_real,
        on="cell_id",
        how="left",
    )

    # ------------------------------------------------------------------
    # Bridge missing short-term lags.
    #
    # The live observation represents the latest observed state.
    # We use it as the live-state bridge rather than inventing missing
    # intermediate hourly observations.
    # ------------------------------------------------------------------

    live_lookup = live[
        [
            "cell_id",
            "estimated_current_aqi",
        ]
    ].copy()

    live_lookup = live_lookup.rename(
        columns={
            "estimated_current_aqi":
                "_live_aqi",
        }
    )

    source = source.merge(
        live_lookup,
        on="cell_id",
        how="left",
    )

    source.loc[
        source["aqi_lag_1h"].isna(),
        "aqi_lag_1h",
    ] = source.loc[
        source["aqi_lag_1h"].isna(),
        "_live_aqi",
    ]

    source.loc[
        source["aqi_lag_6h"].isna(),
        "aqi_lag_6h",
    ] = source.loc[
        source["aqi_lag_6h"].isna(),
        "_latest_real_aqi",
    ]

    print(
        "\n  [live bridge]"
    )

    print(
        f"    live observation = "
        f"{live['live_reference_timestamp'].iloc[0]}"
    )

    print(
        f"    model source hour = "
        f"{source_ts}"
    )

    print(
        f"    1h lag bridged from "
        f"live state: "
        f"{int(missing_1h.sum())} cells"
    )

    print(
        f"    6h lag bridged from "
        f"latest real historical AQI "
        f"({latest_real_ts}): "
        f"{int(missing_6h.sum())} cells"
    )

    # ------------------------------------------------------------------
    # Put source rows back into feature dataframe.
    # ------------------------------------------------------------------

    helper_cols = [
        "_latest_real_aqi",
        "_live_aqi",
    ]

    source = source.drop(
        columns=[
            c
            for c in helper_cols
            if c in source.columns
        ]
    )

    feature_df = feature_df[
        ~source_mask
    ].copy()

    feature_df = pd.concat(
        [
            feature_df,
            source,
        ],
        ignore_index=True,
    )

    feature_df = (
        feature_df
        .sort_values(
            [
                "cell_id",
                "timestamp",
            ]
        )
        .reset_index(drop=True)
    )

    return feature_df


# ============================================================================
# LOAD FORECAST WEATHER
# ============================================================================

def load_forecast_weather():

    print(
        "\n[4/5] Loading forecast weather"
    )

    if not os.path.exists(
        FORECAST_WEATHER_PATH
    ):
        raise FileNotFoundError(
            f"Forecast weather not found:\n"
            f"{FORECAST_WEATHER_PATH}\n\n"
            f"Run:\n"
            f"python ml_pipeline/fetch_forecast_weather.py"
        )

    df = pd.read_csv(
        FORECAST_WEATHER_PATH,
        parse_dates=["timestamp"],
    )

    required = {
        "timestamp",
        "target_temp",
        "target_humidity",
        "target_wind_speed",
        "target_wind_dir",
    }

    missing = (
        required
        - set(df.columns)
    )

    if missing:
        raise RuntimeError(
            f"Forecast weather missing "
            f"columns: {sorted(missing)}"
        )

    df = (
        df
        .drop_duplicates(
            subset=["timestamp"],
            keep="last",
        )
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    print(
        f"  → forecast weather: "
        f"{len(df)} hours "
        f"({df['timestamp'].min()} → "
        f"{df['timestamp'].max()})"
    )

    return df


# ============================================================================
# TARGET WEATHER
# ============================================================================

def get_target_weather(
    forecast_weather: pd.DataFrame,
    target_ts: pd.Timestamp,
):

    fw = (
        forecast_weather
        .set_index("timestamp")
        .sort_index()
    )

    target_ts = pd.Timestamp(
        target_ts
    )

    if target_ts not in fw.index:

        raise RuntimeError(
            f"\nNo forecast weather for "
            f"target timestamp {target_ts}.\n"
            f"Available range: "
            f"{fw.index.min()} → "
            f"{fw.index.max()}"
        )

    row = fw.loc[target_ts]

    return {
        "target_wind_speed":
            float(
                row["target_wind_speed"]
            ),

        "target_wind_dir":
            float(
                row["target_wind_dir"]
            ),

        "target_temp":
            float(
                row["target_temp"]
            ),

        "target_humidity":
            float(
                row["target_humidity"]
            ),
    }


# ============================================================================
# TARGET CALENDAR
# ============================================================================

def target_calendar(
    target_ts: pd.Timestamp,
):

    return {

        "target_month":
            target_ts.month,

        "target_hour":
            target_ts.hour,

        "target_weekday":
            target_ts.dayofweek,

        "target_is_winter":
            int(
                target_ts.month
                in WINTER_MONTHS
            ),

        "target_is_summer":
            int(
                target_ts.month
                in SUMMER_MONTHS
            ),

        "target_is_crop_burn":
            int(
                target_ts.month
                in CROP_BURNING_MONTHS
            ),

        "target_is_festival":
            int(
                (
                    target_ts.month,
                    target_ts.day,
                )
                in FESTIVAL_MONTHS_DAYS
            ),
    }


# ============================================================================
# LOAD CURRENT WEATHER
# ============================================================================

def load_current_weather():

    print(
        "\n  Loading current Open-Meteo weather"
    )

    try:

        from fetch_forecast_weather import (
            get_current_weather
        )

    except ImportError:

        from ml_pipeline.fetch_forecast_weather import (
            get_current_weather
        )

    weather = get_current_weather()

    print(
        f"    timestamp : "
        f"{weather['timestamp']}"
    )

    print(
        f"    temp      : "
        f"{weather['temp']:.1f} °C"
    )

    print(
        f"    humidity  : "
        f"{weather['humidity']:.0f} %"
    )

    print(
        f"    wind      : "
        f"{weather['wind_speed']:.1f} km/h"
    )

    print(
        f"    direction : "
        f"{weather['wind_dir']:.0f}°"
    )

    return weather


# ============================================================================
# BUILD INFERENCE MATRIX
# ============================================================================

def build_inference_matrix(
    feature_df: pd.DataFrame,
    source_ts: pd.Timestamp,
    target_ts: pd.Timestamp,
    forecast_weather: pd.DataFrame,
    static_df: pd.DataFrame,
    current_weather: dict,
):

    source = feature_df[
        feature_df["timestamp"]
        == source_ts
    ].copy()

    if len(source) != 1600:
        raise RuntimeError(
            f"Expected 1600 source rows "
            f"at {source_ts}, found "
            f"{len(source)}."
        )

    # ------------------------------------------------------------------
    # Static features
    #
    # Land-use features are intentionally empty in training data.
    # They are NOT filled with zero.
    # ------------------------------------------------------------------

    static_columns = [
        "cell_id",
        "industrial_pct",
        "construction_pct",
        "green_cover_pct",
        "residential_pct",
        "water_pct",
        "nearest_dist_km",
    ]

    available_static_columns = [
        c
        for c in static_columns
        if c in static_df.columns
    ]

    static_latest = (
        static_df
        .sort_values("timestamp")
        .groupby(
            "cell_id",
            as_index=False,
        )
        .tail(1)
    )

    static_latest = static_latest[
        available_static_columns
    ].copy()

    source = source.merge(
        static_latest,
        on="cell_id",
        how="left",
        suffixes=(
            "",
            "_static",
        ),
    )

    # ------------------------------------------------------------------
    # CURRENT SOURCE WEATHER
    # ------------------------------------------------------------------

    source["wind_speed"] = (
        current_weather["wind_speed"]
    )

    source["wind_dir"] = (
        current_weather["wind_dir"]
    )

    source["temp"] = (
        current_weather["temp"]
    )

    source["humidity"] = (
        current_weather["humidity"]
    )

    # ------------------------------------------------------------------
    # TARGET WEATHER
    # ------------------------------------------------------------------

    target_weather = get_target_weather(
        forecast_weather,
        target_ts,
    )

    for column, value in target_weather.items():

        source[column] = value

    # ------------------------------------------------------------------
    # TARGET CALENDAR
    # ------------------------------------------------------------------

    calendar = target_calendar(
        target_ts
    )

    for column, value in calendar.items():

        source[column] = value

    # ------------------------------------------------------------------
    # IS ESTIMATED
    # ------------------------------------------------------------------

    if "is_estimated" not in source.columns:

        source["is_estimated"] = 1

    # The live source state itself is an estimated/spatial AQI.
    source["is_estimated"] = 1

    # ------------------------------------------------------------------
    # Ensure all model features exist.
    # ------------------------------------------------------------------

    for column in FEATURE_COLS:

        if column not in source.columns:

            source[column] = np.nan

    return source


# ============================================================================
# LOAD MODEL
# ============================================================================

def load_forecaster(
    horizon: int
):

    if not HAVE_XGB:

        raise RuntimeError(
            "XGBoost is not installed.\n"
            "Install with:\n"
            "pip install xgboost"
        )

    path = os.path.join(
        MODELS_DIR,
        f"forecaster_{horizon}h.json"
    )

    if not os.path.exists(path):

        raise FileNotFoundError(
            f"Forecaster model not found:\n"
            f"{path}"
        )

    model = XGBRegressor()

    model.load_model(
        path
    )

    return model


# ============================================================================
# ENSEMBLE
# ============================================================================

def load_ensemble_config():

    path = os.path.join(
        MODELS_DIR,
        "ensemble_config.json"
    )

    if not os.path.exists(path):

        raise FileNotFoundError(
            f"Ensemble configuration not found:\n"
            f"{path}"
        )

    with open(
        path,
        "r",
    ) as file:

        config = json.load(file)

    if "alpha" not in config:

        raise ValueError(
            "ensemble_config.json is missing "
            "'alpha'."
        )

    alpha_config = config["alpha"]

    result = {}

    for horizon in HORIZONS:

        key = str(horizon)

        if key not in alpha_config:

            raise ValueError(
                f"Missing alpha for "
                f"{horizon}h."
            )

        alpha = float(
            alpha_config[key]
        )

        if not 0 <= alpha <= 1:

            raise ValueError(
                f"Invalid alpha for "
                f"{horizon}h: {alpha}"
            )

        result[horizon] = alpha

    return result


# ============================================================================
# MODEL PREDICTION
# ============================================================================

def predict_ml(
    model,
    matrix: pd.DataFrame,
):

    X = matrix[
        FEATURE_COLS
    ].copy()

    # Make all values numeric.
    for column in X.columns:

        X[column] = pd.to_numeric(
            X[column],
            errors="coerce",
        )

    prediction = model.predict(
        X
    )

    prediction = np.asarray(
        prediction,
        dtype=float,
    )

    prediction = np.nan_to_num(
        prediction,
        nan=0.0,
        posinf=500.0,
        neginf=0.0,
    )

    prediction = np.clip(
        prediction,
        0,
        500,
    )

    return prediction


# ============================================================================
# MAIN
# ============================================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "AirGrid live 24h/48h/72h "
            "AQI forecaster"
        )
    )

    args = parser.parse_args()

    print(
        "\n" + "=" * 70
    )

    print(
        " AirGrid — Live AQI Forecaster"
    )

    print(
        f" Horizons: {HORIZONS}"
    )

    print(
        "=" * 70
    )

    # ------------------------------------------------------------------
    # ENSEMBLE
    # ------------------------------------------------------------------

    ensemble_alpha = (
        load_ensemble_config()
    )

    print(
        "\n[Ensemble configuration]"
    )

    for horizon in HORIZONS:

        print(
            f"  {horizon}h alpha = "
            f"{ensemble_alpha[horizon]:.2f}"
        )

    # ------------------------------------------------------------------
    # CONFIDENCE
    # ------------------------------------------------------------------

    print(
        "\n[Confidence configuration]"
    )

    print(
        f"  HIGH   : source AQI < "
        f"{HIGH_CONFIDENCE_MAX_AQI:.0f}"
    )

    print(
        f"  MEDIUM : source AQI >= "
        f"{HIGH_CONFIDENCE_MAX_AQI:.0f} and < "
        f"{MEDIUM_CONFIDENCE_MAX_AQI:.0f}"
    )

    print(
        f"  LOW    : source AQI >= "
        f"{MEDIUM_CONFIDENCE_MAX_AQI:.0f}"
    )

    # ------------------------------------------------------------------
    # LOAD HISTORY
    # ------------------------------------------------------------------

    history = load_live_history()

    # ------------------------------------------------------------------
    # LOAD CURRENT LIVE AQI
    # ------------------------------------------------------------------

    live = load_live_cell_aqi()

    live_ts = pd.Timestamp(
        live[
            "live_reference_timestamp"
        ].iloc[0]
    )

    source_ts = live_ts.floor(
        "h"
    )

    # ------------------------------------------------------------------
    # INJECT LIVE SOURCE STATE
    # ------------------------------------------------------------------

    history, source_ts, live_ts = (
        inject_live_source_state(
            history,
            live,
        )
    )

    # ------------------------------------------------------------------
    # STATIC FEATURES
    # ------------------------------------------------------------------

    static_df = (
        load_static_features()
    )

    # ------------------------------------------------------------------
    # TEMPORAL FEATURES
    # ------------------------------------------------------------------

    feature_df = (
        compute_lag_features(
            history
        )
    )

    # ------------------------------------------------------------------
    # LIVE GAP BRIDGE
    # ------------------------------------------------------------------

    feature_df = (
        bridge_live_lags(
            feature_df,
            source_ts,
            live,
        )
    )

    # ------------------------------------------------------------------
    # FORECAST WEATHER
    # ------------------------------------------------------------------

    forecast_weather = (
        load_forecast_weather()
    )

    # ------------------------------------------------------------------
    # CURRENT WEATHER
    # ------------------------------------------------------------------

    current_weather = (
        load_current_weather()
    )

    # ------------------------------------------------------------------
    # TARGET RANGE CHECK
    # ------------------------------------------------------------------

    forecast_timestamps = set(
        forecast_weather[
            "timestamp"
        ]
    )

    for horizon in HORIZONS:

        target_ts = (
            source_ts
            + pd.Timedelta(
                hours=horizon
            )
        )

        if target_ts not in forecast_timestamps:

            raise RuntimeError(
                f"Target timestamp "
                f"{target_ts} is not "
                f"available in forecast weather."
            )

    print(
        f"\nSource timestamp: "
        f"{source_ts}"
    )

    print(
        "  ✓ All target timestamps "
        "are within the forecast window."
    )

    # ------------------------------------------------------------------
    # FORECAST
    # ------------------------------------------------------------------

    print(
        "\n[5/5] Forecasting per horizon"
    )

    all_results = []

    for horizon in HORIZONS:

        target_ts = (
            source_ts
            + pd.Timedelta(
                hours=horizon
            )
        )

        print(
            f"\n  [{horizon}h] "
            f"source={source_ts} "
            f"→ target={target_ts}"
        )

        matrix = (
            build_inference_matrix(
                feature_df=feature_df,
                source_ts=source_ts,
                target_ts=target_ts,
                forecast_weather=forecast_weather,
                static_df=static_df,
                current_weather=current_weather,
            )
        )

        # --------------------------------------------------------------
        # Missing feature diagnostics
        # --------------------------------------------------------------

        missing_counts = (
            matrix[
                FEATURE_COLS
            ]
            .isna()
            .sum()
        )

        missing_counts = (
            missing_counts[
                missing_counts > 0
            ]
        )

        if not missing_counts.empty:

            print(
                "\n    [note] Missing "
                "inference features:"
            )

            print(
                missing_counts
                .to_string()
            )

            print(
                "    XGBoost will handle "
                "missing values."
            )

        # --------------------------------------------------------------
        # MODEL
        # --------------------------------------------------------------

        model = load_forecaster(
            horizon
        )

        ml_prediction = predict_ml(
            model,
            matrix,
        )

        # --------------------------------------------------------------
        # PERSISTENCE
        # --------------------------------------------------------------

        persistence = (
            matrix["aqi"]
            .to_numpy(
                dtype=float
            )
        )

        persistence = np.clip(
            persistence,
            0,
            500,
        )

        # --------------------------------------------------------------
        # ENSEMBLE
        # --------------------------------------------------------------

        alpha = (
            ensemble_alpha[horizon]
        )

        final_prediction = (
            alpha
            * ml_prediction
            +
            (1 - alpha)
            * persistence
        )

        final_prediction = np.clip(
            final_prediction,
            0,
            500,
        )

        # --------------------------------------------------------------
        # FORECAST CHANGE
        # --------------------------------------------------------------

        forecast_change = (
            final_prediction
            - persistence
        )

        # Relative change is mainly a UI/explanation metric.
        #
        # The denominator is protected against zero so a source AQI
        # of zero does not produce infinity.

        relative_change_pct = (
            forecast_change
            /
            np.maximum(
                np.abs(persistence),
                1.0,
            )
        ) * 100.0

        # --------------------------------------------------------------
        # CONFIDENCE
        # --------------------------------------------------------------

        forecast_confidence = (
            classify_confidence(
                persistence
            )
        )

        # --------------------------------------------------------------
        # RESULT
        # --------------------------------------------------------------

        result = pd.DataFrame(
            {
                "cell_id":
                    matrix["cell_id"]
                    .astype(int),

                "source_timestamp":
                    source_ts,

                "live_observation_timestamp":
                    live_ts,

                "target_timestamp":
                    target_ts,

                "horizon_hours":
                    horizon,

                "ml_predicted_aqi":
                    ml_prediction,

                "persistence_aqi":
                    persistence,

                "alpha":
                    alpha,

                "predicted_aqi":
                    final_prediction,

                "forecast_change":
                    forecast_change,

                "relative_change_pct":
                    relative_change_pct,

                "forecast_confidence":
                    forecast_confidence,
            }
        )

        all_results.append(
            result
        )

        # --------------------------------------------------------------
        # TERMINAL DIAGNOSTICS
        # --------------------------------------------------------------

        confidence_counts = (
            pd.Series(
                forecast_confidence
            )
            .value_counts()
            .reindex(
                [
                    "HIGH",
                    "MEDIUM",
                    "LOW",
                ],
                fill_value=0,
            )
        )

        print(
            f"    → "
            f"{len(result):,} cells"
        )

        print(
            f"    → alpha = "
            f"{alpha:.2f}"
        )

        print(
            f"    → ML AQI: "
            f"{ml_prediction.min():.0f}–"
            f"{ml_prediction.max():.0f} "
            f"(mean "
            f"{ml_prediction.mean():.1f})"
        )

        print(
            f"    → Persistence AQI: "
            f"{persistence.min():.0f}–"
            f"{persistence.max():.0f} "
            f"(mean "
            f"{persistence.mean():.1f})"
        )

        print(
            f"    → Final ensemble AQI: "
            f"{final_prediction.min():.0f}–"
            f"{final_prediction.max():.0f} "
            f"(mean "
            f"{final_prediction.mean():.1f})"
        )

        print(
            f"    → Mean forecast change: "
            f"{forecast_change.mean():+.1f} AQI"
        )

        print(
            f"    → Confidence: "
            f"HIGH={int(confidence_counts['HIGH']):,}, "
            f"MEDIUM={int(confidence_counts['MEDIUM']):,}, "
            f"LOW={int(confidence_counts['LOW']):,}"
        )

        print(
            f"    → Valid forecasts: "
            f"{np.isfinite(final_prediction).sum():,}/"
            f"{len(final_prediction):,}"
        )

    # ------------------------------------------------------------------
    # SAVE
    # ------------------------------------------------------------------

    output = pd.concat(
        all_results,
        ignore_index=True,
    )

    output.to_csv(
        OUTPUT_FORECAST_PATH,
        index=False,
    )

    # ------------------------------------------------------------------
    # FINAL SUMMARY
    # ------------------------------------------------------------------

    print(
        "\n" + "=" * 70
    )

    print(
        f" ✓ Saved: "
        f"{OUTPUT_FORECAST_PATH}"
    )

    print(
        f" Rows: "
        f"{len(output):,}"
    )

    print(
        f" Source timestamp: "
        f"{source_ts}"
    )

    print(
        f" Live observation: "
        f"{live_ts}"
    )

    print(
        f" Unique cells: "
        f"{output['cell_id'].nunique():,}"
    )

    print(
        "\n Summary by horizon:"
    )

    for horizon in HORIZONS:

        subset = output[
            output["horizon_hours"]
            == horizon
        ]

        confidence_counts = (
            subset[
                "forecast_confidence"
            ]
            .value_counts()
            .reindex(
                [
                    "HIGH",
                    "MEDIUM",
                    "LOW",
                ],
                fill_value=0,
            )
        )

        print(
            f"   +{horizon}h: "
            f"{len(subset):,} cells | "
            f"alpha={ensemble_alpha[horizon]:.2f} | "
            f"ML mean="
            f"{subset['ml_predicted_aqi'].mean():.1f} | "
            f"Persistence mean="
            f"{subset['persistence_aqi'].mean():.1f} | "
            f"Final mean="
            f"{subset['predicted_aqi'].mean():.1f} | "
            f"Confidence="
            f"H:{int(confidence_counts['HIGH']):,} "
            f"M:{int(confidence_counts['MEDIUM']):,} "
            f"L:{int(confidence_counts['LOW']):,}"
        )

    print(
        "\n Ensemble methodology:"
    )

    print(
        "   Final AQI = alpha × ML prediction "
        "+ (1 − alpha) × persistence AQI"
    )

    print(
        "   Alpha values are loaded from "
        "ensemble_config.json."
    )

    print(
        "   Alpha is an ensemble hyperparameter, "
        "not part of the AQI calculation standard."
    )

    print(
        "\n Confidence methodology:"
    )

    print(
        "   HIGH   = source AQI < 200"
    )

    print(
        "   MEDIUM = source AQI 200–299"
    )

    print(
        "   LOW    = source AQI >= 300"
    )

    print(
        "   Confidence is based on historically "
        "validated forecast-error behavior."
    )

    print(
        "\n Live-gap methodology:"
    )

    print(
        "   Exact historical timestamps are "
        "used whenever available."
    )

    print(
        "   No intermediate AQI timestamps "
        "are fabricated."
    )

    print(
        "   Live AQI is used as the current "
        "source-state bridge during inference."
    )

    print(
        "   Persistence uses the latest "
        "1600-cell live AQI snapshot."
    )

    print(
        "\n Production weather:"
    )

    print(
        "   Source weather = current "
        "Open-Meteo weather."
    )

    print(
        "   Target weather = Open-Meteo "
        "hourly forecast."
    )

    print(
        "   Weather uncertainty increases "
        "at longer horizons."
    )

    print(
        "\n Land-use features:"
    )

    print(
        "   industrial_pct, construction_pct,"
    )

    print(
        "   green_cover_pct, residential_pct,"
    )

    print(
        "   water_pct remain NaN because "
        "they are intentionally empty "
        "in the training data."
    )

    print(
        "\n" + "=" * 70
    )


if __name__ == "__main__":
    main()
