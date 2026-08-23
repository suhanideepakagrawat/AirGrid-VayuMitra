"""
fetch_real_data.py  —  REAL DATA ONLY (no synthetic reconstruction)
=====================================================================
Builds the training dataset for the Hyperlocal Predictive AQI Forecasting
Agent (PS5 MUST-have feature), using only measured, timestamped data.

RESILIENCE + SPEED NOTES
----------------------------------------------------------
A single multi-year request per sensor can hit a slow window deep into
pagination and time out (HTTP 408). So every sensor's history is fetched
MONTH BY MONTH, cached to disk immediately, and a sensor/month that still
fails after retries is skipped rather than crashing the whole run.

Sensors are also fetched IN PARALLEL (ThreadPoolExecutor) since this is
network I/O bound, not CPU bound — waiting for one sensor's response while
doing nothing else is wasted time. Concurrency is capped (--max-workers,
default 6) and every request goes through a shared rate limiter so more
workers doesn't mean hammering OpenAQ past its actual limits — it just
means less idle waiting between requests that were going to happen anyway.

Duplicate sensors (same station reporting the same pollutant from more than
one sensor ID — happens on OpenAQ occasionally) are deduped down to the one
with the longest real reporting history, cutting redundant requests.

API keys required (env vars — never hardcode):
  export OPENAQ_API_KEY="your_key"     # free: https://explore.openaq.org
  # Google Earth Engine (optional, for satellite):
  #   pip install earthengine-api && earthengine authenticate
"""

import os, sys, math, time, argparse, threading
import concurrent.futures
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, date, timezone

# ─────────────────────────────────────────────────────────────────────────────
# CITY CONFIG
# ─────────────────────────────────────────────────────────────────────────────
CITY = {
    "name":    "Delhi",
    "lat_min": 28.40,
    "lat_max": 28.90,
    "lon_min": 76.80,
    "lon_max": 77.40,
    "lat_center": 28.65,
    "lon_center": 77.10,
}

N_CELLS_X = 40
N_CELLS_Y = 40

DEFAULT_START = date(2023, 1, 1)
DEFAULT_END   = date.today()

OPENAQ_API_KEY = os.environ.get("OPENAQ_API_KEY", "")
OPENAQ_BASE    = "https://api.openaq.org/v3"

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(OUT_DIR, exist_ok=True)

MONTHLY_CACHE_DIR = os.path.join(OUT_DIR, "openaq_monthly_cache")
os.makedirs(MONTHLY_CACHE_DIR, exist_ok=True)

POLLUTANT_PARAMS = {"pm25", "pm10", "no2", "so2", "o3", "co"}  # OpenAQ parameter names

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 5


class _RateLimiter:
    """
    Shared across all worker threads via the module-level instance below.

    OpenAQ's documented limit is ~60 requests/minute (their own SDKs report
    x_ratelimit_limit=60 in practice) — NOT the 8/sec this used to assume,
    which is why 8 parallel workers immediately triggered a 429 storm.

    Rather than hardcode another guess, this reads the REAL rate-limit
    headers OpenAQ sends with every response (x-ratelimit-remaining,
    x-ratelimit-reset) and throttles based on what the server actually
    reports — the same approach OpenAQ's official Python/R SDKs use. A
    conservative fixed pacing (~1.1s between requests) is used as a
    fallback before the first response's headers are known.
    """
    def __init__(self, fallback_min_interval: float = 1.1):
        self.lock = threading.Lock()
        self.remaining = None
        self.reset_at = None
        self.fallback_min_interval = fallback_min_interval
        self.last_call = 0.0

    def before_request(self):
        with self.lock:
            now = time.time()
            # Server told us we're nearly out — wait for its actual reset window
            if self.remaining is not None and self.remaining <= 1 and self.reset_at:
                sleep_for = max(0.0, self.reset_at - now) + 0.5
                if sleep_for > 0:
                    print(f"      [rate limit] {self.remaining} requests left — "
                          f"waiting {sleep_for:.1f}s for OpenAQ's own reset window")
                    time.sleep(sleep_for)
                    now = time.time()
            # Fallback pacing so multiple threads don't burst before we've
            # seen any real headers yet
            elapsed = now - self.last_call
            if elapsed < self.fallback_min_interval:
                time.sleep(self.fallback_min_interval - elapsed)
            self.last_call = time.time()

    def update_from_headers(self, headers):
        with self.lock:
            try:
                remaining = headers.get("x-ratelimit-remaining")
                reset = headers.get("x-ratelimit-reset")
                if remaining is not None:
                    self.remaining = int(remaining)
                if reset is not None:
                    self.reset_at = time.time() + int(reset)
            except (TypeError, ValueError):
                pass  # missing/malformed headers — fallback pacing still applies


_rate_limiter = _RateLimiter()
_print_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# INDIA AQI  —  official CPCB sub-index formula
# ─────────────────────────────────────────────────────────────────────────────
AQI_BP = {
    "PM2.5": [(0,30,0,50),(30,60,51,100),(60,90,101,200),(90,120,201,300),(120,250,301,400),(250,500,401,500)],
    "PM10":  [(0,50,0,50),(50,100,51,100),(100,250,101,200),(250,350,201,300),(350,430,301,400),(430,600,401,500)],
    "NO2":   [(0,40,0,50),(40,80,51,100),(80,180,101,200),(180,280,201,300),(280,400,301,400),(400,800,401,500)],
    "SO2":   [(0,40,0,50),(40,80,51,100),(80,380,101,200),(380,800,201,300),(800,1600,301,400),(1600,2100,401,500)],
    "O3":    [(0,50,0,50),(50,100,51,100),(100,168,101,200),(168,208,201,300),(208,748,301,400),(748,1000,401,500)],
    "CO":    [(0,1,0,50),(1,2,51,100),(2,10,101,200),(10,17,201,300),(17,34,301,400),(34,50,401,500)],
}

def sub_index(conc, pollutant):
    for clo, chi, ilo, ihi in AQI_BP.get(pollutant, []):
        if clo <= conc <= chi:
            return ilo + (ihi - ilo) / (chi - clo) * (conc - clo)
    return None

def aqi_from_row(row) -> float:
    mapping = {"pm25": "PM2.5", "pm10": "PM10", "no2": "NO2", "so2": "SO2", "o3": "O3", "co": "CO"}
    sis = []
    for raw, poll in mapping.items():
        val = row.get(raw)
        if val is not None and not (isinstance(val, float) and math.isnan(val)):
            si = sub_index(float(val), poll)
            if si is not None:
                sis.append(si)
    return round(max(sis), 1) if sis else np.nan


# ─────────────────────────────────────────────────────────────────────────────
# OPENAQ HELPERS  (real data, with retry/backoff on transient failures)
# ─────────────────────────────────────────────────────────────────────────────
def _openaq_get(path: str, params: dict) -> dict:
    """
    Retries on:
      - 408 (request timeout — what killed the run you just hit)
      - 429 (rate limited)
      - 500/502/503/504 (server-side transient errors)
      - connection-level errors (DNS, reset, read timeout at the socket level)
    Exponential backoff, capped at 60s between attempts. Raises only after
    MAX_RETRIES is exhausted — the caller (fetch_sensor_hourly, per month)
    decides whether to skip or propagate.
    """
    if not OPENAQ_API_KEY:
        raise RuntimeError(
            "\n[ERROR] OPENAQ_API_KEY not set.\n"
            "  Get a free key at https://explore.openaq.org (sign up -> Settings -> API key)\n"
            "  export OPENAQ_API_KEY='your_key'\n"
        )
    url = f"{OPENAQ_BASE}{path}"
    headers = {"X-API-Key": OPENAQ_API_KEY}

    for attempt in range(MAX_RETRIES):
        _rate_limiter.before_request()
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
        except requests.exceptions.RequestException as e:
            wait = min(60, 2 ** attempt)
            print(f"      [connection error] {type(e).__name__} — retrying in {wait}s "
                  f"({attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        _rate_limiter.update_from_headers(resp.headers)

        if resp.status_code in RETRYABLE_STATUS:
            wait = 5 * (attempt + 1) if resp.status_code == 429 else min(60, 2 ** attempt)
            print(f"      [HTTP {resp.status_code}] retrying in {wait}s ({attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"OpenAQ request failed after {MAX_RETRIES} retries: {url}")


def fetch_openaq_locations(city: dict) -> pd.DataFrame:
    """Real CPCB station metadata inside the city bbox, incl. real reporting window."""
    print("  Fetching real station list from OpenAQ (bbox + India filter) ...")
    bbox = f"{city['lon_min']},{city['lat_min']},{city['lon_max']},{city['lat_max']}"
    rows, page = [], 1
    while True:
        data = _openaq_get("/locations", {"bbox": bbox, "iso": "IN", "limit": 100, "page": page})
        results = data.get("results", [])
        if not results:
            break
        for loc in results:
            coords = loc.get("coordinates") or {}
            for sensor in loc.get("sensors", []):
                param = (sensor.get("parameter") or {}).get("name", "")
                if param not in POLLUTANT_PARAMS:
                    continue
                rows.append({
                    "location_id":   loc["id"],
                    "station":       loc.get("name", f"loc_{loc['id']}"),
                    "provider":      (loc.get("provider") or {}).get("name", ""),
                    "latitude":      coords.get("latitude"),
                    "longitude":     coords.get("longitude"),
                    "sensor_id":     sensor["id"],
                    "parameter":     param,
                    "datetimeFirst": (loc.get("datetimeFirst") or {}).get("utc"),
                    "datetimeLast":  (loc.get("datetimeLast") or {}).get("utc"),
                })
        if len(results) < 100:
            break
        page += 1
        time.sleep(0.2)

    df = pd.DataFrame(rows).dropna(subset=["latitude", "longitude"])
    n_stations = df["location_id"].nunique()
    print(f"  → {n_stations} real stations, {len(df)} pollutant sensors "
          f"| providers: {sorted(df['provider'].unique().tolist())}")
    if n_stations == 0:
        raise RuntimeError(
            "[ERROR] OpenAQ returned 0 stations for this bbox. Check OPENAQ_API_KEY "
            "and that OpenAQ has coverage for this city (explore.openaq.org)."
        )
    return df


def dedupe_sensors(sensors_df: pd.DataFrame) -> pd.DataFrame:
    """
    Occasionally OpenAQ has more than one sensor ID reporting the same
    pollutant at the same station (e.g. an instrument swap that got a new
    sensor ID instead of continuing the old one). Downloading both is pure
    redundant request volume. Keep only the sensor with the longest real
    reporting span per (station, parameter) — real selection criterion,
    not arbitrary first-seen.
    """
    before = len(sensors_df)
    tmp = sensors_df.copy()
    tmp["_first"] = pd.to_datetime(tmp["datetimeFirst"], errors="coerce", utc=True)
    tmp["_last"] = pd.to_datetime(tmp["datetimeLast"], errors="coerce", utc=True)
    tmp["_span_days"] = (tmp["_last"] - tmp["_first"]).dt.days
    tmp = tmp.sort_values("_span_days", ascending=False)
    deduped = tmp.drop_duplicates(subset=["station", "parameter"], keep="first")
    deduped = deduped.drop(columns=["_first", "_last", "_span_days"]).reset_index(drop=True)
    after = len(deduped)
    if after < before:
        print(f"  → deduped sensors: {before} → {after} "
              f"(kept longest-history sensor per station+pollutant, dropped {before - after} redundant)")
    return deduped


def _month_chunks(start_ts: pd.Timestamp, end_ts: pd.Timestamp):
    """Yield (chunk_start, chunk_end) tz-aware Timestamp pairs covering
    [start_ts, end_ts) at calendar-month boundaries."""
    cur = pd.Timestamp(year=start_ts.year, month=start_ts.month, day=1, tz=start_ts.tzinfo)
    while cur < end_ts:
        nxt = cur + pd.offsets.MonthBegin(1)
        yield max(cur, start_ts), min(nxt, end_ts)
        cur = nxt


def _fetch_sensor_hourly_window(sensor_id: int, dt_from: str, dt_to: str) -> pd.DataFrame:
    """Paginated fetch for ONE window (should be ~1 month, kept small on purpose).
    Retries on transient failures happen inside _openaq_get; if a page still
    fails after all retries, the exception propagates up to the per-month
    caller in fetch_sensor_hourly, which skips just that month."""
    rows, page = [], 1
    while True:
        data = _openaq_get(f"/sensors/{sensor_id}/hours", {
            "datetime_from": dt_from, "datetime_to": dt_to, "limit": 1000, "page": page,
        })
        results = data.get("results", [])
        if not results:
            break
        for r in results:
            period = r.get("period") or {}
            dt_from_obj = (period.get("datetimeFrom") or {}).get("utc")
            val = r.get("value")
            if dt_from_obj is None or val is None:
                continue
            rows.append({"timestamp": dt_from_obj, "value": val})
        if len(results) < 1000:
            break
        page += 1
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame(columns=["timestamp", "value"])
    return pd.DataFrame(rows)


def fetch_sensor_hourly(sensor_id: int, win_from: pd.Timestamp, win_to: pd.Timestamp) -> pd.DataFrame:
    """
    Real hourly measurements for one sensor, fetched and cached MONTH BY
    MONTH. A month that fails after all retries is skipped — left as a real
    gap (honest), not invented, and doesn't take the rest of the sensor's
    history down with it. Already-cached months are loaded instantly on a
    re-run, so an interrupted multi-hour pull resumes cheaply.
    """
    frames = []
    for chunk_start, chunk_end in _month_chunks(win_from, win_to):
        cache_file = os.path.join(
            MONTHLY_CACHE_DIR, f"{sensor_id}_{chunk_start.strftime('%Y-%m')}.csv"
        )
        if os.path.exists(cache_file):
            try:
                cached = pd.read_csv(cache_file)
            except pd.errors.EmptyDataError:
                # Leftover from before this fix (or an interrupted write) —
                # a genuinely-empty month got cached with no header at all.
                # Treat as "0 real rows this month" and rewrite it properly
                # so this doesn't recur on the next run.
                cached = pd.DataFrame(columns=["timestamp", "value"])
                cached.to_csv(cache_file, index=False)
            if not cached.empty:
                frames.append(cached)
            continue
        try:
            month_df = _fetch_sensor_hourly_window(
                sensor_id, chunk_start.isoformat(), chunk_end.isoformat()
            )
        except Exception as e:
            print(f"      [skip month] sensor {sensor_id} {chunk_start.strftime('%Y-%m')} "
                  f"failed after {MAX_RETRIES} retries ({e}) — real gap, not invented")
            continue
        month_df.to_csv(cache_file, index=False)  # cache even if empty, so we don't retry a genuinely-empty month
        if not month_df.empty:
            frames.append(month_df)

    if not frames:
        return pd.DataFrame(columns=["timestamp", "value"])
    return pd.concat(frames, ignore_index=True)


def _process_one_sensor(i: int, total: int, srow, start: date, end: date):
    """One sensor's full fetch (all its months). Runs inside a worker thread.
    Returns (hourly_df_or_None, skipped_label_or_None) — never raises, so one
    bad sensor can never take down the ThreadPoolExecutor or the rest of the
    run."""
    try:
        real_first = pd.to_datetime(srow.datetimeFirst) if srow.datetimeFirst else None
        real_last  = pd.to_datetime(srow.datetimeLast) if srow.datetimeLast else None
        if real_first is None or real_last is None:
            return None, None
        win_from = max(pd.Timestamp(start, tz="UTC"), real_first)
        win_to   = min(pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1), real_last)
        if win_from >= win_to:
            return None, None

        with _print_lock:
            print(f"    [{i}/{total}] {srow.station} · {srow.parameter} "
                  f"real range {win_from.date()}→{win_to.date()}")
        hourly = fetch_sensor_hourly(srow.sensor_id, win_from, win_to)
        if hourly.empty:
            return None, None
        hourly["station"]   = srow.station
        hourly["latitude"]  = srow.latitude
        hourly["longitude"] = srow.longitude
        hourly["parameter"] = srow.parameter
        return hourly, None
    except Exception as e:
        with _print_lock:
            print(f"    [SKIP SENSOR] {srow.station} · {srow.parameter} failed unexpectedly "
                  f"({type(e).__name__}: {e}) — continuing with remaining sensors")
        return None, f"{srow.station}/{srow.parameter}"


def build_real_station_timeseries(sensors_df: pd.DataFrame, start: date, end: date,
                                   max_workers: int = 6) -> pd.DataFrame:
    """Pulls REAL hourly history for every station/pollutant, IN PARALLEL
    across sensors (each sensor's own months are still fetched sequentially
    and cached, since a single sensor's months share disk cache locality).
    A shared rate limiter (see _RateLimiter) keeps total request rate to
    OpenAQ bounded regardless of worker count — more workers reduces idle
    waiting on slow individual responses, it doesn't increase load on the
    server beyond what was going to happen anyway."""
    total = len(sensors_df)
    print(f"  Pulling real hourly measurements for {total} sensors, "
          f"{max_workers} parallel workers, month-by-month with disk caching ...")

    all_rows, skipped = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_process_one_sensor, i, total, srow, start, end): srow
            for i, srow in enumerate(sensors_df.itertuples(), 1)
        }
        for fut in concurrent.futures.as_completed(futures):
            hourly, skip_label = fut.result()
            if hourly is not None:
                all_rows.append(hourly)
            if skip_label:
                skipped.append(skip_label)

    if skipped:
        preview = skipped[:10]
        print(f"  → {len(skipped)} sensor(s) skipped entirely: {preview}"
              f"{' ...' if len(skipped) > 10 else ''}")

    if not all_rows:
        raise RuntimeError(
            "[ERROR] No real historical measurements returned for any sensor in this "
            "window. Try widening --start-date, or check station coverage on "
            "explore.openaq.org for this city."
        )
    long_df = pd.concat(all_rows, ignore_index=True)
    long_df["timestamp"] = pd.to_datetime(long_df["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    print(f"  → {len(long_df):,} real hourly readings across {long_df['station'].nunique()} stations")
    return long_df


def pivot_station_hourly(long_df: pd.DataFrame) -> pd.DataFrame:
    """Long (station, timestamp, parameter, value) → wide, one row per station-hour."""
    wide = long_df.pivot_table(
        index=["station", "latitude", "longitude", "timestamp"],
        columns="parameter", values="value", aggfunc="mean",
    ).reset_index()
    wide.columns.name = None
    wide["true_aqi"] = wide.apply(aqi_from_row, axis=1)
    return wide


# ─────────────────────────────────────────────────────────────────────────────
# GRID + NEAREST-STATION JOIN
# ─────────────────────────────────────────────────────────────────────────────
def build_grid() -> pd.DataFrame:
    lats = np.linspace(CITY["lat_min"], CITY["lat_max"], N_CELLS_Y)
    lons = np.linspace(CITY["lon_min"], CITY["lon_max"], N_CELLS_X)
    cells = [
        {"cell_id": i * N_CELLS_X + j, "lat": round(lat, 6), "lon": round(lon, 6)}
        for i, lat in enumerate(lats) for j, lon in enumerate(lons)
    ]
    df = pd.DataFrame(cells)
    df.to_csv(os.path.join(OUT_DIR, "cells_static.csv"), index=False)
    print(f"  → {len(df)} grid cells ({N_CELLS_Y}×{N_CELLS_X})")
    return df


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(min(a, 1.0)))


def nearest_station_static_join(cells: pd.DataFrame, station_locs: pd.DataFrame) -> pd.DataFrame:
    """
    Static (one-time) nearest-station assignment per cell.
    has_station=1 is given to exactly ONE cell per station — the cell for
    which that station is the closest match, chosen by minimum distance
    among all cells claiming that station as nearest. Guarantees
    one-station-to-one-cell with no duplicate labels, and works regardless
    of grid spacing vs. station layout (unlike a fixed distance threshold,
    which could give a station zero or multiple labelled cells).
    """
    print("  Assigning nearest real station to each cell ...")
    rows = []
    for _, c in cells.iterrows():
        dists = station_locs.apply(
            lambda s: haversine_km(c.lat, c.lon, s.latitude, s.longitude), axis=1
        )
        idx = dists.idxmin()
        rows.append({
            "cell_id":         c.cell_id,
            "nearest_station": station_locs.loc[idx, "station"],
            "nearest_dist_km": round(dists[idx], 3),
        })
    result = pd.DataFrame(rows)

    result["has_station"] = 0
    for station_name, grp in result.groupby("nearest_station"):
        best_idx = grp["nearest_dist_km"].idxmin()
        result.loc[best_idx, "has_station"] = 1

    print(f"  → has_station=1: {result['has_station'].sum()} cells (one per station, real "
          f"true_aqi) | {len(result) - result['has_station'].sum()} cells are proxy-only "
          f"(true_aqi = NaN, to be predicted later)")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# WEATHER  (Open-Meteo — real, unchanged)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_openmeteo_history(start: date, end: date) -> pd.DataFrame:
    print(f"  Fetching Open-Meteo historical weather {start} → {end} ...")
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": CITY["lat_center"], "longitude": CITY["lon_center"],
        "start_date": str(start), "end_date": str(end),
        "hourly": "temperature_2m,relativehumidity_2m,windspeed_10m,winddirection_10m",
        "timezone": "Asia/Kolkata",
    }
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    h = resp.json()["hourly"]
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(h["time"]),
        "temp": h["temperature_2m"], "humidity": h["relativehumidity_2m"],
        "wind_speed": h["windspeed_10m"], "wind_dir": h["winddirection_10m"],
    })
    print(f"  → {len(df):,} real hourly weather rows")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# SATELLITE (GEE) — batched + cached across the full historical window
# ─────────────────────────────────────────────────────────────────────────────
SATELLITE_CACHE_DIR = os.path.join(OUT_DIR, "satellite_cache")
os.makedirs(SATELLITE_CACHE_DIR, exist_ok=True)


def fetch_satellite_gee_batch(cells: pd.DataFrame, target_date: str) -> pd.DataFrame:
    """Real Sentinel-5P NO2 + MODIS AOD for one date, ALL cells in one round trip."""
    import ee
    ee.Initialize()
    next_date = (datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    no2_img = (ee.ImageCollection("COPERNICUS/S5P/OFFL/L3_NO2")
               .filterDate(target_date, next_date)
               .select("tropospheric_NO2_column_number_density").mean())
    aod_img = (ee.ImageCollection("MODIS/061/MCD19A2_GRANULES")
               .filterDate(target_date, next_date)
               .select("Optical_Depth_047").mean())

    fc = ee.FeatureCollection([
        ee.Feature(ee.Geometry.Point([float(c.lon), float(c.lat)]), {"cell_id": int(c.cell_id)})
        for c in cells.itertuples()
    ])
    no2_feats = no2_img.reduceRegions(collection=fc, reducer=ee.Reducer.mean(), scale=1000).getInfo()["features"]
    aod_feats = aod_img.reduceRegions(collection=fc, reducer=ee.Reducer.mean(), scale=1000).getInfo()["features"]
    no2_map = {f["properties"]["cell_id"]: f["properties"].get("mean") for f in no2_feats}
    aod_map = {f["properties"]["cell_id"]: f["properties"].get("mean") for f in aod_feats}

    return pd.DataFrame({
        "cell_id": cells["cell_id"].values,
        "no2_satellite": cells["cell_id"].map(no2_map).values,
        "aod_satellite": cells["cell_id"].map(aod_map).values,
    })


def get_satellite_for_date_cached(cells: pd.DataFrame, date_str: str) -> pd.DataFrame:
    cache_file = os.path.join(SATELLITE_CACHE_DIR, f"{date_str}.csv")
    if os.path.exists(cache_file):
        return pd.read_csv(cache_file)
    try:
        sat = fetch_satellite_gee_batch(cells, date_str)
    except Exception as e:
        print(f"    [GEE] {date_str} failed ({e}) — skipping, will stay NaN")
        return pd.DataFrame(columns=["cell_id", "no2_satellite", "aod_satellite"])
    sat.to_csv(cache_file, index=False)
    return sat


def fetch_satellite_for_range(cells: pd.DataFrame, start: date, end: date,
                               frequency: str = "weekly") -> pd.DataFrame:
    try:
        import ee
        ee.Initialize()
    except Exception as e:
        print(f"  [GEE] Not available ({e}) — satellite cols will be NaN for the whole range.")
        return pd.DataFrame(columns=["cell_id", "date", "no2_satellite", "aod_satellite"])

    step_days = 1 if frequency == "daily" else 7
    dates = pd.date_range(start, end, freq=f"{step_days}D").date
    print(f"  Fetching real satellite for {len(dates)} dates ({frequency}, cached to {SATELLITE_CACHE_DIR}) ...")
    frames = []
    for i, d in enumerate(dates):
        if i % 10 == 0:
            print(f"    ... satellite date {i}/{len(dates)}")
        sat = get_satellite_for_date_cached(cells, str(d))
        if sat.empty:
            continue
        sat = sat.copy()
        sat["date"] = d
        frames.append(sat)

    if not frames:
        print("  [GEE] No satellite data retrieved for any date in range.")
        return pd.DataFrame(columns=["cell_id", "date", "no2_satellite", "aod_satellite"])

    result = pd.concat(frames, ignore_index=True)
    print(f"  → real satellite coverage for {result['date'].nunique()} distinct dates")
    return result


def merge_satellite_onto_cells(df: pd.DataFrame, sat_by_date: pd.DataFrame, frequency: str) -> pd.DataFrame:
    df = df.copy()
    df["date"] = df["timestamp"].dt.date
    if sat_by_date.empty:
        df["no2_satellite"] = np.nan
        df["aod_satellite"] = np.nan
        return df.drop(columns=["date"])
    merged = df.merge(sat_by_date, on=["cell_id", "date"], how="left")
    if frequency == "weekly":
        merged = merged.sort_values(["cell_id", "timestamp"])
        for col in ["no2_satellite", "aod_satellite"]:
            merged[col] = merged.groupby("cell_id")[col].transform(lambda s: s.ffill(limit=7).bfill(limit=7))
    return merged.drop(columns=["date"])


# ─────────────────────────────────────────────────────────────────────────────
# ROAD DENSITY (real, static) — built by the separate fetch_road_density_fast.py
# script (single city-wide query + clipped spatial join). This just merges
# its cached output.
# ─────────────────────────────────────────────────────────────────────────────
def merge_traffic_index_if_available(cells: pd.DataFrame) -> pd.DataFrame:
    cache_path = os.path.join(OUT_DIR, "road_density_cache.csv")
    if not os.path.exists(cache_path):
        print("  [traffic] data/road_density_cache.csv not found — run "
              "fetch_road_density_fast.py first. traffic_index will be NaN.")
        cells["traffic_index"] = np.nan
        return cells
    rd = pd.read_csv(cache_path)
    print(f"  [traffic] merged real OSM road-density index from {cache_path}")
    return cells.merge(rd[["cell_id", "traffic_index"]], on="cell_id", how="left")


LANDUSE_COLS = ["industrial_pct", "construction_pct", "green_cover_pct", "residential_pct", "water_pct"]


def merge_landuse_if_available(cells: pd.DataFrame) -> pd.DataFrame:
    """Real OSM land-use fractions from the separate fetch_landuse_features.py script."""
    landuse_path = os.path.join(OUT_DIR, "landuse_static.csv")
    if not os.path.exists(landuse_path):
        print("  [land use] data/landuse_static.csv not found — run fetch_landuse_features.py "
              "first. Land-use columns will be NaN for now.")
        for c in LANDUSE_COLS:
            cells[c] = np.nan
        return cells
    landuse = pd.read_csv(landuse_path)
    print(f"  [land use] merged real OSM land-use fractions from {landuse_path}")
    return cells.merge(landuse, on="cell_id", how="left")


def add_dynamic_traffic(df: pd.DataFrame) -> pd.DataFrame:
    """
    traffic_index (real, static, from OSM road density) × a documented
    diurnal/weekday heuristic = traffic_index_dynamic. Keeps REAL spatial
    variation (different cells have different amounts of real road) while
    adding a clearly-labelled temporal heuristic on top — not measured live
    congestion, say so in the pitch.

    Deliberately does NOT multiply by no2 or any other pollutant: no2
    already directly determines true_aqi for labelled rows via the CPCB
    formula, so folding it into a feature would be circular.
    """
    hour = df["hour"]
    weekday = df["weekday"]
    diurnal = 0.4 + 0.35 * np.exp(-((hour - 9) ** 2) / 8) + 0.35 * np.exp(-((hour - 19) ** 2) / 10)
    weekend_factor = np.where(weekday >= 5, 0.7, 1.0)
    base = df["traffic_index"].fillna(0) if "traffic_index" in df.columns else 0.0
    df["traffic_index_dynamic"] = (base * diurnal * weekend_factor).round(4)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# SEASONAL FEATURES (calendar facts — deterministic, fine as-is)
# ─────────────────────────────────────────────────────────────────────────────
FESTIVAL_MONTHS_DAYS = {(10,24),(10,25),(11,1),(11,12),(11,13)}
CROP_BURNING_MONTHS  = {10, 11}
WINTER_MONTHS        = {11, 12, 1, 2}
SUMMER_MONTHS        = {4, 5, 6}

def add_seasonal_features(df: pd.DataFrame) -> pd.DataFrame:
    ts = df["timestamp"]
    df["month"] = ts.dt.month
    df["day"] = ts.dt.day
    df["hour"] = ts.dt.hour
    df["weekday"] = ts.dt.dayofweek
    df["is_winter"] = ts.dt.month.isin(WINTER_MONTHS).astype(int)
    df["is_summer"] = ts.dt.month.isin(SUMMER_MONTHS).astype(int)
    df["is_crop_burn"] = ts.dt.month.isin(CROP_BURNING_MONTHS).astype(int)
    df["is_festival"] = ts.apply(lambda t: 1 if (t.month, t.day) in FESTIVAL_MONTHS_DAYS else 0)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# LAG FEATURES — computed on REAL true_aqi only; NaN elsewhere
# ─────────────────────────────────────────────────────────────────────────────
def add_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    print("  Computing lag features (per cell, from real readings only) ...")
    df = df.sort_values(["cell_id", "timestamp"]).reset_index(drop=True)
    grp = df.groupby("cell_id")["true_aqi"]
    for h in [1, 6, 12, 24, 48]:
        df[f"aqi_lag_{h}h"] = grp.shift(h)
    df["aqi_roll_mean_24h"] = grp.transform(lambda x: x.rolling(24, min_periods=6).mean()).round(2)
    df["aqi_roll_mean_7d"]  = grp.transform(lambda x: x.rolling(168, min_periods=24).mean()).round(2)
    df["aqi_prev_day_max"]  = grp.transform(lambda x: x.rolling(24, min_periods=6).max().shift(24)).round(2)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# ASSEMBLE CELL-HOUR TABLE
#   - Every cell inherits its nearest station's REAL hourly series as proxy
#     features (pm25/pm10/no2/so2/o3/co columns).
#   - true_aqi populated ONLY for has_station==1 cells (their own real value).
#   - ALL static per-cell columns present on cells_meta (land use %, traffic
#     index, or anything added later) are carried forward automatically.
# ─────────────────────────────────────────────────────────────────────────────
def assemble_cell_hour_table(
    cells_meta: pd.DataFrame,
    station_wide: pd.DataFrame,
    timestamps: pd.DataFrame,
) -> pd.DataFrame:

    print("  Assembling cell-hour table from real station series (no invented rows) ...")

    pollutant_cols = [
        c for c in ["pm25", "pm10", "no2", "so2", "o3", "co"]
        if c in station_wide.columns
    ]

    station_groups = {
        name: g.sort_values("timestamp")
        for name, g in station_wide.groupby("station")
    }

    core_cols = {
        "cell_id",
        "lat",
        "lon",
        "nearest_station",
        "nearest_dist_km",
        "has_station",
    }

    static_extra_cols = [
        c for c in cells_meta.columns
        if c not in core_cols
    ]

    if static_extra_cols:
        print(
            f"  → carrying forward static per-cell columns: {static_extra_cols}"
        )

    all_rows = []

    ####################################################################
    # MASTER TIMELINE
    ####################################################################

    master_time = pd.DataFrame({
        "timestamp": timestamps
    })

    ####################################################################
    # BUILD EVERY CELL
    ####################################################################

    for c in cells_meta.itertuples():

        s = station_groups.get(c.nearest_station)

        ############################################################
        # Station never reported
        ############################################################

        if s is None or s.empty:

            block = master_time.copy()

            for p in pollutant_cols:
                block[p] = np.nan

            block["true_aqi"] = np.nan

        ############################################################
        # Station has observations
        ############################################################

        else:

            block = master_time.merge(
                s[
                    [
                        "timestamp",
                        *pollutant_cols,
                        "true_aqi",
                    ]
                ],
                on="timestamp",
                how="left",
                sort=True,
            )

            value_cols = pollutant_cols + ["true_aqi"]

            ########################################################
            # Carry values forward
            ########################################################

            block[value_cols] = block[value_cols].ffill()

            ########################################################
            # Fill beginning if first few hours are missing
            ########################################################

            block[value_cols] = block[value_cols].bfill()

        ############################################################
        # Metadata
        ############################################################

        block["cell_id"] = c.cell_id
        block["lat"] = c.lat
        block["lon"] = c.lon

        block["nearest_station"] = c.nearest_station
        block["nearest_dist_km"] = c.nearest_dist_km
        block["has_station"] = c.has_station

        for col in static_extra_cols:
            block[col] = getattr(c, col)

        ############################################################
        # Ground truth only exists for station cells
        ############################################################

        if c.has_station != 1:
            block["true_aqi"] = np.nan

        all_rows.append(block)

    df = pd.concat(all_rows, ignore_index=True)

    print(
        f"  → {len(df):,} real cell-hour rows "
        f"({df['true_aqi'].notna().sum():,} with real true_aqi labels)"
    )

    return df


# ─────────────────────────────────────────────────────────────────────────────
# FINAL COLUMN ORDER
# ─────────────────────────────────────────────────────────────────────────────
FINAL_COLS = [
    "cell_id", "timestamp", "lat", "lon",
    "wind_speed", "wind_dir", "temp", "humidity",
    "no2_satellite", "aod_satellite",
    "traffic_index", "traffic_index_dynamic",
    "industrial_pct", "construction_pct", "green_cover_pct", "residential_pct", "water_pct",
    "nearest_dist_km",
    "pm25", "pm10", "no2", "so2", "o3", "co",
    "aqi_lag_1h", "aqi_lag_6h", "aqi_lag_12h", "aqi_lag_24h", "aqi_lag_48h",
    "aqi_roll_mean_24h", "aqi_roll_mean_7d", "aqi_prev_day_max",
    "month", "day", "hour", "weekday",
    "is_winter", "is_summer", "is_crop_burn", "is_festival",
    "nearest_station", "has_station",
    "true_aqi",   # ← TARGET, real, NaN for cells with no station
]


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Build REAL AQI training dataset for Delhi (OpenAQ-based)")
    parser.add_argument("--start-date", default=str(DEFAULT_START), help="Requested start YYYY-MM-DD (clipped to real per-sensor availability)")
    parser.add_argument("--end-date", default=str(DEFAULT_END), help="Requested end YYYY-MM-DD")
    parser.add_argument("--skip-satellite", action="store_true")
    parser.add_argument("--satellite-frequency", choices=["daily", "weekly"], default="weekly",
                         help="How often to query GEE across the historical window.")
    parser.add_argument("--max-workers", type=int, default=4,
                         help="Parallel sensor downloads. Since the shared rate limiter now "
                              "paces requests to OpenAQ's own reported limit (~60/min) "
                              "regardless of worker count, more workers mainly helps overlap "
                              "waiting time rather than increase raw throughput — 4 is plenty; "
                              "going much higher won't make this meaningfully faster.")
    args = parser.parse_args()

    start = date.fromisoformat(args.start_date)
    end   = date.fromisoformat(args.end_date)

    if (end - start).days > 400 and not os.path.exists(
        os.path.join(OUT_DIR, f"openaq_hourly_{start}_{end}.csv.gz")
    ):
        print(f"  [advisory] You've requested {(end-start).days} days across ~800+ sensors — "
              f"even parallelized, this is a genuinely large pull. For faster iteration while "
              f"still developing the model, consider narrowing first, e.g.:\n"
              f"    python3 fetch_real_data.py --start-date 2025-01-01 --end-date 2025-12-31 --skip-satellite\n"
              f"  One year of hourly data is normally enough to build and validate a first model; "
              f"widen to the full range once the pipeline is proven end-to-end.\n")

    print(f"\n{'='*60}\n  Urban AQI Forecasting — REAL Dataset Builder\n"
          f"  City : {CITY['name']}\n  Grid : {N_CELLS_Y}×{N_CELLS_X} = {N_CELLS_Y*N_CELLS_X} cells\n"
          f"  Requested window : {start} → {end} (clipped per-sensor to real availability)\n{'='*60}\n")

    print("[1/8] Real CPCB station list (OpenAQ)")
    sensors_df = fetch_openaq_locations(CITY)
    sensors_df = dedupe_sensors(sensors_df)
    station_locs = sensors_df.drop_duplicates("station")[["station", "latitude", "longitude"]]

    print("\n[2/8] Real historical hourly measurements per station (OpenAQ, monthly-cached, parallel)")
    cache_name = f"openaq_hourly_{start}_{end}.csv.gz"
    cache_path = os.path.join(OUT_DIR, cache_name)
    if os.path.exists(cache_path):
        print(f"  Loading cached OpenAQ data from {cache_path}")
        long_df = pd.read_csv(cache_path, compression="gzip")
        long_df["timestamp"] = pd.to_datetime(long_df["timestamp"])
    else:
        print("  No cache for this exact date range — downloading from OpenAQ ...")
        long_df = build_real_station_timeseries(sensors_df, start, end, max_workers=args.max_workers)
        print(f"  Saving OpenAQ cache to {cache_path}")
        long_df.to_csv(cache_path, index=False, compression="gzip")

    cache_start, cache_end = long_df["timestamp"].min(), long_df["timestamp"].max()
    print(f"  Loaded data covers real range {cache_start} → {cache_end}")
    if cache_start.date() > start or cache_end.date() < end - timedelta(days=1):
        print(f"  [note] real coverage is narrower than requested ({start}→{end}) — "
              f"expected, since sensors are clipped to their real reporting window "
              f"and some sensor-months may have been skipped after retries.")

    station_wide = pivot_station_hourly(long_df)
    station_wide.to_csv(os.path.join(OUT_DIR, "station_hourly_wide.csv"), index=False)
    station_locs.to_csv(os.path.join(OUT_DIR, "stations_static.csv"), index=False)
    print(f"  → real station coordinates saved to data/stations_static.csv, "
          f"pivoted station-hour table saved to data/station_hourly_wide.csv")

    print("\n[3/8] Building grid")
    cells = build_grid()

    print("\n[4/8] Nearest-station join (static geometry)")
    joined = nearest_station_static_join(cells, station_locs)
    cells_meta = cells.merge(joined, on="cell_id")

    print("\n[5/8] Real static land use + traffic index (OSM)")
    cells_meta = merge_traffic_index_if_available(cells_meta)
    cells_meta = merge_landuse_if_available(cells_meta)

    print("\n[6/8] Assembling cell-hour table from real station series")
    timestamps = (
        station_wide["timestamp"]
        .drop_duplicates()
        .sort_values()
        .reset_index(drop=True)
    )

    df = assemble_cell_hour_table(
        cells_meta,
        station_wide,
        timestamps,
    )

    print(f"\n     Real historical weather {df['timestamp'].min()} → {df['timestamp'].max()}")
    weather = fetch_openmeteo_history(df["timestamp"].min().date(), df["timestamp"].max().date())
    df = df.merge(weather, on="timestamp", how="left")

    print("\n[7/8] Seasonal features + dynamic traffic")
    df = add_seasonal_features(df)  # must run before dynamic traffic (needs hour/weekday)
    df = add_dynamic_traffic(df)

    if not args.skip_satellite:
        print(f"\n     Real satellite across full window ({args.satellite_frequency}, cached)")
        sat_by_date = fetch_satellite_for_range(
            cells[["cell_id", "lat", "lon"]], df["timestamp"].min().date(), df["timestamp"].max().date(),
            frequency=args.satellite_frequency,
        )
        df = merge_satellite_onto_cells(df, sat_by_date, args.satellite_frequency)
    else:
        df["no2_satellite"] = np.nan
        df["aod_satellite"] = np.nan

    print("\n[8/8] Lag features")
    df = add_lag_features(df)

    out_cols = [c for c in FINAL_COLS if c in df.columns]
    df = df[out_cols]
    out_path = os.path.join(OUT_DIR, "training_dataset.csv")
    df.to_csv(out_path, index=False)

    n_labelled = df["true_aqi"].notna().sum()
    sat_coverage = df["no2_satellite"].notna().mean() * 100 if "no2_satellite" in df.columns else 0.0
    landuse_coverage = df["industrial_pct"].notna().mean() * 100 if "industrial_pct" in df.columns else 0.0
    print(f"\n{'='*60}\n  ✓ Dataset saved: {out_path}\n"
          f"  Rows   : {len(df):,}\n  Cells  : {df['cell_id'].nunique()}\n"
          f"  Real labelled rows (true_aqi, has_station=1): {n_labelled:,}\n"
          f"  Proxy-only rows (true_aqi=NaN, to be predicted): {len(df)-n_labelled:,}\n"
          f"  Satellite coverage: {sat_coverage:.0f}% of rows have a real NO2/AOD reading\n"
          f"  Land-use coverage: {landuse_coverage:.0f}% of rows have real OSM land-use %\n"
          f"  AQI range (real labels only): {df['true_aqi'].min():.0f}–{df['true_aqi'].max():.0f}\n"
          f"\n  Next step: train the spatial-estimation model (§6) on the labelled\n"
          f"  rows only, validate with leave-one-station-out, THEN predict onto\n"
          f"  the proxy-only cells.\n{'='*60}\n")


if __name__ == "__main__":
    main()
