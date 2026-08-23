"""
update_live_cell_aqi.py

AirGrid — Live station AQI -> 1600-cell current AQI

Purpose
-------
Takes the latest live OpenAQ station AQI observations and produces
a current AQI estimate for all 1600 AirGrid cells.

Spatial estimation strategy
---------------------------
1. Fresh station cells:
       current AQI = actual live station AQI

2. Non-station cells:
       current AQI =
           XGBoost spatial estimate
           +
           multi-station IDW estimate

The XGBoost model remains the primary spatial model.

IDW prevents large regions of cells from receiving exactly the
same AQI merely because they share one nearest station.

IMPORTANT
---------
This does NOT retrain the spatial estimator.

It loads:
    data/live_station_data.csv
    data/training_dataset.csv
    models/spatial_estimator.json

The trained spatial model expects exactly 28 features.
"""

import os
import argparse
import numpy as np
import pandas as pd
from xgboost import XGBRegressor


# ============================================================
# PATHS
# ============================================================

ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

DATA_DIR = os.path.join(
    ROOT,
    "data"
)

MODELS_DIR = os.path.join(
    ROOT,
    "models"
)

LIVE_PATH = os.path.join(
    DATA_DIR,
    "live_station_data.csv"
)

TRAINING_PATH = os.path.join(
    DATA_DIR,
    "training_dataset.csv"
)

MODEL_PATH = os.path.join(
    MODELS_DIR,
    "spatial_estimator.json"
)

OUTPUT_PATH = os.path.join(
    DATA_DIR,
    "live_cell_aqi.csv"
)


# ============================================================
# EXACT MODEL FEATURES
# ============================================================

FEATURES = [
    "wind_speed",
    "wind_dir",
    "temp",
    "humidity",
    "no2_satellite",
    "aod_satellite",
    "industrial_pct",
    "construction_pct",
    "green_cover_pct",
    "residential_pct",
    "water_pct",
    "month",
    "hour",
    "weekday",
    "is_winter",
    "is_summer",
    "is_crop_burn",
    "is_festival",
    "proxy_dist_km",
    "proxy_true_aqi",
    "proxy_pm25",
    "proxy_pm10",
    "proxy_no2",
    "proxy_so2",
    "proxy_o3",
    "proxy_co",
]


# ============================================================
# SETTINGS
# ============================================================

# A station observation older than this is not considered
# sufficiently fresh for live spatial estimation.
DEFAULT_MAX_AGE_MINUTES = 120

# Maximum number of cells expected.
EXPECTED_CELLS = 1600

# ------------------------------------------------------------
# Multi-station IDW configuration
# ------------------------------------------------------------

# Weight = 1 / distance^IDW_POWER
IDW_POWER = 2.0

# Maximum number of nearest fresh stations used by IDW.
IDW_MAX_STATIONS = 8

# Initial spatial blend.
#
# IMPORTANT:
# These are initial engineering weights.
# They should eventually be calibrated using historical
# spatial backtesting.
SPATIAL_XGB_WEIGHT = 0.60
SPATIAL_IDW_WEIGHT = 0.40


# ============================================================
# LOAD XGBOOST MODEL
# ============================================================

def load_spatial_model():

    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(
            f"Spatial model not found:\n{MODEL_PATH}\n\n"
            "Run train_spatial_estimator.py first."
        )

    print("Loading spatial estimator:")
    print(f"  {MODEL_PATH}")

    model = XGBRegressor()

    model.load_model(MODEL_PATH)

    return model


# ============================================================
# LOAD LIVE STATION DATA
# ============================================================

def load_live_data():

    if not os.path.exists(LIVE_PATH):
        raise FileNotFoundError(
            f"Live station file not found:\n{LIVE_PATH}\n\n"
            "Run fetch_live_station_data.py first."
        )

    df = pd.read_csv(
        LIVE_PATH,
        parse_dates=["timestamp"]
    )

    required = [
        "station",
        "latitude",
        "longitude",
        "timestamp",
        "pm25",
        "pm10",
        "no2",
        "so2",
        "o3",
        "co",
        "true_aqi",
    ]

    missing = [
        c for c in required
        if c not in df.columns
    ]

    if missing:
        raise RuntimeError(
            f"live_station_data.csv missing columns: {missing}"
        )

    df["true_aqi"] = pd.to_numeric(
        df["true_aqi"],
        errors="coerce"
    )

    df = df.dropna(
        subset=[
            "station",
            "latitude",
            "longitude",
            "timestamp",
            "true_aqi",
        ]
    ).copy()

    df = df[
        (df["true_aqi"] >= 0)
        & (df["true_aqi"] <= 500)
    ].copy()

    if df.empty:
        raise RuntimeError(
            "No valid live station AQI observations found."
        )

    return df


# ============================================================
# LOAD GRID / TRAINING DATA
# ============================================================

def load_training_grid():

    if not os.path.exists(TRAINING_PATH):
        raise FileNotFoundError(
            f"Training dataset not found:\n{TRAINING_PATH}"
        )

    # Only read columns required for live inference.
    needed = [
        "cell_id",
        "timestamp",
        "lat",
        "lon",
        "wind_speed",
        "wind_dir",
        "temp",
        "humidity",
        "no2_satellite",
        "aod_satellite",
        "industrial_pct",
        "construction_pct",
        "green_cover_pct",
        "residential_pct",
        "water_pct",
        "month",
        "hour",
        "weekday",
        "is_winter",
        "is_summer",
        "is_crop_burn",
        "is_festival",
        "nearest_station",
        "nearest_dist_km",
    ]

    header = pd.read_csv(
        TRAINING_PATH,
        nrows=0
    ).columns.tolist()

    usecols = [
        c for c in needed
        if c in header
    ]

    df = pd.read_csv(
        TRAINING_PATH,
        usecols=usecols,
        parse_dates=["timestamp"]
    )

    # --------------------------------------------------------
    # Get the latest complete 1600-cell snapshot.
    # --------------------------------------------------------

    coverage = (
        df.groupby("timestamp")["cell_id"]
        .nunique()
        .sort_index()
    )

    valid = coverage[
        coverage >= EXPECTED_CELLS
    ]

    if valid.empty:
        raise RuntimeError(
            "Could not find a complete 1600-cell grid snapshot."
        )

    latest_ts = valid.index.max()

    grid = df[
        df["timestamp"] == latest_ts
    ].copy()

    grid = grid.drop_duplicates(
        subset=["cell_id"],
        keep="first"
    )

    print(
        f"Training/grid snapshot: {latest_ts}"
    )

    print(
        f"Grid cells: {grid['cell_id'].nunique()}"
    )

    if grid["cell_id"].nunique() != EXPECTED_CELLS:
        raise RuntimeError(
            f"Expected {EXPECTED_CELLS} cells but found "
            f"{grid['cell_id'].nunique()}."
        )

    return grid


# ============================================================
# HAVERSINE DISTANCE
# ============================================================

def haversine_km(
    lat1,
    lon1,
    lat2,
    lon2
):
    """
    Vectorized Haversine distance in kilometres.
    """

    lat1 = np.radians(lat1)
    lat2 = np.radians(lat2)

    dlat = lat2 - lat1

    dlon = np.radians(
        lon2 - lon1
    )

    a = (
        np.sin(dlat / 2.0) ** 2
        +
        np.cos(lat1)
        * np.cos(lat2)
        * np.sin(dlon / 2.0) ** 2
    )

    return 6371.0 * 2.0 * np.arcsin(
        np.sqrt(a)
    )


# ============================================================
# SELECT FRESH LIVE STATIONS
# ============================================================

def select_fresh_stations(
    live,
    max_age_minutes
):

    latest_live_ts = live["timestamp"].max()

    live = live.copy()

    live["age_minutes"] = (
        latest_live_ts - live["timestamp"]
    ).dt.total_seconds() / 60.0

    fresh = live[
        live["age_minutes"] <= max_age_minutes
    ].copy()

    # One latest observation per station.
    fresh = (
        fresh
        .sort_values(
            ["station", "timestamp"]
        )
        .drop_duplicates(
            subset=["station"],
            keep="last"
        )
    )

    print(
        f"Live reference time: {latest_live_ts}"
    )

    print(
        f"Fresh stations <= {max_age_minutes} min: "
        f"{fresh['station'].nunique()}"
    )

    if fresh.empty:
        raise RuntimeError(
            "No sufficiently fresh stations available."
        )

    return fresh, latest_live_ts


# ============================================================
# BUILD PROXY FOR EACH CELL
# ============================================================

def attach_live_proxies(
    grid,
    live
):
    """
    Preserve the existing XGBoost feature design.

    For each cell, the closest fresh station is used for the
    proxy features expected by the trained spatial model.

    IMPORTANT:
    This nearest station is ONLY used to construct the model's
    proxy features.

    It is NOT used to assign the final AQI to the cell.

    The final AQI for non-station cells is calculated later
    using XGBoost + multi-station IDW.
    """

    grid = grid.copy()

    station_lat = live[
        "latitude"
    ].to_numpy(
        dtype=float
    )

    station_lon = live[
        "longitude"
    ].to_numpy(
        dtype=float
    )

    station_aqi = live[
        "true_aqi"
    ].to_numpy(
        dtype=float
    )

    station_pm25 = pd.to_numeric(
        live["pm25"],
        errors="coerce"
    ).to_numpy(
        dtype=float
    )

    station_pm10 = pd.to_numeric(
        live["pm10"],
        errors="coerce"
    ).to_numpy(
        dtype=float
    )

    station_no2 = pd.to_numeric(
        live["no2"],
        errors="coerce"
    ).to_numpy(
        dtype=float
    )

    station_so2 = pd.to_numeric(
        live["so2"],
        errors="coerce"
    ).to_numpy(
        dtype=float
    )

    station_o3 = pd.to_numeric(
        live["o3"],
        errors="coerce"
    ).to_numpy(
        dtype=float
    )

    station_co = pd.to_numeric(
        live["co"],
        errors="coerce"
    ).to_numpy(
        dtype=float
    )

    proxy_dist = []
    proxy_aqi = []
    proxy_pm25 = []
    proxy_pm10 = []
    proxy_no2 = []
    proxy_so2 = []
    proxy_o3 = []
    proxy_co = []

    for _, cell in grid.iterrows():

        distances = haversine_km(
            cell["lat"],
            cell["lon"],
            station_lat,
            station_lon
        )

        idx = int(
            np.nanargmin(distances)
        )

        proxy_dist.append(
            float(distances[idx])
        )

        proxy_aqi.append(
            float(station_aqi[idx])
        )

        proxy_pm25.append(
            station_pm25[idx]
        )

        proxy_pm10.append(
            station_pm10[idx]
        )

        proxy_no2.append(
            station_no2[idx]
        )

        proxy_so2.append(
            station_so2[idx]
        )

        proxy_o3.append(
            station_o3[idx]
        )

        proxy_co.append(
            station_co[idx]
        )

    grid["proxy_dist_km"] = proxy_dist
    grid["proxy_true_aqi"] = proxy_aqi
    grid["proxy_pm25"] = proxy_pm25
    grid["proxy_pm10"] = proxy_pm10
    grid["proxy_no2"] = proxy_no2
    grid["proxy_so2"] = proxy_so2
    grid["proxy_o3"] = proxy_o3
    grid["proxy_co"] = proxy_co

    return grid


# ============================================================
# MULTI-STATION IDW
# ============================================================

def compute_idw_aqi(
    grid,
    fresh,
    power=IDW_POWER,
    max_stations=IDW_MAX_STATIONS
):
    """
    Estimate AQI for every grid cell using multiple fresh stations.

    Weight:
        w_i = 1 / distance_i^power

    Final IDW:
        AQI = sum(w_i * AQI_i) / sum(w_i)

    Only the nearest `max_stations` valid fresh stations are used.

    This is deliberately calculated independently from the
    XGBoost prediction so that the two estimates provide
    complementary spatial information.
    """

    station_lat = fresh[
        "latitude"
    ].to_numpy(
        dtype=float
    )

    station_lon = fresh[
        "longitude"
    ].to_numpy(
        dtype=float
    )

    station_aqi = fresh[
        "true_aqi"
    ].to_numpy(
        dtype=float
    )

    grid_lat = grid[
        "lat"
    ].to_numpy(
        dtype=float
    )

    grid_lon = grid[
        "lon"
    ].to_numpy(
        dtype=float
    )

    output = np.full(
        len(grid),
        np.nan,
        dtype=float
    )

    if len(station_aqi) == 0:
        return output

    for i, (
        lat,
        lon
    ) in enumerate(
        zip(
            grid_lat,
            grid_lon
        )
    ):

        distances = haversine_km(
            lat,
            lon,
            station_lat,
            station_lon
        )

        valid = (
            np.isfinite(distances)
            &
            np.isfinite(station_aqi)
            &
            (distances >= 0)
        )

        if not np.any(valid):
            continue

        valid_indices = np.where(
            valid
        )[0]

        # ----------------------------------------------------
        # Exact station location
        # ----------------------------------------------------

        zero_indices = valid_indices[
            distances[valid_indices] < 1e-6
        ]

        if len(zero_indices) > 0:

            output[i] = float(
                station_aqi[
                    zero_indices[0]
                ]
            )

            continue

        # ----------------------------------------------------
        # Nearest N stations
        # ----------------------------------------------------

        order = valid_indices[
            np.argsort(
                distances[
                    valid_indices
                ]
            )
        ]

        order = order[
            :max_stations
        ]

        d = distances[order]

        aqi = station_aqi[order]

        # ----------------------------------------------------
        # Inverse-distance weighting
        # ----------------------------------------------------

        weights = 1.0 / np.power(
            np.maximum(
                d,
                1e-6
            ),
            power
        )

        weight_sum = np.sum(
            weights
        )

        if (
            np.isfinite(weight_sum)
            and weight_sum > 0
        ):

            estimate = (
                np.sum(
                    weights * aqi
                )
                /
                weight_sum
            )

            output[i] = float(
                estimate
            )

    return np.clip(
        output,
        0,
        500
    )


# ============================================================
# BUILD MODEL MATRIX
# ============================================================

def build_features(grid):

    X = pd.DataFrame(
        index=grid.index
    )

    for feature in FEATURES:

        if feature not in grid.columns:

            X[feature] = np.nan

        else:

            X[feature] = pd.to_numeric(
                grid[feature],
                errors="coerce"
            )

    # XGBoost can handle NaN.

    return X


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=DEFAULT_MAX_AGE_MINUTES,
        help=(
            "Maximum age of a station observation "
            "to be used for live spatial estimation."
        ),
    )

    parser.add_argument(
        "--output",
        default=OUTPUT_PATH,
    )

    parser.add_argument(
        "--idw-power",
        type=float,
        default=IDW_POWER,
        help="Inverse-distance weighting power."
    )

    parser.add_argument(
        "--idw-max-stations",
        type=int,
        default=IDW_MAX_STATIONS,
        help="Maximum number of fresh stations used by IDW."
    )

    parser.add_argument(
        "--xgb-weight",
        type=float,
        default=SPATIAL_XGB_WEIGHT,
        help="Weight of XGBoost spatial estimate."
    )

    parser.add_argument(
        "--idw-weight",
        type=float,
        default=SPATIAL_IDW_WEIGHT,
        help="Weight of IDW spatial estimate."
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # Validate blend
    # --------------------------------------------------------

    weight_sum = (
        args.xgb_weight
        +
        args.idw_weight
    )

    if not np.isclose(
        weight_sum,
        1.0,
        atol=1e-6
    ):
        raise ValueError(
            "XGBoost and IDW weights must sum to 1.0. "
            f"Received {args.xgb_weight} + "
            f"{args.idw_weight} = {weight_sum}"
        )

    if args.idw_power <= 0:
        raise ValueError(
            "IDW power must be > 0."
        )

    if args.idw_max_stations < 1:
        raise ValueError(
            "IDW max stations must be >= 1."
        )

    print()
    print("=" * 70)
    print("AirGrid — LIVE 1600-CELL AQI UPDATE")
    print("=" * 70)

    print()
    print("Spatial estimation configuration:")
    print(
        f"  XGBoost weight     : {args.xgb_weight:.2f}"
    )
    print(
        f"  IDW weight         : {args.idw_weight:.2f}"
    )
    print(
        f"  IDW power          : {args.idw_power:.2f}"
    )
    print(
        f"  IDW max stations   : {args.idw_max_stations}"
    )

    # --------------------------------------------------------
    # 1. MODEL
    # --------------------------------------------------------

    print()
    print("[1/5] Loading trained spatial model")

    model = load_spatial_model()

    # --------------------------------------------------------
    # 2. LIVE DATA
    # --------------------------------------------------------

    print()
    print("[2/5] Loading live station AQI")

    live = load_live_data()

    print(
        f"  → {len(live)} live station rows"
    )

    print(
        f"  → {live['station'].nunique()} unique stations"
    )

    # --------------------------------------------------------
    # 3. FRESH STATIONS
    # --------------------------------------------------------

    print()
    print("[3/5] Selecting fresh station observations")

    fresh, live_ts = select_fresh_stations(
        live,
        args.max_age_minutes
    )

    print()
    print("Fresh station AQI:")
    print(
        fresh[
            [
                "station",
                "timestamp",
                "true_aqi"
            ]
        ]
        .sort_values("true_aqi")
        .to_string(index=False)
    )

    # --------------------------------------------------------
    # 4. GRID + LIVE PROXIES
    # --------------------------------------------------------

    print()
    print("[4/5] Building live spatial features")

    grid = load_training_grid()

    grid = attach_live_proxies(
        grid,
        fresh
    )

    X = build_features(
        grid
    )

    print(
        f"  → feature matrix: "
        f"{X.shape[0]} × {X.shape[1]}"
    )

    if X.shape[1] != len(FEATURES):

        raise RuntimeError(
            f"Expected {len(FEATURES)} model features "
            f"but built {X.shape[1]}."
        )

    # --------------------------------------------------------
    # 5. PREDICT
    # --------------------------------------------------------

    print()
    print("[5/5] Estimating AQI across 1600 cells")

    # ========================================================
    # 5A. XGBOOST SPATIAL ESTIMATE
    # ========================================================

    xgb_predictions = model.predict(
        X
    )

    xgb_predictions = np.asarray(
        xgb_predictions,
        dtype=float
    )

    xgb_predictions = np.clip(
        xgb_predictions,
        0,
        500
    )

    grid[
        "xgb_spatial_aqi"
    ] = xgb_predictions

    print()
    print("XGBoost spatial estimate:")
    print(
        f"  range : "
        f"{np.nanmin(xgb_predictions):.2f} → "
        f"{np.nanmax(xgb_predictions):.2f}"
    )
    print(
        f"  mean  : "
        f"{np.nanmean(xgb_predictions):.2f}"
    )
    print(
        f"  unique: "
        f"{np.unique(np.round(xgb_predictions, 4)).size}"
    )

    # ========================================================
    # 5B. MULTI-STATION IDW
    # ========================================================

    print()
    print(
        "Computing multi-station IDW estimate..."
    )

    idw_predictions = compute_idw_aqi(
        grid,
        fresh,
        power=args.idw_power,
        max_stations=args.idw_max_stations
    )

    grid[
        "idw_aqi"
    ] = idw_predictions

    valid_idw = np.isfinite(
        idw_predictions
    )

    print(
        f"  valid cells : "
        f"{valid_idw.sum()} / {len(grid)}"
    )

    if valid_idw.any():

        print(
            f"  range       : "
            f"{np.nanmin(idw_predictions):.2f} → "
            f"{np.nanmax(idw_predictions):.2f}"
        )

        print(
            f"  mean        : "
            f"{np.nanmean(idw_predictions):.2f}"
        )

        unique_idw = np.unique(
        np.round(
            idw_predictions[valid_idw],
            4
        )
    ).size

    print(
        f"  unique      : {unique_idw}"
    )

    # ========================================================
    # 5C. XGBOOST + IDW BLEND
    # ========================================================

    print()
    print(
        "Blending XGBoost + multi-station IDW..."
    )

    blended = xgb_predictions.copy()

    both_valid = (
        np.isfinite(
            xgb_predictions
        )
        &
        np.isfinite(
            idw_predictions
        )
    )

    blended[both_valid] = (
        args.xgb_weight
        *
        xgb_predictions[both_valid]
        +
        args.idw_weight
        *
        idw_predictions[both_valid]
    )

    # If IDW is unavailable, retain XGBoost.
    xgb_only = (
        np.isfinite(
            xgb_predictions
        )
        &
        ~np.isfinite(
            idw_predictions
        )
    )

    blended[xgb_only] = (
        xgb_predictions[xgb_only]
    )

    blended = np.clip(
        blended,
        0,
        500
    )

    grid[
        "estimated_current_aqi"
    ] = blended

    # ========================================================
    # 5D. EXACT FRESH STATION OVERRIDE
    # ========================================================

    print()
    print(
        "Applying exact live station observations..."
    )

    fresh_station_names = set(
        fresh["station"]
    )

    # Build station -> AQI lookup.
    station_aqi_lookup = (
        fresh
        .set_index("station")[
            "true_aqi"
        ]
        .to_dict()
    )

    direct_count = 0

    for idx, row in grid.iterrows():

        station = row.get(
            "nearest_station"
        )

        if station in fresh_station_names:

            real_aqi = float(
                station_aqi_lookup[
                    station
                ]
            )

            grid.at[
                idx,
                "estimated_current_aqi"
            ] = real_aqi

            grid.at[
                idx,
                "source"
            ] = "live_station"

            direct_count += 1

    # --------------------------------------------------------
    # All remaining cells are spatial estimates.
    # --------------------------------------------------------

    if "source" not in grid.columns:

        grid["source"] = "spatial_blend"

    grid.loc[
        grid["source"].isna(),
        "source"
    ] = "spatial_blend"

    grid[
        "live_reference_timestamp"
    ] = live_ts

    # --------------------------------------------------------
    # Spatial blend metadata
    # --------------------------------------------------------

    grid[
        "spatial_xgb_weight"
    ] = args.xgb_weight

    grid[
        "spatial_idw_weight"
    ] = args.idw_weight

    grid[
        "idw_power"
    ] = args.idw_power

    grid[
        "idw_max_stations"
    ] = args.idw_max_stations

    # --------------------------------------------------------
    # Spatial diagnostics
    # --------------------------------------------------------

    spatial_count = (
        grid["source"] == "spatial_blend"
    ).sum()

    print()
    print(
        "=" * 70
    )
    print(
        "SPATIAL ESTIMATION SUMMARY"
    )
    print(
        "=" * 70
    )

    print(
        f"Fresh stations       : "
        f"{fresh['station'].nunique()}"
    )

    print(
        f"Direct station cells : "
        f"{direct_count}"
    )

    print(
        f"Spatial blend cells  : "
        f"{spatial_count}"
    )

    print()
    print(
        "Final estimated AQI:"
    )

    print(
        f"  min    : "
        f"{grid['estimated_current_aqi'].min():.2f}"
    )

    print(
        f"  max    : "
        f"{grid['estimated_current_aqi'].max():.2f}"
    )

    print(
        f"  mean   : "
        f"{grid['estimated_current_aqi'].mean():.2f}"
    )

    print(
        f"  unique : "
        f"{grid['estimated_current_aqi'].nunique()}"
    )

    # ========================================================
    # FINAL OUTPUT
    # ========================================================

    output_cols = [
        "cell_id",
        "lat",
        "lon",

        "nearest_station",
        "nearest_dist_km",

        "xgb_spatial_aqi",
        "idw_aqi",
        "estimated_current_aqi",

        "source",
        "live_reference_timestamp",

        "spatial_xgb_weight",
        "spatial_idw_weight",
        "idw_power",
        "idw_max_stations",

        "proxy_dist_km",
        "proxy_true_aqi",
        "proxy_pm25",
        "proxy_pm10",
        "proxy_no2",
        "proxy_so2",
        "proxy_o3",
        "proxy_co",
    ]

    output_cols = [
        c for c in output_cols
        if c in grid.columns
    ]

    out = grid[
        output_cols
    ].copy()

    out.to_csv(
        args.output,
        index=False
    )

    # ========================================================
    # FINAL SUMMARY
    # ========================================================

    print()
    print(
        "=" * 70
    )
    print(
        "LIVE CELL AQI UPDATE COMPLETE"
    )
    print(
        "=" * 70
    )

    print(
        f"Reference time       : {live_ts}"
    )

    print(
        f"Fresh stations       : "
        f"{fresh['station'].nunique()}"
    )

    print(
        f"1600-cell rows       : "
        f"{len(out)}"
    )

    print(
        f"Direct station cells : "
        f"{direct_count}"
    )

    print(
        f"Spatial blend cells  : "
        f"{len(out) - direct_count}"
    )

    print()
    print(
        "Spatial method:"
    )

    print(
        f"  XGBoost weight : "
        f"{args.xgb_weight:.2f}"
    )

    print(
        f"  IDW weight     : "
        f"{args.idw_weight:.2f}"
    )

    print(
        f"  IDW power      : "
        f"{args.idw_power:.2f}"
    )

    print(
        f"  IDW stations   : "
        f"{args.idw_max_stations}"
    )

    print()
    print(
        "Source distribution:"
    )

    print(
        out["source"]
        .value_counts()
        .to_string()
    )

    print()
    print(
        "AQI statistics:"
    )

    print(
        out[
            "estimated_current_aqi"
        ]
        .describe()
        .to_string()
    )

    print()
    print(
        f"Saved: {args.output}"
    )


if __name__ == "__main__":
    main()