"""
train_spatial_estimator.py
=============================
PS5 §6 — Ward-level estimation: predicting AQI where there are no stations.

INPUT:  data/training_dataset.csv   (real station-hour rows, from fetch_real_data.py)
        data/stations_static.csv    (real station lat/lon)
OUTPUT: data/cell_aqi_estimated.csv (AQI predicted for all 1600 cells, every real hour)
        data/spatial_estimator.json (saved model — reusable without retraining)
        Printed leave-one-station-out RMSE vs a real IDW baseline and a
        nearest-station baseline.

PIPELINE POSITION
------------------
    OpenAQ stations -> real labels -> [THIS SCRIPT: spatial estimation model]
    -> AQI for all 1600 cells -> forecast model (next script)

TWO MODES
----------
  Training (default):
      python3 src/train_spatial_estimator.py
      python3 src/train_spatial_estimator.py --dataset data/training_dataset.csv

  Predict-only (load saved model, skip retraining):
      python3 src/train_spatial_estimator.py --predict-only
      python3 src/train_spatial_estimator.py --predict-only --dataset data/new_dataset.csv

TWO BASELINES (not the same thing)
------------------------------------
- nearest_baseline: single nearest OTHER station's real reading (1-NN).
- idw_baseline: real inverse-distance weighting across up to IDW_K nearest
  other stations. This is what PS5 §6 means by "IDW" — cite this number.
"""

import os
import numpy as np
import pandas as pd
import math
import argparse

try:
    from xgboost import XGBRegressor
    HAVE_XGB = True
except ImportError:
    from sklearn.ensemble import RandomForestRegressor
    HAVE_XGB = False
    print("[WARN] xgboost not installed — falling back to RandomForestRegressor.")

DATA_DIR   = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
MODEL_PATH = os.path.join(DATA_DIR, "spatial_estimator.json")   # XGBoost; .joblib used for RF

FEATURE_COLS = [
    "wind_speed", "wind_dir", "temp", "humidity",
    "no2_satellite", "aod_satellite",
    "industrial_pct", "construction_pct", "green_cover_pct", "residential_pct", "water_pct",
    "month", "hour", "weekday", "is_winter", "is_summer", "is_crop_burn", "is_festival",
    "proxy_dist_km", "proxy_true_aqi",
    "proxy_pm25", "proxy_pm10", "proxy_no2", "proxy_so2", "proxy_o3", "proxy_co",
]
TARGET_COL = "true_aqi"

IDW_K     = 3
IDW_POWER = 2


# ─── UTILITIES ───────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(min(a, 1.0)))


def idw_predict(value_dist_pairs, k=IDW_K, power=IDW_POWER) -> float:
    if not value_dist_pairs:
        return np.nan
    pairs = sorted(value_dist_pairs, key=lambda vd: vd[1])[:k]
    for val, dist in pairs:
        if dist < 1e-6:
            return val
    weights = [1.0 / (dist ** power) for _, dist in pairs]
    total_w = sum(weights)
    return sum(w * v for w, (v, _) in zip(weights, pairs)) / total_w if total_w > 0 else np.nan


def clean_for_model(df: pd.DataFrame, feature_cols: list, target_col: str = "true_aqi") -> pd.DataFrame:
    """Drop rows with NaN/inf labels; replace inf in features with NaN (XGBoost handles NaN natively)."""
    out = df.copy()
    out[target_col] = pd.to_numeric(out[target_col], errors="coerce")
    out = out[out[target_col].notna() & np.isfinite(out[target_col])].copy()
    present = [c for c in feature_cols if c in out.columns]
    out[present] = out[present].replace([np.inf, -np.inf], np.nan)
    out[target_col] = out[target_col].astype("float32")
    return out


def make_model(fast: bool = False):
    if HAVE_XGB:
        if fast:
            return XGBRegressor(n_estimators=150, max_depth=4, learning_rate=0.08,
                                subsample=0.8, colsample_bytree=0.8, random_state=42, missing=np.nan)
        return XGBRegressor(n_estimators=300, max_depth=5, learning_rate=0.05,
                            subsample=0.8, colsample_bytree=0.8, random_state=42, missing=np.nan)
    if fast:
        return RandomForestRegressor(n_estimators=100, max_depth=8, random_state=42, n_jobs=-1)
    return RandomForestRegressor(n_estimators=300, max_depth=10, random_state=42, n_jobs=-1)


def load_model():
    """Load the saved spatial estimator from disk."""
    if HAVE_XGB:
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(f"Saved model not found: {MODEL_PATH}")
        print(f"  Loading model: {MODEL_PATH}")
        model = make_model(fast=False)
        model.load_model(MODEL_PATH)
        return model
    else:
        import joblib
        rf_path = os.path.join(DATA_DIR, "spatial_estimator.joblib")
        if not os.path.exists(rf_path):
            raise FileNotFoundError(f"Saved model not found: {rf_path}")
        print(f"  Loading model: {rf_path}")
        return joblib.load(rf_path)


# ─── DATA LOADING ────────────────────────────────────────────────────────────

def load_labelled_station_rows(dataset_path: str) -> "tuple[pd.DataFrame, pd.DataFrame]":
    """
    Real station-hour rows (has_station==1) from any dataset CSV.
    Accepts a dataset path so main() can pass --dataset for predict-only mode.
    """
    if not os.path.exists(dataset_path):
        raise FileNotFoundError(f"[ERROR] {dataset_path} not found. Run fetch_real_data.py first.")
    df = pd.read_csv(dataset_path, parse_dates=["timestamp"])
    labelled = df[df["has_station"] == 1].copy()

    labelled[TARGET_COL] = pd.to_numeric(labelled[TARGET_COL], errors="coerce")
    n_before = len(labelled)
    n_nan = int(labelled[TARGET_COL].isna().sum())
    n_inf = int(np.isinf(labelled[TARGET_COL].fillna(0)).sum())
    if n_nan or n_inf:
        print(f"  [data quality] {n_nan:,} NaN + {n_inf:,} inf true_aqi rows — dropping.")
    labelled = labelled[labelled[TARGET_COL].notna() & np.isfinite(labelled[TARGET_COL])].copy()
    if len(labelled) < n_before:
        print(f"  → {n_before - len(labelled):,} rows dropped ({len(labelled):,} remain)")

    before = len(labelled)
    labelled = labelled.drop_duplicates(subset=["nearest_station", "timestamp"], keep="first")
    if len(labelled) < before:
        print(f"  [WARN] {before - len(labelled):,} duplicate (station, timestamp) rows deduped.")

    stations_path = os.path.join(DATA_DIR, "stations_static.csv")
    if not os.path.exists(stations_path):
        raise FileNotFoundError(f"[ERROR] {stations_path} not found. Re-run fetch_real_data.py.")
    stations = pd.read_csv(stations_path).drop_duplicates(subset=["station"], keep="first")

    print(f"  → {len(labelled):,} real labelled rows across "
          f"{labelled['nearest_station'].nunique()} stations")
    return labelled, stations


# ─── FEATURE ENGINEERING ─────────────────────────────────────────────────────

def build_station_distance_matrix(stations: pd.DataFrame) -> pd.DataFrame:
    names = stations["station"].tolist()
    mat = pd.DataFrame(index=names, columns=names, dtype=float)
    for _, si in stations.iterrows():
        for _, sj in stations.iterrows():
            mat.loc[si.station, sj.station] = haversine_km(
                si.latitude, si.longitude, sj.latitude, sj.longitude
            )
    return mat


def build_spatially_honest_features(labelled: pd.DataFrame, dist_mat: pd.DataFrame) -> pd.DataFrame:
    """
    For every labelled row (station S, hour T):
      proxy features come from the nearest OTHER station reporting at the same hour T.
      idw_baseline_pred from up to IDW_K nearest OTHER stations at that hour.
    Rows with no other station at that hour are dropped.
    """
    print("  Building spatially-honest features ...")
    pollutant_cols = [c for c in ["pm25","pm10","no2","so2","o3","co"] if c in labelled.columns]
    by_time = {ts: grp.set_index("nearest_station")
               for ts, grp in labelled.groupby("timestamp")}

    out_rows = []
    for _, row in labelled.iterrows():
        ts          = row["timestamp"]
        self_st     = row["nearest_station"]
        same_hour   = by_time.get(ts)
        if same_hour is None or len(same_hour) < 2:
            continue
        candidates = [s for s in same_hour.index if s != self_st]
        if not candidates:
            continue
        dists   = {s: dist_mat.loc[self_st, s] for s in candidates}
        nearest = min(dists, key=dists.get)
        other   = same_hour.loc[nearest]

        idw_pairs = [(float(same_hour.loc[s].get("true_aqi", np.nan)), dists[s])
                     for s in candidates if pd.notna(same_hour.loc[s].get("true_aqi", np.nan))]
        idw_pred  = idw_predict(idw_pairs)

        keep = ["cell_id","timestamp","lat","lon","wind_speed","wind_dir","temp","humidity",
                "no2_satellite","aod_satellite",
                "industrial_pct","construction_pct","green_cover_pct","residential_pct","water_pct",
                "month","hour","weekday","is_winter","is_summer","is_crop_burn","is_festival",
                TARGET_COL]
        rec = row[[c for c in keep if c in row.index]].to_dict()
        rec["proxy_dist_km"]    = dists[nearest]
        rec["proxy_true_aqi"]   = other.get("true_aqi", np.nan)
        rec["idw_baseline_pred"] = idw_pred
        for p in pollutant_cols:
            rec[f"proxy_{p}"] = other.get(p, np.nan)
        rec["station"] = self_st
        out_rows.append(rec)

    result  = pd.DataFrame(out_rows)
    dropped = len(labelled) - len(result)
    print(f"  → {len(result):,} rows with honest proxy "
          f"({dropped:,} dropped — no other station at that hour)")
    return result


# ─── VALIDATION ──────────────────────────────────────────────────────────────

def leave_one_station_out(features: pd.DataFrame, loso_max_stations: int = None):
    print("\n  Leave-one-station-out SPATIAL validation ...")
    all_st = features["station"].unique()
    stations = all_st
    if loso_max_stations and len(all_st) > loso_max_stations:
        rng = np.random.default_rng(42)
        stations = rng.choice(all_st, size=loso_max_stations, replace=False)
        print(f"  [note] sampling {loso_max_stations} / {len(all_st)} stations for LOSO")

    ml_errs, nn_errs, idw_errs, per_st = [], [], [], []
    for held in stations:
        train = clean_for_model(features[features["station"] != held].copy(), FEATURE_COLS, TARGET_COL)
        test  = clean_for_model(features[features["station"] == held].copy(), FEATURE_COLS, TARGET_COL)
        if len(train) < 50 or test.empty:
            continue
        model = make_model(fast=True)
        model.fit(train[FEATURE_COLS], train[TARGET_COL])
        pred = model.predict(test[FEATURE_COLS])
        yte  = test[TARGET_COL].values

        ml_rmse  = float(np.sqrt(np.mean((pred - yte) ** 2)))
        nn_rmse  = float(np.sqrt(np.mean((test["proxy_true_aqi"].values - yte) ** 2)))
        idw_mask = test["idw_baseline_pred"].notna()
        idw_rmse = (float(np.sqrt(np.mean((test.loc[idw_mask,"idw_baseline_pred"].values
                                           - yte[idw_mask.values]) ** 2)))
                    if idw_mask.any() else float("nan"))

        ml_errs.append(ml_rmse); nn_errs.append(nn_rmse)
        if not np.isnan(idw_rmse): idw_errs.append(idw_rmse)
        per_st.append({"station": held, "n_rows": len(test),
                       "ml_rmse": round(ml_rmse,2),
                       "nearest_baseline_rmse": round(nn_rmse,2),
                       "idw_baseline_rmse": round(idw_rmse,2) if not np.isnan(idw_rmse) else None})

    print(pd.DataFrame(per_st).sort_values("ml_rmse").to_string(index=False))
    mean_ml  = np.mean(ml_errs)
    mean_nn  = np.mean(nn_errs)
    mean_idw = np.mean(idw_errs) if idw_errs else float("nan")
    print(f"\n  Mean LOSO RMSE — ML model              : {mean_ml:.2f}")
    print(f"  Mean LOSO RMSE — nearest-station (1-NN): {mean_nn:.2f}")
    if not np.isnan(mean_idw):
        imp = (1 - mean_ml / mean_idw) * 100
        print(f"  Mean LOSO RMSE — real IDW (k={IDW_K})     : {mean_idw:.2f}")
        print(f"  ML improvement over IDW baseline       : {imp:+.1f}%  ← cite THIS")
    print(f"  ML improvement over nearest baseline   : {(1 - mean_ml/mean_nn)*100:+.1f}%")


# ─── TRAINING + SAVING ───────────────────────────────────────────────────────

def train_final_model(features: pd.DataFrame):
    print("\n  Training final model on ALL labelled data ...")
    clean = clean_for_model(features, FEATURE_COLS, TARGET_COL)
    if len(clean) < len(features):
        print(f"  [data quality] {len(features)-len(clean):,} rows dropped ({len(clean):,} remain)")
    model = make_model(fast=False)
    model.fit(clean[FEATURE_COLS], clean[TARGET_COL])

    if HAVE_XGB:
        model.save_model(MODEL_PATH)
        print(f"  → saved: {MODEL_PATH}")
    else:
        import joblib
        rf_path = os.path.join(DATA_DIR, "spatial_estimator.joblib")
        joblib.dump(model, rf_path)
        print(f"  → saved: {rf_path}")

    return model


# ─── PREDICTION ──────────────────────────────────────────────────────────────

def predict_all_cells(model, dataset_path: str) -> pd.DataFrame:
    print("\n  Predicting AQI for all cells ...")
    df = pd.read_csv(dataset_path, parse_dates=["timestamp"])
    pollutant_cols = [c for c in ["pm25","pm10","no2","so2","o3","co"] if c in df.columns]

    station_aqi_lookup = (
        df[df["has_station"] == 1][["nearest_station","timestamp","true_aqi"]]
        .dropna(subset=["true_aqi"])
        .drop_duplicates(subset=["nearest_station","timestamp"], keep="first")
        .rename(columns={"true_aqi": "proxy_true_aqi"})
    )
    df_proxy = df.merge(station_aqi_lookup, on=["nearest_station","timestamp"],
                        how="left", validate="many_to_one")
    assert len(df_proxy) == len(df), "Row count changed after proxy merge — investigate."

    X = pd.DataFrame({
        "wind_speed": df["wind_speed"], "wind_dir": df["wind_dir"],
        "temp": df["temp"], "humidity": df["humidity"],
        "no2_satellite": df["no2_satellite"], "aod_satellite": df["aod_satellite"],
        "industrial_pct": df["industrial_pct"], "construction_pct": df["construction_pct"],
        "green_cover_pct": df["green_cover_pct"], "residential_pct": df["residential_pct"],
        "water_pct": df["water_pct"],
        "month": df["month"], "hour": df["hour"], "weekday": df["weekday"],
        "is_winter": df["is_winter"], "is_summer": df["is_summer"],
        "is_crop_burn": df["is_crop_burn"], "is_festival": df["is_festival"],
        "proxy_dist_km": df["nearest_dist_km"],
        "proxy_true_aqi": df_proxy["proxy_true_aqi"],
    })
    for p in pollutant_cols:
        X[f"proxy_{p}"] = df[p]
    X[FEATURE_COLS] = X[FEATURE_COLS].replace([np.inf, -np.inf], np.nan)

    # Model prediction for every row
    # Model prediction for every row
    pred = model.predict(X[FEATURE_COLS]).astype("float64")

    # Create the output column as float64
    df["predicted_aqi"] = pred

    # Only overwrite when a real observation actually exists
    mask = (df["has_station"] == 1) & df["true_aqi"].notna()

    df["predicted_aqi"] = np.where(
        mask,
        df["true_aqi"].astype("float64"),
        df["predicted_aqi"],
    )

    # Safety clamp
    df["predicted_aqi"] = df["predicted_aqi"].clip(0, 500)
    df["is_estimated"]  = (df["has_station"] != 1).astype(int)

    out = df[["cell_id","timestamp","lat","lon","predicted_aqi","is_estimated",
              "nearest_station","nearest_dist_km"]]
    out_path = os.path.join(DATA_DIR, "cell_aqi_estimated.csv")
    out.to_csv(out_path, index=False)
    print(f"  → {len(out):,} rows saved to {out_path} "
          f"({(out['is_estimated']==0).sum():,} real, {(out['is_estimated']==1).sum():,} estimated)")
    return out


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Spatial AQI estimation model (PS5 §6)")
    parser.add_argument("--loso-max-stations", type=int, default=20,
                        help="Cap on stations for LOSO validation. 0 = exhaustive.")
    parser.add_argument("--dataset", type=str,
                        default=os.path.join(DATA_DIR, "training_dataset.csv"),
                        help="Dataset CSV for training or prediction.")
    parser.add_argument("--predict-only", action="store_true",
                        help="Load saved model and predict without retraining.")
    args = parser.parse_args()

    # ── PREDICT-ONLY MODE ────────────────────────────────────────────────────
    if args.predict_only:
        print(f"\n{'='*60}\n  Spatial Estimator — PREDICT ONLY\n{'='*60}")
        print(f"\n  Dataset : {args.dataset}")
        model = load_model()
        predict_all_cells(model, args.dataset)
        print("\n  ✓ Predict-only inference complete.")
        return

    # ── TRAINING MODE ────────────────────────────────────────────────────────
    loso_cap = None if args.loso_max_stations == 0 else args.loso_max_stations

    print(f"\n{'='*60}\n  Spatial Estimation Model — PS5 §6\n"
          f"  {'XGBoost' if HAVE_XGB else 'RandomForest'}\n"
          f"  Dataset: {args.dataset}\n{'='*60}\n")

    print("[1/4] Loading real labelled station data")
    labelled, stations = load_labelled_station_rows(args.dataset)

    print("\n[2/4] Station-to-station distances")
    dist_mat = build_station_distance_matrix(stations)

    print("\n[3/4] Honest features + leave-one-station-out validation")
    features = build_spatially_honest_features(labelled, dist_mat)
    if features.empty:
        raise RuntimeError(
            "No rows had an honest same-hour proxy. Try a longer date range "
            "in fetch_real_data.py so more stations overlap in time."
        )
    leave_one_station_out(features, loso_max_stations=loso_cap)

    print("\n[4/4] Training final model + predicting all cells")
    model = train_final_model(features)
    predict_all_cells(model, args.dataset)

    print(f"\n{'='*60}\n  ✓ Done\n"
          f"  Saved model : {MODEL_PATH}\n"
          f"  Next: python3 src/train_forecaster.py\n{'='*60}\n")


if __name__ == "__main__":
    main()
