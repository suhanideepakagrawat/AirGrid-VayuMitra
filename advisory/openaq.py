"""Live station readings from OpenAQ v3 — the "what is happening right now" source.

This is the ONLY live air-quality source in the product, and it is deliberately the
same source the models were trained on: OpenAQ republishes CPCB / DPCC / IMD / IITM /
HSPCB station feeds. Because these are the actual government instruments, our numbers
match what a citizen sees on the CPCB app *by construction* rather than by luck.

Why not a gridded model (CAMS, etc.): benchmarked against 62 of these same Delhi
stations it scored r=0.085, RMSE 106.7, bias -61.7, and returned only five distinct
values for the whole city. At 11 km resolution it cannot see the difference between
Anand Vihar and Lodhi Road, which is the entire problem we exist to solve.

DEFENSIVE BY DESIGN, like advisory/llm.py: every function returns None / empty rather
than raising, so a missing key or a dead network degrades the live layer to
"unavailable" and the forecast layer keeps working.

Two traps this module handles, both found by inspecting the real API:

1. **Dead stations.** 31 of the ~90 OpenAQ locations around Delhi last reported more
   than 30 days ago — one in 2018. Every location is filtered on `datetimeLast`.
2. **Duplicate parameters in different units.** OpenAQ exposes no2 as id 5 (ug/m3),
   7 (ppm) and 15 (ppb). The CPCB AQI formula wants ug/m3, so only the metric ids are
   accepted and the rest are ignored.
"""
from __future__ import annotations

import concurrent.futures
import os
import threading
import time

import requests

BASE = "https://api.openaq.org/v3"

# Delhi bounding box, matching CITY in ml_pipeline/fetch_real_data.py, as
# "lon_min,lat_min,lon_max,lat_max". Deliberately bbox and not coordinates+radius:
# OpenAQ caps radius at 25 km (30 km is rejected outright), and a 25 km circle
# centred on Delhi clips the bbox corners. bbox covers the full training area and
# finds more stations — 104 locations / 63 live, against 90 / 54 for the circle.
CITY_BBOX = "76.80,28.40,77.40,28.90"

# parameter id -> (canonical name, factor to convert the reported unit to ug/m3).
#
# Delhi stations carry TWO generations of sensors and this matters enormously: the
# legacy ug/m3 sensors (ids 1-6) are mostly DEAD — at R K Puram they last reported in
# February 2018 — while the sensors actually reporting today publish NO2 and SO2 in
# **ppb**. Accepting only the metric ids therefore silently drops live NO2/SO2, which
# are exactly the traffic and industry fingerprints. Both generations are accepted and
# the age filter discards whichever is stale.
#
# ppb -> ug/m3 at 25 C / 1013 hPa: multiply by molar mass / 24.45.
#   NO2 46.01/24.45 = 1.88   SO2 64.07/24.45 = 2.62   O3 48.00/24.45 = 1.96
#
# CO is deliberately excluded: its unit labelling is inconsistent (id 102 is tagged
# ppb but reports values around 1.0, which is only plausible as ppm) and the CPCB CO
# sub-index needs an 8-hour rolling average we cannot form from a single latest value.
# PM2.5 and PM10 dominate the Delhi AQI in practice, so nothing material is lost.
PARAMS: dict[int, tuple[str, float]] = {
    1:   ("pm10", 1.0),
    2:   ("pm25", 1.0),
    3:   ("o3", 1.0),
    5:   ("no2", 1.0),
    6:   ("so2", 1.0),
    10:  ("o3", 1962.0),      # ppm
    15:  ("no2", 1.88),       # ppb — the live NO2 feed
    101: ("so2", 2.62),       # ppb — the live SO2 feed
    32:  ("o3", 1.96),        # ppb
}

# Station-reported meteorology, passed through unconverted. Real anemometer readings
# at ~48 Delhi stations, which beat interpolated model wind for upwind reasoning.
MET_PARAMS: dict[int, str] = {22: "wind_direction", 34: "wind_speed"}

# A station is "live" if it reported within this window.
FRESH_HOURS = 6.0

_LOCATIONS_TTL = 3600.0     # station metadata barely changes
_LATEST_TTL = 600.0         # readings are hourly; 10 min is plenty
_HTTP_TIMEOUT = 20.0
_MAX_WORKERS = 8

_lock = threading.Lock()
_locations_cache: tuple[float, list[dict]] | None = None
_latest_cache: tuple[float, list[dict]] | None = None


def api_key() -> str:
    return os.getenv("OPENAQ_API_KEY", "").strip()


def available() -> bool:
    """True if a key is configured. Does not guarantee the network works."""
    return bool(api_key())


def _get(path: str, params: dict | None = None) -> dict | None:
    key = api_key()
    if not key:
        return None
    try:
        r = requests.get(f"{BASE}{path}", params=params or {},
                         headers={"X-API-Key": key}, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _hours_since(iso: str | None) -> float:
    """Age in hours of an ISO-8601 UTC timestamp; +inf when unparseable."""
    if not iso:
        return float("inf")
    try:
        import datetime as dt
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600.0
    except Exception:
        return float("inf")


def fetch_locations(force: bool = False) -> list[dict]:
    """Delhi-area stations that are actually reporting, with their sensor map.

    Returns [{id, name, lat, lon, age_hours, sensors: {sensor_id: parameter}}].
    """
    global _locations_cache
    with _lock:
        if not force and _locations_cache and time.time() - _locations_cache[0] < _LOCATIONS_TTL:
            return _locations_cache[1]

    data = _get("/locations", {"bbox": CITY_BBOX, "limit": 1000})
    if not data or "results" not in data:
        return []

    out: list[dict] = []
    for loc in data["results"]:
        age = _hours_since((loc.get("datetimeLast") or {}).get("utc"))
        if age > FRESH_HOURS:
            continue                                   # dead station — see trap 1
        coords = loc.get("coordinates") or {}
        lat, lon = coords.get("latitude"), coords.get("longitude")
        if lat is None or lon is None:
            continue
        # sensor_id -> (canonical name, unit factor). Both sensor generations are
        # kept; _latest_for_location drops whichever one is stale.
        sensors: dict[int, tuple[str, float]] = {}
        for s in loc.get("sensors") or []:
            pid = (s.get("parameter") or {}).get("id")
            sid = s.get("id")
            if sid is None:
                continue
            if pid in PARAMS:
                sensors[int(sid)] = PARAMS[pid]
            elif pid in MET_PARAMS:
                sensors[int(sid)] = (MET_PARAMS[pid], 1.0)
        if not sensors:
            continue
        out.append({"id": loc.get("id"), "name": loc.get("name") or "unknown",
                    "lat": float(lat), "lon": float(lon),
                    "age_hours": round(age, 2), "sensors": sensors})

    with _lock:
        _locations_cache = (time.time(), out)
    return out


def _latest_for_location(loc: dict) -> dict | None:
    """Newest reading per pollutant for one station."""
    data = _get(f"/locations/{loc['id']}/latest")
    if not data or "results" not in data:
        return None

    values: dict[str, float] = {}
    met: dict[str, float] = {}
    ages: dict[str, float] = {}
    newest: str | None = None

    for row in data["results"]:
        entry = loc["sensors"].get(row.get("sensorsId"))
        if not entry:
            continue
        name, factor = entry
        val = row.get("value")
        if val is None or float(val) < 0:
            continue
        stamp = (row.get("datetime") or {}).get("utc")
        age = _hours_since(stamp)
        if age > FRESH_HOURS:
            continue                     # drops the dead 2018-era sensor generation
        # When both generations report, keep the fresher reading.
        if name in ages and ages[name] <= age:
            continue
        ages[name] = age
        if name in MET_PARAMS.values():
            met[name] = float(val)
        else:
            values[name] = round(float(val) * factor, 2)
        if newest is None or str(stamp) > newest:
            newest = str(stamp)

    if not values:
        return None
    return {"station_id": loc["id"], "station": loc["name"],
            "lat": loc["lat"], "lon": loc["lon"],
            "observed_at": newest, "age_hours": round(_hours_since(newest), 2),
            "pollutants": values, "met": met}


def live_stations(force: bool = False) -> list[dict]:
    """Current readings for every live Delhi station. [] when unavailable.

    Fetched in parallel because this is network-bound: ~54 stations serially would
    take far too long for a request-path endpoint, even a cached one.
    """
    global _latest_cache
    with _lock:
        if not force and _latest_cache and time.time() - _latest_cache[0] < _LATEST_TTL:
            return _latest_cache[1]

    locs = fetch_locations(force=force)
    if not locs:
        return []

    out: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        for res in pool.map(_latest_for_location, locs):
            if res:
                out.append(res)

    out.sort(key=lambda s: s["station"])
    with _lock:
        _latest_cache = (time.time(), out)
    return out


def cache_age_seconds() -> float | None:
    """Seconds since the readings cache was filled; None if never."""
    with _lock:
        return None if _latest_cache is None else time.time() - _latest_cache[0]
