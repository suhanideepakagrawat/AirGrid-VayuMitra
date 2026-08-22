"""Regenerate the 24/48/72 h forecast from current data — the staleness fix.

The served forecast was a single frozen run (2026-07-12), presented by the UI as
though it were current. This script re-runs the SAME trained models on today's
station readings, so the forecast layer can carry a recent timestamp and the date on
screen becomes a selling point instead of a confession.

WHAT THIS IS AND IS NOT
-----------------------
This is **inference, not training**. `models/spatial_estimator.json` and
`models/forecaster_{24,48,72}h.json` are used exactly as trained in July; nothing is
refitted. That matters for honesty (the validation numbers still describe the models
being used) and for practicality (a refresh is minutes, not hours).

WHY THIS IS FAR CHEAPER THAN THE ORIGINAL PIPELINE
--------------------------------------------------
`ml_pipeline/fetch_real_data.py` pulls years of history because it is building a
*training set*. Inference only needs enough history to fill the longest lookback
feature — `aqi_roll_mean_7d`, i.e. seven days. We fetch ten for headroom.

Two audit findings make it cheaper still:

* All five land-use features are **unused by every model** (0 splits across 250 trees
  each), because `data/landuse_static.csv` never existed and they trained as NaN.
  The same applies to the two satellite features in the spatial estimator. We pass
  NaN and reproduce training conditions exactly.
* Weather is city-level in the original pipeline, so it stays city-level here.

SAFETY
------
Writes to `data/future_aqi_forecast_ward.NEW.csv` and never touches the live file.
`--promote` swaps it in only after the sanity gates below pass, keeping a timestamped
backup. A bad run should be discarded, not shipped.

    python scripts/refresh_forecast.py                # generate + validate
    python scripts/refresh_forecast.py --promote      # ...and swap it in
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from advisory import openaq  # noqa: E402
from advisory.live import AQI_BP, sub_index  # noqa: E402

DATA = REPO / "data"
MODELS = REPO / "models"
LIVE_FILE = DATA / "future_aqi_forecast_ward.csv"
NEW_FILE = DATA / "future_aqi_forecast_ward.NEW.csv"
# What the advisory actually reads: config/city.yaml points `data_file` here, and
# LIVE_FILE only supplies ward names for the join. Refreshing one without the other
# leaves every ward AQI on screen stale.
ATTR_FILE = DATA / "source_attribution.csv"
# The immutable 1,600-cell / 209-ward grid, extracted once from the original pipeline
# output. Read the grid from here and never from LIVE_FILE: LIVE_FILE is what this
# script overwrites, so sourcing the grid from it means any run that loses cells
# permanently shrinks the grid for every run after it. One thin reporting hour
# silently took the product from 209 wards to 198 that way.
GRID_FILE = DATA / "cell_grid.csv"

HISTORY_DAYS = 10
HORIZONS = [24, 48, 72]

# How far back to look for the best-covered source hour. Wide enough that one thin
# reporting hour cannot force a low-coverage run, narrow enough to stay current.
SOURCE_SEARCH_HOURS = 48

# The grid the product states everywhere. Gated against this ABSOLUTE number rather
# than against the previous run: comparing to "last time" ratchets downward, and a
# run that quietly shipped 198 wards was accepted precisely because the run before it
# had already slipped to 198.
EXPECTED_WARDS = 209
MIN_WARDS = 205

CITY_LAT, CITY_LON = 28.65, 77.10

FESTIVAL_MONTHS_DAYS = {(10, 24), (10, 25), (11, 1), (11, 12), (11, 13)}
WINTER_MONTHS = {11, 12, 1, 2}
SUMMER_MONTHS = {4, 5, 6}
CROP_BURNING_MONTHS = {10, 11}

SPATIAL_FEATURES = [
    "wind_speed", "wind_dir", "temp", "humidity",
    "no2_satellite", "aod_satellite",
    "industrial_pct", "construction_pct", "green_cover_pct", "residential_pct", "water_pct",
    "month", "hour", "weekday", "is_winter", "is_summer", "is_crop_burn", "is_festival",
    "proxy_dist_km", "proxy_true_aqi",
    "proxy_pm25", "proxy_pm10", "proxy_no2", "proxy_so2", "proxy_o3", "proxy_co",
]

FORECAST_FEATURES = [
    "aqi_lag_1h", "aqi_lag_6h", "aqi_lag_12h", "aqi_lag_24h", "aqi_lag_48h",
    "aqi_roll_mean_24h", "aqi_roll_mean_7d", "aqi_prev_day_max",
    "wind_speed", "wind_dir", "temp", "humidity",
    "target_wind_speed", "target_wind_dir", "target_temp", "target_humidity",
    "industrial_pct", "construction_pct", "green_cover_pct", "residential_pct", "water_pct",
    "nearest_dist_km", "is_estimated",
    "target_month", "target_hour", "target_weekday",
    "target_is_winter", "target_is_summer", "target_is_crop_burn", "target_is_festival",
]

# Never used by any model (all NaN during training) — passed as NaN, not invented.
DEAD_FEATURES = ["industrial_pct", "construction_pct", "green_cover_pct",
                 "residential_pct", "water_pct", "no2_satellite", "aod_satellite"]


def log(msg: str) -> None:
    print(msg, flush=True)


def _predict(model_path: Path, X: pd.DataFrame) -> np.ndarray:
    """Run a saved XGBoost model without pulling in scikit-learn.

    The models were saved from XGBRegressor, but the sklearn wrapper refuses to even
    construct without scikit-learn installed. The native Booster loads the identical
    JSON and predicts identically, so the script keeps a light dependency footprint.
    """
    import xgboost as xgb

    booster = xgb.Booster()
    booster.load_model(str(model_path))
    dm = xgb.DMatrix(X.astype("float32"), feature_names=list(X.columns))
    return booster.predict(dm)


# ─── 1. Cell geometry, reused from the committed pipeline output ─────────────

def load_cells() -> pd.DataFrame:
    """cell_id, lat, lon, nearest_dist_km, is_estimated, ward — from the live file.

    Geography does not change between runs, so there is no reason to recompute the
    grid or redo the ward join; only the *values* are being refreshed.
    """
    src = GRID_FILE if GRID_FILE.exists() else LIVE_FILE
    df = pd.read_csv(src, usecols=["cell_id", "lat", "lon", "nearest_dist_km",
                                   "is_estimated", "Ward_No", "Ward_Name",
                                   "distance_to_ward"])
    cells = df.drop_duplicates("cell_id").reset_index(drop=True)
    log(f"[1/6] {len(cells)} cells loaded from the committed grid")
    return cells


# ─── 2. Station history ──────────────────────────────────────────────────────

def fetch_station_history() -> pd.DataFrame:
    """Hourly pollutant history for every live Delhi station.

    Long format: station_id, lat, lon, timestamp, pollutant, value(ug/m3).
    """
    locs = openaq.fetch_locations()
    if not locs:
        raise SystemExit("No live OpenAQ stations — is OPENAQ_API_KEY set?")

    since = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    jobs = []
    for loc in locs:
        for sensor_id, (name, factor) in loc["sensors"].items():
            if name in ("pm25", "pm10", "no2", "so2", "o3"):
                jobs.append((loc, name, sensor_id, factor))

    rows: list[dict] = []

    def _one(job):
        loc, name, sensor_id, factor = job
        data = openaq._get(f"/sensors/{sensor_id}/hours",
                           {"datetime_from": since, "limit": 500})
        out = []
        for r in (data or {}).get("results", []):
            v = r.get("value")
            if v is None or float(v) < 0:
                continue
            ts = ((r.get("period") or {}).get("datetimeFrom") or {}).get("utc")
            if not ts:
                continue
            out.append({"station_id": loc["id"], "lat": loc["lat"], "lon": loc["lon"],
                        "timestamp": ts, "pollutant": name,
                        "value": float(v) * factor})
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for chunk in pool.map(_one, jobs):
            rows.extend(chunk)

    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("No station history returned.")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.floor("h")
    df = df.groupby(["station_id", "lat", "lon", "timestamp", "pollutant"],
                    as_index=False)["value"].median()
    log(f"[2/6] {len(df):,} station-hour-pollutant readings from "
        f"{df.station_id.nunique()} stations")
    return df


def station_hourly_wide(long: pd.DataFrame) -> pd.DataFrame:
    """One row per station-hour with a column per pollutant, plus its CPCB AQI."""
    wide = long.pivot_table(index=["station_id", "lat", "lon", "timestamp"],
                            columns="pollutant", values="value").reset_index()
    wide.columns.name = None
    for p in ("pm25", "pm10", "no2", "so2", "o3", "co"):
        if p not in wide.columns:
            wide[p] = np.nan

    def _aqi(row):
        subs = [sub_index(row[p], p) for p in AQI_BP if pd.notna(row.get(p))]
        subs = [s for s in subs if s is not None]
        return max(subs) if subs else np.nan

    wide["true_aqi"] = wide.apply(_aqi, axis=1)
    wide = wide.dropna(subset=["true_aqi"])
    log(f"        {len(wide):,} station-hours with a computable CPCB AQI")
    return wide


# ─── 3. Weather, city-level (as the original pipeline did) ───────────────────

def fetch_weather(start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """Hourly weather covering history and the forecast horizon. Keyless."""
    import json
    import urllib.parse
    import urllib.request

    def _get(url: str) -> dict:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r)

    hourly = "temperature_2m,relativehumidity_2m,windspeed_10m,winddirection_10m"
    q = urllib.parse.urlencode({
        "latitude": CITY_LAT, "longitude": CITY_LON, "hourly": hourly,
        "past_days": min(92, HISTORY_DAYS + 2), "forecast_days": 5,
        "timezone": "UTC",
    })
    d = _get(f"https://api.open-meteo.com/v1/forecast?{q}")
    h = d["hourly"]
    wx = pd.DataFrame({
        "timestamp": pd.to_datetime(h["time"], utc=True),
        "temp": h["temperature_2m"],
        "humidity": h["relativehumidity_2m"],
        "wind_speed": h["windspeed_10m"],
        "wind_dir": h["winddirection_10m"],
    }).dropna()
    log(f"[3/6] {len(wx)} hours of weather ({wx.timestamp.min()} -> {wx.timestamp.max()})")
    return wx


# ─── 4. Spatial estimation: station readings -> every cell ───────────────────

K_NEAREST = 4


def _nearest_station(cells: pd.DataFrame, stations: pd.DataFrame,
                     k: int = 1) -> pd.DataFrame:
    """For each cell, its k closest stations with distances (rank 0 = nearest).

    k > 1 exists because stations report intermittently. Binding a cell to its single
    nearest station means that whenever that one instrument misses an hour, the cell
    has no row at all — which is how a run ended up covering 1,579 of 1,600 cells and
    dropping eleven wards. With a few candidates we can fall back to the next-nearest
    station that actually reported.
    """
    slat = np.radians(stations["lat"].to_numpy())
    slon = np.radians(stations["lon"].to_numpy())
    clat = np.radians(cells["lat"].to_numpy())[:, None]
    clon = np.radians(cells["lon"].to_numpy())[:, None]
    dlat, dlon = slat - clat, slon - clon
    a = np.sin(dlat / 2) ** 2 + np.cos(clat) * np.cos(slat) * np.sin(dlon / 2) ** 2
    dist = 2 * 6371.0 * np.arcsin(np.sqrt(a))
    k = min(k, dist.shape[1])
    order = np.argsort(dist, axis=1)[:, :k]
    frames = []
    for rank in range(k):
        idx = order[:, rank]
        frames.append(pd.DataFrame({
            "cell_id": cells["cell_id"].to_numpy(),
            "proxy_station": stations["station_id"].to_numpy()[idx],
            "proxy_dist_km": dist[np.arange(len(cells)), idx],
            "proxy_rank": rank,
        }))
    return pd.concat(frames, ignore_index=True)


def estimate_cell_aqi(cells: pd.DataFrame, wide: pd.DataFrame,
                      wx: pd.DataFrame) -> pd.DataFrame:
    """Run the trained spatial estimator for every cell at every hour."""
    station_pos = wide[["station_id", "lat", "lon"]].drop_duplicates("station_id")
    link = _nearest_station(cells, station_pos, k=K_NEAREST)

    proxy = wide.rename(columns={
        "true_aqi": "proxy_true_aqi", "pm25": "proxy_pm25", "pm10": "proxy_pm10",
        "no2": "proxy_no2", "so2": "proxy_so2", "o3": "proxy_o3", "co": "proxy_co",
        "station_id": "proxy_station",
    })[["proxy_station", "timestamp", "proxy_true_aqi", "proxy_pm25", "proxy_pm10",
        "proxy_no2", "proxy_so2", "proxy_o3", "proxy_co"]]

    grid = link.merge(proxy, on="proxy_station", how="inner")
    # One row per cell-hour: keep the closest candidate that actually reported.
    grid = (grid.sort_values(["cell_id", "timestamp", "proxy_rank"])
                .drop_duplicates(["cell_id", "timestamp"], keep="first")
                .drop(columns=["proxy_rank"]))
    grid = grid.merge(cells[["cell_id", "lat", "lon"]], on="cell_id", how="left")
    grid = grid.merge(wx, on="timestamp", how="left")

    ts = grid["timestamp"].dt
    grid["month"], grid["hour"], grid["weekday"] = ts.month, ts.hour, ts.dayofweek
    grid["is_winter"] = grid["month"].isin(WINTER_MONTHS).astype(int)
    grid["is_summer"] = grid["month"].isin(SUMMER_MONTHS).astype(int)
    grid["is_crop_burn"] = grid["month"].isin(CROP_BURNING_MONTHS).astype(int)
    grid["is_festival"] = [1 if (m, d) in FESTIVAL_MONTHS_DAYS else 0
                           for m, d in zip(grid["month"], ts.day)]
    for c in DEAD_FEATURES:
        grid[c] = np.nan

    grid["aqi"] = _predict(MODELS / "spatial_estimator.json",
                           grid[SPATIAL_FEATURES]).clip(0, 500)

    out = grid[["cell_id", "lat", "lon", "timestamp", "aqi", "proxy_dist_km"]].copy()
    log(f"[4/6] estimated {len(out):,} cell-hours "
        f"({out.cell_id.nunique()} cells x {out.timestamp.nunique()} hours)")
    return out


# ─── 5. Lag features + forecast ──────────────────────────────────────────────

def build_lags(df: pd.DataFrame) -> pd.DataFrame:
    """Exact-timestamp lags and gap-safe rolling stats.

    Mirrors ml_pipeline/predict_future_aqi.py deliberately: a missing hour must stay
    NaN rather than silently shifting to a wrong row, and rolling windows are computed
    on a continuous hourly index so gaps cannot corrupt the window size. Any drift
    from that method would feed the models something they were not trained on.
    """
    df = df.drop_duplicates(["cell_id", "timestamp"]).sort_values(["cell_id", "timestamp"])
    lookup = df.set_index(["cell_id", "timestamp"])["aqi"].sort_index()

    for h in (1, 6, 12, 24, 48):
        keys = pd.MultiIndex.from_arrays(
            [df["cell_id"], df["timestamp"] - pd.Timedelta(hours=h)])
        df[f"aqi_lag_{h}h"] = lookup.reindex(keys).to_numpy(dtype="float32")

    frames = []
    for cid, grp in df.groupby("cell_id"):
        s = grp.set_index("timestamp")["aqi"].sort_index()
        idx = pd.date_range(s.index.min(), s.index.max(), freq="h")
        sf = s.reindex(idx)
        frames.append(pd.DataFrame({
            "cell_id": cid, "timestamp": idx,
            "aqi_roll_mean_24h": sf.rolling(24, min_periods=6).mean().values,
            "aqi_roll_mean_7d": sf.rolling(168, min_periods=24).mean().values,
            "aqi_prev_day_max": sf.rolling(24, min_periods=6).max().shift(24).values,
        }))
    roll = pd.concat(frames, ignore_index=True).set_index(["cell_id", "timestamp"]).sort_index()
    keys = pd.MultiIndex.from_arrays([df["cell_id"], df["timestamp"]])
    for c in ("aqi_roll_mean_24h", "aqi_roll_mean_7d", "aqi_prev_day_max"):
        df[c] = roll[c].reindex(keys).to_numpy(dtype="float32")
    return df


def run_forecast(cell_hours: pd.DataFrame, cells: pd.DataFrame,
                 wx: pd.DataFrame) -> pd.DataFrame:
    """Pick the best source hour, then predict +24/48/72 h for every cell."""
    lagged = build_lags(cell_hours)

    # Source hour = the most recent hour with broad CELL coverage.
    #
    # Deliberately matches ml_pipeline/predict_future_aqi.py, which selects on cell
    # count alone (MIN_CELLS) and does not require every lag to be present. An
    # earlier version here also demanded non-null lags, which cost 62 cells and
    # dropped 16 wards off the map entirely — a visible regression against the "209
    # wards" the product states everywhere. XGBoost learned default split directions
    # for missing values during training, so a NaN lag is handled exactly as it was
    # then; filtering those rows out is stricter than the model was ever trained to
    # expect, and pays for it in coverage.
    coverage = lagged.groupby("timestamp")["cell_id"].nunique()
    if coverage.empty:
        raise SystemExit("No cell-hours available — history too short.")

    # Prefer FULL coverage over the very latest hour.
    #
    # Taking the newest hour above a 95% threshold looked fine in one run and then
    # quietly shipped 198 wards in the next, because an hour with 1,520 of 1,600
    # cells clears 95% while losing eleven wards off the map. "209 wards" is a
    # headline number stated across the product, so it must not wobble run to run
    # for the sake of a couple of hours' recency. Among the most recent day, take the
    # best coverage available and break ties by recency.
    recent = coverage[coverage.index >= coverage.index.max() - pd.Timedelta(hours=SOURCE_SEARCH_HOURS)]
    best = recent.max()
    source_ts = recent[recent == best].index.max()
    complete = lagged[(lagged["timestamp"] == source_ts)
                      & lagged["aqi_lag_24h"].notna()]["cell_id"].nunique()
    log(f"[5/6] source hour {source_ts} ({coverage.loc[source_ts]} cells, "
        f"{complete} with a full 24h lag; best coverage {coverage.max()})")

    src = lagged[lagged["timestamp"] == source_ts].copy()
    src = src.merge(cells[["cell_id", "nearest_dist_km", "is_estimated",
                           "Ward_No", "Ward_Name", "distance_to_ward"]],
                    on="cell_id", how="left")
    src = src.merge(wx, on="timestamp", how="left")
    for c in DEAD_FEATURES:
        src[c] = np.nan

    wx_idx = wx.set_index("timestamp")
    results = []
    for horizon in HORIZONS:
        target_ts = source_ts + pd.Timedelta(hours=horizon)
        if target_ts not in wx_idx.index:
            raise SystemExit(f"No forecast weather for {target_ts}.")
        tw = wx_idx.loc[target_ts]
        frame = src.copy()
        frame["target_wind_speed"] = tw["wind_speed"]
        frame["target_wind_dir"] = tw["wind_dir"]
        frame["target_temp"] = tw["temp"]
        frame["target_humidity"] = tw["humidity"]
        frame["target_month"] = target_ts.month
        frame["target_hour"] = target_ts.hour
        frame["target_weekday"] = target_ts.dayofweek
        frame["target_is_winter"] = int(target_ts.month in WINTER_MONTHS)
        frame["target_is_summer"] = int(target_ts.month in SUMMER_MONTHS)
        frame["target_is_crop_burn"] = int(target_ts.month in CROP_BURNING_MONTHS)
        frame["target_is_festival"] = int((target_ts.month, target_ts.day)
                                          in FESTIVAL_MONTHS_DAYS)

        pred = _predict(MODELS / f"forecaster_{horizon}h.json",
                        frame[FORECAST_FEATURES]).clip(0, 500)

        out = frame[["cell_id", "lat", "lon", "is_estimated", "nearest_dist_km",
                     "Ward_No", "Ward_Name", "distance_to_ward"]].copy()
        out["nearest_station"] = "OpenAQ live network"
        out["source_timestamp"] = source_ts.tz_localize(None)
        out["target_timestamp"] = target_ts.tz_localize(None)
        out["horizon_hours"] = horizon
        out["forecast_aqi"] = np.round(pred, 1)
        out["source_aqi"] = frame["aqi"].to_numpy()
        results.append(out)
        log(f"        +{horizon}h -> {len(out)} cells, "
            f"AQI {out.forecast_aqi.min():.0f}-{out.forecast_aqi.max():.0f} "
            f"(mean {out.forecast_aqi.mean():.1f})")

    return pd.concat(results, ignore_index=True)


# ─── 6. Sanity gates ─────────────────────────────────────────────────────────

def validate(new: pd.DataFrame, old: pd.DataFrame) -> list[str]:
    """Refuse to promote a run that looks wrong. Returns a list of failures."""
    fails = []
    cells_new = new.cell_id.nunique()
    cells_old = old.cell_id.nunique()
    if cells_new < cells_old * 0.7:
        fails.append(f"cell coverage collapsed: {cells_new} vs {cells_old}")
    if sorted(new.horizon_hours.unique()) != HORIZONS:
        fails.append(f"horizons wrong: {sorted(new.horizon_hours.unique())}")
    mean = new.forecast_aqi.mean()
    if not (20 <= mean <= 450):
        fails.append(f"implausible mean AQI {mean:.1f}")
    if new.forecast_aqi.isna().any():
        fails.append("NaN forecasts present")
    if new.forecast_aqi.nunique() < 20:
        fails.append(f"forecast nearly constant ({new.forecast_aqi.nunique()} values)")
    if new.Ward_Name.isna().mean() > 0.35:
        fails.append(f"{new.Ward_Name.isna().mean():.0%} of rows have no ward")
    wards_new = new.Ward_Name.nunique()
    if wards_new < MIN_WARDS:
        fails.append(f"only {wards_new} wards, expected ~{EXPECTED_WARDS} "
                     f"(the product states {EXPECTED_WARDS} everywhere)")
    age_h = (dt.datetime.now(dt.timezone.utc)
             - pd.Timestamp(new.source_timestamp.iloc[0]).tz_localize("UTC")
             ).total_seconds() / 3600
    if age_h > 48:
        fails.append(f"source hour is {age_h:.0f}h old — not a fresh run")
    return fails


def refresh_attribution_aqi(fresh: pd.DataFrame) -> str:
    """Carry the new AQI into source_attribution.csv, which is what /wards serves.

    Refreshing the forecast file alone leaves every ward AQI on screen exactly as
    stale as before — the metadata would read "issued 2 hours ago" over July's
    numbers, which is worse than saying nothing at all.

    ONLY the AQI-derived columns are rewritten: forecast_aqi, aqi_severity, and the
    low-AQI branch of attribution_status. The source split itself (dominant_source,
    the percentages, confidence) is left untouched, because regenerating it means
    re-running the Colab attribution notebook against OSM extracts.

    KNOWN LIMITATION, stated rather than hidden: those percentages were computed with
    July's forecast wind. Local geography dominates the score at low wind speeds so
    the ranking is broadly stable, but the upwind-corridor component is not refreshed
    here. The live pollutant fingerprints in advisory/fingerprints.py are the current
    source evidence.
    """
    attr = pd.read_csv(ATTR_FILE)
    before = attr["forecast_aqi"].mean()

    new_aqi = (fresh.groupby(["cell_id", "horizon_hours"])["forecast_aqi"]
               .mean().rename("new_aqi"))
    merged = attr.merge(new_aqi, on=["cell_id", "horizon_hours"], how="left")
    updated = int(merged["new_aqi"].notna().sum())
    merged["forecast_aqi"] = merged["new_aqi"].fillna(merged["forecast_aqi"]).round(1)
    merged = merged.drop(columns=["new_aqi"])

    from advisory.health_bands import band_for_aqi
    merged["aqi_severity"] = [band_for_aqi(a).label_en for a in merged["forecast_aqi"]]

    # attribution_status has exactly one AQI-dependent branch; the geospatial
    # verdicts ("Insufficient geospatial source evidence") are left alone.
    low = "Low AQI - source attribution not operationally important"
    was_low = merged["attribution_status"] == low
    now_low = merged["forecast_aqi"] < 100
    merged.loc[was_low & ~now_low, "attribution_status"] = "Attributed"
    merged.loc[~was_low & now_low, "attribution_status"] = low

    merged.to_csv(ATTR_FILE, index=False)
    return (f"attribution AQI refreshed: {updated}/{len(attr)} rows, "
            f"mean {before:.1f} -> {merged['forecast_aqi'].mean():.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Regenerate the forecast from live data")
    ap.add_argument("--promote", action="store_true",
                    help="replace the served forecast if every sanity gate passes")
    args = ap.parse_args()

    if not openaq.available():
        raise SystemExit("OPENAQ_API_KEY is not set.")

    cells = load_cells()
    long = fetch_station_history()
    wide = station_hourly_wide(long)
    wx = fetch_weather(wide.timestamp.min(), wide.timestamp.max())
    cell_hours = estimate_cell_aqi(cells, wide, wx)
    out = run_forecast(cell_hours, cells, wx)

    out.to_csv(NEW_FILE, index=False)
    log(f"[6/6] wrote {NEW_FILE.name} ({len(out):,} rows)")

    old = pd.read_csv(LIVE_FILE)
    fails = validate(out, old)
    log("")
    log("SANITY GATES")
    if fails:
        for f in fails:
            log(f"   FAIL  {f}")
        log("\nNot promoting. The served forecast is untouched.")
        raise SystemExit(1)
    log("   all gates passed")

    old_ts = pd.read_csv(LIVE_FILE, usecols=["source_timestamp"]).source_timestamp.iloc[0]
    log(f"\n   served run : {old_ts}  (mean AQI {old.forecast_aqi.mean():.1f})")
    log(f"   new run    : {out.source_timestamp.iloc[0]}  "
        f"(mean AQI {out.forecast_aqi.mean():.1f})")

    if not args.promote:
        log("\nDry run. Re-run with --promote to swap it in.")
        return

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for src in (LIVE_FILE, ATTR_FILE):
        shutil.copy2(src, DATA / f"{src.stem}.{stamp}.bak.csv")
    shutil.move(str(NEW_FILE), str(LIVE_FILE))
    note = refresh_attribution_aqi(pd.read_csv(LIVE_FILE))
    log(f"\nPromoted (backups stamped {stamp})")
    log(f"   {note}")


if __name__ == "__main__":
    main()
