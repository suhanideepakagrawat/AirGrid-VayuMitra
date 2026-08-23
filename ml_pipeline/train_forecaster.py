"""
train_forecaster.py
======================
PS5 §8 — AQI forecaster (Model 2): predicts AQI 24h / 48h / 72h ahead per
grid cell.

PIPELINE POSITION
--------------------
    OpenAQ stations -> real labels -> spatial estimation model (Model 1)
    -> AQI for all 1600 cells -> THIS SCRIPT (Model 2: forecaster)
    -> 24h / 48h / 72h AQI forecast per cell

WHY LAG FEATURES ARE RECOMPUTED HERE (not reused from training_dataset.csv)
-------------------------------------------------------------------------------
training_dataset.csv's lag features were built on `true_aqi`, which is NaN
everywhere except the ~36 station cells (by design — see fetch_real_data.py).
A forecaster needs to predict every cell, so this script recomputes lag and
rolling features on the FULL real+estimated series from
data/cell_aqi_estimated.csv instead.

VALIDATION METHOD
---------------------
Time-based train/test split (train on the earlier period, test on the later
period, across all cells) — NOT a random split, which would leak nearby
timestamps between train and test and overstate accuracy. Every horizon is
compared against a persistence baseline ("tomorrow's AQI = today's AQI"),
which is the standard naive forecast benchmark PS5 §15 asks for
("beat a naive baseline").

HONESTY CAVEAT — STATE THIS IN THE PITCH
---------------------------------------------
PS5 lists "weather forecast" as a forecaster input. This script uses TWO
kinds of weather feature, and they carry different honesty caveats:

  - wind_speed/wind_dir/temp/humidity: weather AT THE SOURCE timestamp t
    (current/recent conditions feeding the lag features).
  - target_wind_speed/target_wind_dir/target_temp/target_humidity: weather
    AT THE TARGET timestamp t+horizon. During training/backtesting this is
    real HISTORICAL weather (it already happened, from Open-Meteo's
    archive endpoint) — using the real historical value at t+horizon is
    the correct way to train, since it's what the model will need to
    condition on at inference. But at LIVE inference time, t+horizon is in
    the future and hasn't happened yet — you MUST swap this column for
    real forecast weather (Open-Meteo's forecast endpoint, not archive) or
    the model will be silently fed a value it never sees in production.
    Say this plainly if asked: training uses historical-actual weather at
    the target time as a stand-in for what a forecast API would supply;
    it is not itself a live forecast integration.

Target-time CALENDAR features (month/hour/weekday/season/festival) are
NOT subject to this caveat — the calendar 24h from now is known exactly,
no forecast needed, so those are computed directly from the target
timestamp and used as-is.
"""

import os
import numpy as np
import pandas as pd

try:
    from xgboost import XGBRegressor
    HAVE_XGB = True
except ImportError:
    from sklearn.ensemble import RandomForestRegressor
    HAVE_XGB = False
    print("[WARN] xgboost not installed — falling back to RandomForestRegressor. "
          "pip install xgboost --break-system-packages for the PS-recommended model.")

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
HORIZONS_HOURS = [24, 48, 72]

FESTIVAL_MONTHS_DAYS = {(10, 24), (10, 25), (11, 1), (11, 12), (11, 13)}
WINTER_MONTHS = {11, 12, 1, 2}
SUMMER_MONTHS = {4, 5, 6}
CROP_BURNING_MONTHS = {10, 11}

FEATURE_COLS_BASE = [
    "aqi_lag_1h", "aqi_lag_6h", "aqi_lag_12h", "aqi_lag_24h", "aqi_lag_48h",
    "aqi_roll_mean_24h", "aqi_roll_mean_7d", "aqi_prev_day_max",
    "wind_speed", "wind_dir", "temp", "humidity",
    "target_wind_speed", "target_wind_dir", "target_temp", "target_humidity",
    "industrial_pct", "construction_pct", "green_cover_pct", "residential_pct", "water_pct",
    "nearest_dist_km", "is_estimated",
    # target-time calendar — known exactly in advance, not a forecast:
    "target_month", "target_hour", "target_weekday",
    "target_is_winter", "target_is_summer", "target_is_crop_burn", "target_is_festival",
]


def make_model():
    if HAVE_XGB:
        return XGBRegressor(
            n_estimators=250, max_depth=6, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            tree_method="hist", max_bin=256, n_jobs=6,
            random_state=42, missing=np.nan,
        )
    return RandomForestRegressor(n_estimators=200, max_depth=10, random_state=42, n_jobs=-1)


def reduce_training_rows(train: pd.DataFrame, max_estimated_rows: int = 1_000_000) -> pd.DataFrame:
    """
    Keeps ALL real-cell rows (the only ground truth this project has), and
    subsamples estimated-cell rows down to a cap. Two reasons this matters
    at 7M+ rows, not just speed:
      1. RAM/runtime on a laptop — training on every one of 7M+ rows with
         400-tree XGBoost is genuinely impractical outside a beefy machine.
      2. Estimated rows are Model 1's OUTPUT, not ground truth. With real
         rows outnumbered ~20:1 by estimated ones, training on all of them
         unweighted lets Model 1's own biases dominate what Model 2 learns
         far more than is healthy — capping the estimated share keeps real
         station data properly influential rather than diluted to noise.
    Test set is intentionally left untouched by this — only the amount of
    TRAINING data changes; validation still measures against everything.
    """
    if "is_estimated" not in train.columns:
        return train

    real = train[train["is_estimated"] == 0]
    estimated = train[train["is_estimated"] == 1]

    if len(estimated) > max_estimated_rows:
        estimated = estimated.sample(n=max_estimated_rows, random_state=42)

    reduced = pd.concat([real, estimated], ignore_index=True)
    return reduced.sample(frac=1, random_state=42).reset_index(drop=True)


def load_and_merge() -> pd.DataFrame:
    """Combine the full (real+estimated) AQI series from Model 1 with the
    feature columns from training_dataset.csv.

    Reads only the columns actually needed (via usecols) and as float32
    from the start, instead of loading training_dataset.csv's full ~30
    columns (pollutants, its own unused lag features, satellite, etc.) at
    float64 and subsetting after. At 7M+ rows this is the difference
    between loading roughly 30 columns and 11 — a real memory win that
    matters before any of the heavier lag/rolling computation even starts.
    """
    est_path = os.path.join(DATA_DIR, "cell_aqi_estimated.csv")
    full_path = os.path.join(DATA_DIR, "training_dataset.csv")
    if not os.path.exists(est_path):
        raise FileNotFoundError(f"[ERROR] {est_path} not found. Run train_spatial_estimator.py first.")
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"[ERROR] {full_path} not found. Run fetch_real_data.py first.")

    est_dtypes = {"cell_id": "int32", "lat": "float32", "lon": "float32",
                  "predicted_aqi": "float32", "is_estimated": "int8", "nearest_dist_km": "float32"}
    est = pd.read_csv(est_path, parse_dates=["timestamp"], dtype=est_dtypes)

    feature_cols = [
        "cell_id", "timestamp", "wind_speed", "wind_dir", "temp", "humidity",
        "industrial_pct", "construction_pct", "green_cover_pct", "residential_pct", "water_pct",
    ]
    # Only request columns that actually exist in the file (older datasets
    # built before land use was added would be missing those columns).
    header = pd.read_csv(full_path, nrows=0).columns
    usecols = [c for c in feature_cols if c in header]
    full_dtypes = {c: "float32" for c in usecols if c not in ("cell_id", "timestamp")}
    full_dtypes["cell_id"] = "int32"
    full = pd.read_csv(full_path, usecols=usecols, parse_dates=["timestamp"], dtype=full_dtypes)

    merged = est.merge(full, on=["cell_id", "timestamp"], how="left")
    merged = merged.rename(columns={"predicted_aqi": "aqi"})
    merged["aqi"] = merged["aqi"].astype("float32")
    n_real = int((merged["is_estimated"] == 0).sum())
    n_est = int((merged["is_estimated"] == 1).sum())
    print(f"  → merged {len(merged):,} cell-hour rows ({n_real:,} real, {n_est:,} estimated)")
    return merged


def add_full_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Lag/rolling features on the COMPLETE aqi series (real+estimated) — this
    is what makes forecasting possible for all 1600 cells, not just the
    handful of station cells.

    TIME-BASED, not row-based: this pipeline deliberately leaves real gaps
    as gaps (a station that didn't report an hour, a month that failed
    after retries) rather than inventing data — which means a plain
    groupby().shift(n) is WRONG here. shift(n) skips n ROWS, and a single
    missing hour makes every subsequent row's "n hours ago" actually be
    n+1 (or more) real hours ago, silently. Two different fixes for the
    two kinds of feature:
      - point lags (1h/6h/12h/24h/48h): exact-timestamp lookup via an
        indexed Series (not a merge — merging 5 times at 7M+ rows would
        copy the entire dataframe 5 times over; reindex() on a (cell_id,
        timestamp)-indexed Series avoids that). Looks up exactly (this
        cell, this timestamp - h hours); if that exact hour doesn't exist
        in the data, the lag is correctly NaN, not silently wrong.
      - rolling stats (24h mean, 7d mean, prev-day max): reindex each
        cell's series onto a continuous hourly grid first (gaps become
        explicit NaN rows), THEN roll over that. A 24-row window on a
        truly continuous hourly index really does mean 24 elapsed hours.
    """
    print("  Computing TIME-BASED lag/rolling features (indexed reindex() lookups, not row-shifts "
          "or repeated full-frame merges) ...")
    df = df.sort_values(["cell_id", "timestamp"]).reset_index(drop=True)

    before = len(df)
    df = df.drop_duplicates(subset=["cell_id", "timestamp"], keep="first")
    if len(df) < before:
        print(f"  [WARN] {before - len(df):,} duplicate (cell_id, timestamp) rows found in the "
              f"AQI series — deduped, keeping first. Investigate if this is more than a handful.")

    aqi_lookup = df.set_index(["cell_id", "timestamp"])["aqi"].sort_index()

    for h in [1, 6, 12, 24, 48]:
        lag_keys = pd.MultiIndex.from_arrays(
            [df["cell_id"], df["timestamp"] - pd.Timedelta(hours=h)]
        )
        df[f"aqi_lag_{h}h"] = aqi_lookup.reindex(lag_keys).to_numpy(dtype="float32")

    print("  Reindexing each cell to a continuous hourly grid for rolling stats "
          "(gaps become explicit NaN, not silently skipped) ...")
    roll_frames = []
    for cell_id, grp in df.groupby("cell_id"):
        s = grp.set_index("timestamp")["aqi"].sort_index()
        full_idx = pd.date_range(s.index.min(), s.index.max(), freq="h")
        s_full = s.reindex(full_idx)
        roll_frames.append(pd.DataFrame({
            "cell_id": cell_id,
            "timestamp": full_idx,
            "aqi_roll_mean_24h": s_full.rolling(24, min_periods=6).mean().values,
            "aqi_roll_mean_7d": s_full.rolling(168, min_periods=24).mean().values,
            "aqi_prev_day_max": s_full.rolling(24, min_periods=6).max().shift(24).values,
        }))
    roll_df = pd.concat(roll_frames, ignore_index=True)
    roll_lookup = roll_df.set_index(["cell_id", "timestamp"]).sort_index()
    row_keys = pd.MultiIndex.from_arrays([df["cell_id"], df["timestamp"]])
    for c in ["aqi_roll_mean_24h", "aqi_roll_mean_7d", "aqi_prev_day_max"]:
        df[c] = roll_lookup[c].reindex(row_keys).to_numpy(dtype="float32").round(2)
    return df


def build_horizon_dataset(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """One row per (cell, t): features at t, target = aqi at t+horizon,
    persistence_baseline = aqi at t (naive 'no change' forecast).

    target_aqi is an EXACT-TIMESTAMP lookup (indexed reindex(), not
    groupby().shift(-horizon) and not a merge) — the same row-vs-time bug
    as the lag features above: shift(-horizon) would grab whatever row
    happens to be `horizon` ROWS later, which is only really `horizon`
    HOURS later if there are zero gaps in between. A missing hour anywhere
    in the window silently mislabels the target. The lookup instead finds
    (this cell, this timestamp + horizon) directly — if that hour doesn't
    exist in the real data, target_aqi is correctly NaN and the row gets
    dropped (see dropna below), rather than training on a wrongly-shifted
    value. reindex() also avoids merge's full-dataframe-copy overhead.

    target_is_estimated: whether Model 1's output AT THE TARGET time was
    real or estimated, looked up the same way as target_aqi rather than
    reused from the SOURCE row's is_estimated. NOTE: in the current
    pipeline is_estimated is a static per-cell property (fixed by whether
    that grid cell contains a station — has_station never changes over
    time for a given cell_id), so target_is_estimated and the source row's
    is_estimated are mathematically guaranteed to be identical for the
    same cell today. This lookup is still done properly rather than
    reusing the source value, so the evaluation split below stays correct
    if is_estimated ever becomes time-varying in a future version (e.g. a
    station going offline for a stretch and being backfilled by Model 1
    only for those specific hours).

    Also attaches target-time weather (historical-actual, at t+horizon) —
    see module HONESTY CAVEAT: correct for training, must be swapped for a
    real forecast at live inference."""
    df = df.sort_values(["cell_id", "timestamp"]).reset_index(drop=True)
    target_ts = df["timestamp"] + pd.Timedelta(hours=horizon)

    out = df.copy()
    out["persistence_baseline"] = df["aqi"]
    out["_target_ts"] = target_ts

    target_keys = pd.MultiIndex.from_arrays([df["cell_id"], target_ts])
    aqi_lookup = df.set_index(["cell_id", "timestamp"])["aqi"].sort_index()
    out["target_aqi"] = aqi_lookup.reindex(target_keys).to_numpy(dtype="float32")
    if "is_estimated" in df.columns:
        is_est_lookup = df.set_index(["cell_id", "timestamp"])["is_estimated"].sort_index()
        out["target_is_estimated"] = is_est_lookup.reindex(target_keys).to_numpy()

    out["target_month"] = target_ts.dt.month
    out["target_hour"] = target_ts.dt.hour
    out["target_weekday"] = target_ts.dt.dayofweek
    out["target_is_winter"] = target_ts.dt.month.isin(WINTER_MONTHS).astype(int)
    out["target_is_summer"] = target_ts.dt.month.isin(SUMMER_MONTHS).astype(int)
    out["target_is_crop_burn"] = target_ts.dt.month.isin(CROP_BURNING_MONTHS).astype(int)
    out["target_is_festival"] = target_ts.apply(lambda t: 1 if (t.month, t.day) in FESTIVAL_MONTHS_DAYS else 0)

    # Weather is city-wide (one Open-Meteo location), so every cell shares
    # the same value at a given timestamp — one row per unique timestamp is
    # enough for the lookup. validate="many_to_one" makes sure that holds;

    # if it doesn't (e.g. a future multi-station-weather version of this
    # pipeline), this fails loudly instead of silently duplicating rows.
    weather_cols = [c for c in ["wind_speed", "wind_dir", "temp", "humidity"] if c in df.columns]
    if weather_cols:
        weather_lookup = (
            df.drop_duplicates(subset=["timestamp"])[["timestamp", *weather_cols]]
            .rename(columns={"timestamp": "_target_ts",
                              **{c: f"target_{c}" for c in weather_cols}})
        )
        before = len(out)
        out = out.merge(weather_lookup, on="_target_ts", how="left", validate="many_to_one")
        assert len(out) == before, (
            "build_horizon_dataset: row count changed after target-time weather merge — "
            "the many_to_one validation should have caught this; stop and investigate."
        )
    out = out.drop(columns=["_target_ts"])

    out = out.dropna(subset=["target_aqi"])  # can't train/eval without a real future value to compare to
    return out


def temporal_train_test_split(df: pd.DataFrame, test_frac: float = 0.2):
    """Time-based split — train on the earlier period, test on the later
    period, across all cells. A random split would leak nearby timestamps
    between train and test and overstate how good the model really is."""
    cutoff = df["timestamp"].quantile(1 - test_frac)
    train = df[df["timestamp"] < cutoff]
    test = df[df["timestamp"] >= cutoff]
    return train, test


def train_and_validate_horizon(df: pd.DataFrame, horizon: int):
    print(f"\n{'='*60}\n  Horizon: {horizon}h ahead\n{'='*60}")
    hdf = build_horizon_dataset(df, horizon)
    feature_cols = [c for c in FEATURE_COLS_BASE if c in hdf.columns]

    train, test = temporal_train_test_split(hdf)
    print(f"  original train rows: {len(train):,} (up to {train['timestamp'].max() if len(train) else 'n/a'})")
    print(f"  test rows          : {len(test):,} (from {test['timestamp'].min() if len(test) else 'n/a'}) "
          f"— kept complete, only training rows are reduced")
    if len(train) < 100 or len(test) < 20:
        print(f"  [WARN] Not enough data for a reliable {horizon}h model yet — every row needs "
              f"{horizon}+ hours of FUTURE history to have a real target, so short date ranges "
              f"starve the longer horizons especially. Widen --start-date/--end-date in "
              f"fetch_real_data.py and re-run the pipeline if this keeps happening.")
        return None

    train = reduce_training_rows(train, max_estimated_rows=1_000_000)
    print(f"  reduced train rows : {len(train):,} (all real rows kept; estimated rows capped at 1,000,000 "
          f"so Model 1's output can't dominate training, and so this fits on a laptop)")

    Xtr = train[feature_cols].astype("float32")
    ytr = train["target_aqi"].astype("float32")
    Xte = test[feature_cols].astype("float32")
    yte = test["target_aqi"].astype("float32")

    model = make_model()
    model.fit(Xtr, ytr)
    pred = model.predict(Xte)

    model_rmse = float(np.sqrt(np.mean((pred - yte.values) ** 2)))

    # persistence_baseline is the SOURCE-time aqi value, which can itself be
    # NaN (e.g. a real station-hour with too few pollutants that hour for
    # aqi_from_row() to compute anything — the same real-world gap fixed
    # upstream for training LABELS in train_spatial_estimator.py, but it
    # still legitimately shows up here since cell_aqi_estimated.csv doesn't
    # drop those rows, only leaves them NaN). A plain np.mean() propagates
    # a SINGLE NaN to the whole result — which is exactly why baseline_rmse
    # came back NaN for every horizon despite 1.39M mostly-valid test rows.
    # Fix: compute baseline_rmse only over rows where both the baseline and
    # the target are finite. And since that's now a DIFFERENT (smaller)
    # subset than what model_rmse (above) was computed over, comparing them
    # directly would not be apples-to-apples — so also compute
    # model_rmse_comparable on that same valid-baseline subset, and base
    # the improvement-over-baseline % on the comparable numbers, not on
    # mixing a full-test-set model score against a subset baseline score.
    baseline_vals = test["persistence_baseline"].to_numpy(dtype="float32")
    yte_vals = yte.to_numpy(dtype="float32")
    valid_baseline = np.isfinite(baseline_vals) & np.isfinite(yte_vals)

    if valid_baseline.sum() > 0:
        model_rmse_comparable = float(np.sqrt(np.mean((pred[valid_baseline] - yte_vals[valid_baseline]) ** 2)))
        baseline_rmse = float(np.sqrt(np.mean((baseline_vals[valid_baseline] - yte_vals[valid_baseline]) ** 2)))
        improvement = (1 - model_rmse_comparable / baseline_rmse) * 100 if baseline_rmse > 0 else float("nan")
    else:
        model_rmse_comparable = float("nan")
        baseline_rmse = float("nan")
        improvement = float("nan")

    print(f"  Model RMSE — all test rows          : {model_rmse:.2f}")
    print(f"  Model RMSE — baseline-comparable rows: {model_rmse_comparable:.2f}")
    print(f"  Persistence baseline RMSE            : {baseline_rmse:.2f}")
    print(f"  Improvement over baseline: {improvement:+.1f}%")
    print(f"  Comparable rows: {int(valid_baseline.sum()):,} / {len(test):,}")
    if valid_baseline.sum() < len(test):
        print(f"  [note] {len(test) - int(valid_baseline.sum()):,} test rows had a NaN source-time "
              f"AQI (real data gap) and were excluded from the baseline comparison only — the model "
              f"itself was still evaluated on them via model_rmse above (it can predict from other "
              f"features even when the source-time AQI itself is missing).")
    if not np.isnan(baseline_rmse) and model_rmse_comparable >= baseline_rmse:
        print(f"  [WARN] Model did NOT beat the naive baseline at {horizon}h. This is a real "
              f"result to report honestly, not to hide — it usually means more historical data "
              f"or stronger features are needed before this horizon is solid. A 72h forecast "
              f"is genuinely harder than 24h; don't be surprised if it's the weak point.")

    # --- Error-propagation check: does forecast accuracy differ between
    # targets whose value at t+horizon is REAL (station cell) vs whose
    # value was itself predicted by Model 1 (estimated)? Uses
    # target_is_estimated (the target-time flag), not the source row's
    # is_estimated — evaluating against what the TARGET actually is, not
    # what the source happened to be. In the current pipeline these are
    # always identical for a given cell (is_estimated is a static per-cell
    # property — see build_horizon_dataset docstring), so this doesn't
    # change today's numbers, but it's the correct thing to key off if
    # that ever stops being true. A large gap here is the concrete,
    # measured version of "Model 2 learns from Model 1's mistakes" — not
    # just a caveat in a docstring. Caveat: for estimated targets we don't
    # have real ground truth to check Model 1's estimates against, so this
    # doesn't prove Model 1 was right there; it only shows whether the
    # forecaster's OWN behavior differs across the two populations, which
    # is still useful signal (e.g. estimated series are often smoother
    # than real ones, which can quietly inflate apparent accuracy at
    # estimated cells without that accuracy being trustworthy).
    real_baseline_rmse = model_real_rmse = np.nan
    est_baseline_rmse = model_est_rmse = np.nan
    split_col = "target_is_estimated" if "target_is_estimated" in test.columns else (
        "is_estimated" if "is_estimated" in test.columns else None
    )
    if split_col:
        is_real = test[split_col].values == 0
        is_est = test[split_col].values == 1
        # Same fix as the main baseline_rmse above: persistence_baseline can
        # be NaN within either subset too (a real-cell source-time gap isn't
        # excluded by is_real/is_est alone), so mask with valid_baseline
        # here as well — this was the exact inconsistency: the main
        # baseline got the finite-value fix, these two split baselines
        # didn't, and a single NaN in either subset silently NaN'd both.
        real_comparable = is_real & valid_baseline
        est_comparable = is_est & valid_baseline

        # Report model-vs-baseline on the EXACT SAME rows for fair comparison.
        # This avoids comparing model RMSE on all subset rows against baseline
        # RMSE on only rows where persistence_baseline is finite.
        if real_comparable.sum() >= 20:
            model_real_rmse = float(np.sqrt(np.mean(
                (pred[real_comparable] - yte_vals[real_comparable]) ** 2)))
            real_baseline_rmse = float(np.sqrt(np.mean(
                (baseline_vals[real_comparable] - yte_vals[real_comparable]) ** 2)))

        if est_comparable.sum() >= 20:
            model_est_rmse = float(np.sqrt(np.mean(
                (pred[est_comparable] - yte_vals[est_comparable]) ** 2)))
            est_baseline_rmse = float(np.sqrt(np.mean(
                (baseline_vals[est_comparable] - yte_vals[est_comparable]) ** 2)))

        print(f"  Split by target-time Model-1 status ({split_col}):")
        if not np.isnan(model_real_rmse):
            baseline_part = (f", baseline RMSE {real_baseline_rmse:.2f} (n_comparable={int(real_comparable.sum()):,})"
                              if not np.isnan(real_baseline_rmse) else ", baseline: not enough comparable rows")
            print(f"    real targets      (n_total={int(is_real.sum()):,}, n_comparable={int(real_comparable.sum()):,}): "
                  f"model RMSE {model_real_rmse:.2f}{baseline_part}")
        else:
            print("    real targets: not enough test rows to report")
        if not np.isnan(model_est_rmse):
            baseline_part = (f", baseline RMSE {est_baseline_rmse:.2f} (n_comparable={int(est_comparable.sum()):,})"
                              if not np.isnan(est_baseline_rmse) else ", baseline: not enough comparable rows")
            print(f"    estimated targets (n_total={int(is_est.sum()):,}, n_comparable={int(est_comparable.sum()):,}): "
                  f"model RMSE {model_est_rmse:.2f}{baseline_part}")
        else:
            print("    estimated targets: not enough test rows to report")
        if not (np.isnan(model_real_rmse) or np.isnan(model_est_rmse)):
            gap = model_est_rmse - model_real_rmse
            print(f"    gap (estimated − real): {gap:+.2f} — "
                  f"{'estimated targets look WORSE, consistent with propagated Model-1 error' if gap > 0 else 'estimated targets look no worse (or better — see caveat above, verify before trusting this)'}")

    test_out = test[["cell_id", "timestamp"]].copy()
    test_out["actual_aqi"] = yte.values
    test_out["predicted_aqi"] = pred
    test_out["persistence_baseline"] = test["persistence_baseline"].values
    test_out["horizon_hours"] = horizon
    if split_col:
        test_out["target_is_estimated"] = test[split_col].values

    summary = {
        "horizon_hours": horizon, "model_rmse": round(model_rmse, 2),
        "model_rmse_comparable": round(model_rmse_comparable, 2) if not np.isnan(model_rmse_comparable) else None,
        "baseline_rmse": round(baseline_rmse, 2) if not np.isnan(baseline_rmse) else None,
        "improvement_pct": round(improvement, 1) if not np.isnan(improvement) else None,
        "n_baseline_comparable": int(valid_baseline.sum()),
        "model_rmse_real_cells": round(model_real_rmse, 2) if not np.isnan(model_real_rmse) else None,
        "model_rmse_estimated_cells": round(model_est_rmse, 2) if not np.isnan(model_est_rmse) else None,
        "baseline_rmse_real_cells": round(real_baseline_rmse, 2) if not np.isnan(real_baseline_rmse) else None,
        "baseline_rmse_estimated_cells": round(est_baseline_rmse, 2) if not np.isnan(est_baseline_rmse) else None,
        "improvement_pct_real_cells": (
            round((1 - model_real_rmse / real_baseline_rmse) * 100, 1)
            if not (np.isnan(model_real_rmse) or np.isnan(real_baseline_rmse)) and real_baseline_rmse > 0
            else None
        ),
        "improvement_pct_estimated_cells": (
            round((1 - model_est_rmse / est_baseline_rmse) * 100, 1)
            if not (np.isnan(model_est_rmse) or np.isnan(est_baseline_rmse)) and est_baseline_rmse > 0
            else None
        ),
        "n_real_comparable": int(real_comparable.sum()) if split_col else 0,
        "n_estimated_comparable": int(est_comparable.sum()) if split_col else 0,
        "n_train": len(train), "n_test": len(test),
    }
    return model, summary, test_out


def main():
    print(f"\n{'='*60}\n  AQI Forecaster — PS5 Section 8 (Model 2)\n"
          f"  {'XGBoost' if HAVE_XGB else 'RandomForest (xgboost not installed)'}\n{'='*60}\n")

    print("[1/3] Loading + merging real+estimated AQI (Model 1 output) with feature columns")
    df = load_and_merge()

    print("\n[2/3] Recomputing lag/rolling features on the FULL AQI series")
    df = add_full_lag_features(df)

    print("\n[3/3] Training + validating per horizon (24h / 48h / 72h)")
    summary_rows, all_predictions = [], []
    for horizon in HORIZONS_HOURS:
        result = train_and_validate_horizon(df, horizon)
        if result is None:
            continue
        model, summary, test_out = result
        summary_rows.append(summary)
        all_predictions.append(test_out)

        model_path = os.path.join(
            DATA_DIR, f"forecaster_{horizon}h." + ("json" if HAVE_XGB else "pkl")
        )
        if HAVE_XGB:
            model.save_model(model_path)
        else:
            import joblib
            joblib.dump(model, model_path)
        print(f"  → saved model: {model_path}")

    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        summary_path = os.path.join(DATA_DIR, "forecaster_validation_summary.csv")
        summary_df.to_csv(summary_path, index=False)
        print(f"\n{'='*60}\n  ✓ Validation summary saved: {summary_path}\n")
        print(summary_df.to_string(index=False))
    else:
        print("\n[FAIL] No horizon had enough data to train. You likely need a longer "
              "date range from fetch_real_data.py — even 72h-ahead needs 3+ days of "
              "real history before a single row can have a valid target.")
        return

    if all_predictions:
        preds_df = pd.concat(all_predictions, ignore_index=True)
        preds_path = os.path.join(DATA_DIR, "forecast_predictions.csv")
        preds_df.to_csv(preds_path, index=False)
        print(f"\n  ✓ Test-set predictions saved: {preds_path}")

    print(f"\n{'='*60}\n"
          f"  This is your accuracy story for the pitch:\n"
          f"    '24h/48h/72h AQI forecast — RMSE X vs persistence-baseline RMSE Y, Z% better.'\n"
          f"  For the trustworthy number specifically, cite model_rmse_real_cells, not the\n"
          f"  combined RMSE (see the real-vs-estimated split printed above).\n"
          f"\n  HONESTY CAVEAT (say this if asked): target_wind_speed/target_temp/etc. are\n"
          f"  historical-ACTUAL weather at t+horizon, used during training as a stand-in\n"
          f"  for what a forecast would supply — correct for backtesting, but at live\n"
          f"  inference on real future hours you MUST swap these for Open-Meteo's forecast\n"
          f"  endpoint (not archive), or the model is being fed data it will never have in\n"
          f"  production. Target-time calendar features (season/hour/weekday/festival) have\n"
          f"  no such caveat — the calendar 24h from now needs no forecasting.\n{'='*60}\n")


if __name__ == "__main__":
    main()
