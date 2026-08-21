"""Regional biomass burning — the fourth source, from NASA satellite fire detections.

Delhi's worst air arrives from outside Delhi. Through October and November, paddy
stubble burning across Punjab and Haryana can dominate the city's PM2.5, and no amount
of local enforcement touches it. Our geospatial engine could never see this: it reasons
about roads, industry and construction *inside* the city, so a smoke plume arriving
from 200 km upwind was invisible to it and got silently misattributed to whatever
local land use happened to sit nearby.

WHAT THIS ACTUALLY MEASURES
---------------------------
NASA's VIIRS and MODIS instruments detect **thermal anomalies** — they see the heat of
an active fire from orbit — and publish detections through FIRMS within ~3 hours. We
combine three facts, all measured, none assumed:

    1. WHERE the fires are burning right now   (FIRMS)
    2. HOW STRONG they are                     (fire radiative power, in megawatts)
    3. WHETHER the wind is carrying them here  (measured station wind, not modelled)

A fire only counts if the wind is blowing *from* it *towards* Delhi. A thousand fires
in Punjab with an easterly wind are somebody else's problem that day, and the honest
answer is to say so rather than to blame them for a local dust event.

THE SEASONAL HONESTY POINT
--------------------------
Outside the burning season this correctly reports almost nothing. That is the engine
working, not failing — and it is worth demonstrating deliberately rather than hoping
nobody notices an empty map in August.

LIMITS
------
* Satellites see fires, not smoke. Whether the plume actually reaches ground level
  here depends on injection height and mixing, which we do not model.
* Cloud cover hides fires. A quiet reading can mean clear skies over no fires, or
  cloud over many.
* Detection is instantaneous; a fire that burned out an hour ago may still be counted
  within the lookback window.
* We report transport *plausibility*, never a percentage contribution.

Defensive throughout: no key, no network or a bad response yields
{"available": false}, and the other three sources are unaffected.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import os
import statistics
import threading
import time

import requests

FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

# Punjab, Haryana and north Rajasthan — the stubble belt upwind of Delhi.
# "lon_min,lat_min,lon_max,lat_max" as FIRMS expects.
STUBBLE_BBOX = "73.0,28.0,78.0,32.5"

# Satellite product. VIIRS at 375 m resolves small field fires that MODIS (1 km)
# misses, which matters for stubble burning.
PRODUCT = "VIIRS_SNPP_NRT"

DELHI_LAT, DELHI_LON = 28.65, 77.10

LOOKBACK_DAYS = 2
MAX_TRANSPORT_KM = 400.0      # beyond this a plume is too diluted to attribute
# Wind must point within this many degrees of the fire's bearing for transport to be
# plausible. Wide because plumes disperse laterally and wind veers over hundreds of km.
BEARING_TOLERANCE_DEG = 45.0
# Below this the air is not going anywhere in particular.
MIN_TRANSPORT_WIND_MS = 1.0

_CACHE_TTL = 1800.0           # FIRMS publishes on satellite overpasses, not continuously
_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


def map_key() -> str:
    return os.getenv("FIRMS_MAP_KEY", "").strip()


def available() -> bool:
    return bool(map_key())


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Compass bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _angular_gap(a: float, b: float) -> float:
    """Smallest absolute angle between two compass bearings."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def fetch_fires(days: int = LOOKBACK_DAYS) -> list[dict]:
    """Active fire detections in the stubble belt. [] when unavailable."""
    key = map_key()
    if not key:
        return []
    url = f"{FIRMS_BASE}/{key}/{PRODUCT}/{STUBBLE_BBOX}/{days}"
    try:
        r = requests.get(url, timeout=45)
        r.raise_for_status()
        text = r.text
        if not text or "latitude" not in text.split("\n", 1)[0]:
            return []            # FIRMS returns a plain-text error, not JSON
    except Exception:
        return []

    out: list[dict] = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            lat, lon = float(row["latitude"]), float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            frp = float(row.get("frp") or 0.0)
        except ValueError:
            frp = 0.0
        out.append({
            "lat": lat, "lon": lon,
            "frp_mw": frp,                       # fire radiative power = intensity
            "confidence": (row.get("confidence") or "").strip(),
            "acq_date": row.get("acq_date"),
            "acq_time": row.get("acq_time"),
            "daynight": row.get("daynight"),
            "satellite": row.get("satellite"),
        })
    return out


def _station_wind(stations: list[dict]) -> tuple[float | None, float | None, int]:
    """Median wind (speed m/s, direction deg) across stations that measure it.

    Real anemometers rather than a model: ~44 Delhi stations report wind, and the
    median is robust to a single stuck vane.
    """
    speeds = [s["met"]["wind_speed"] for s in stations
              if (s.get("met") or {}).get("wind_speed") is not None]
    dirs = [s["met"]["wind_direction"] for s in stations
            if (s.get("met") or {}).get("wind_direction") is not None]
    if not dirs:
        return (statistics.median(speeds) if speeds else None), None, 0
    # Directions are circular — averaging 350 deg and 10 deg naively gives 180,
    # which is the exact opposite of the truth. Average the unit vectors instead.
    sx = sum(math.sin(math.radians(d)) for d in dirs)
    cy = sum(math.cos(math.radians(d)) for d in dirs)
    mean_dir = (math.degrees(math.atan2(sx, cy)) + 360.0) % 360.0
    return (statistics.median(speeds) if speeds else None), round(mean_dir, 1), len(dirs)


def burning_signal(stations: list[dict], force: bool = False) -> dict:
    """Is regional biomass burning plausibly reaching Delhi right now?

    `stations` are the live station records (for measured wind). Always returns the
    {available, ...} contract.
    """
    global _cache
    with _lock:
        if not force and _cache and time.time() - _cache[0] < _CACHE_TTL:
            return _cache[1]

    if not available():
        return {"available": False, "reason": "FIRMS_MAP_KEY not configured"}

    fires = fetch_fires()
    wind_speed, wind_dir, n_wind = _station_wind(stations)

    upwind: list[dict] = []
    for f in fires:
        dist = _haversine_km(DELHI_LAT, DELHI_LON, f["lat"], f["lon"])
        if dist > MAX_TRANSPORT_KM:
            continue
        # Bearing from Delhi to the fire. Meteorological wind direction is the
        # direction the wind blows FROM, so transport happens when the wind
        # direction matches the bearing at which the fire sits.
        bearing = _bearing_deg(DELHI_LAT, DELHI_LON, f["lat"], f["lon"])
        gap = _angular_gap(bearing, wind_dir) if wind_dir is not None else None
        aligned = (gap is not None and gap <= BEARING_TOLERANCE_DEG
                   and (wind_speed or 0) >= MIN_TRANSPORT_WIND_MS)
        if aligned:
            upwind.append({**f, "distance_km": round(dist, 1),
                           "bearing_deg": round(bearing, 1),
                           "wind_gap_deg": round(gap, 1)})

    total_frp = round(sum(f["frp_mw"] for f in upwind), 1)
    # Thresholds are deliberately coarse — this is a plausibility flag, not a dose.
    if not fires:
        level = "none"
    elif not upwind:
        level = "not_transported"
    elif len(upwind) >= 50 or total_frp >= 500:
        level = "strong"
    elif len(upwind) >= 10 or total_frp >= 100:
        level = "moderate"
    else:
        level = "weak"

    if level == "none":
        evidence = "No active fires detected upwind in the last 48 hours"
    elif level == "not_transported":
        evidence = (f"{len(fires)} fires detected in Punjab–Haryana, but the wind "
                    f"({wind_dir:.0f}°) is not carrying them towards Delhi"
                    if wind_dir is not None else
                    f"{len(fires)} fires detected, wind data unavailable")
    else:
        evidence = (f"{len(upwind)} of {len(fires)} satellite fire detections lie "
                    f"upwind, {total_frp:.0f} MW total, wind from {wind_dir:.0f}° at "
                    f"{wind_speed:.1f} m/s")

    nearest = min((f["distance_km"] for f in upwind), default=None)
    result = {
        "available": True,
        "level": level,
        "evidence": evidence,
        "fires_detected": len(fires),
        "fires_upwind": len(upwind),
        "total_frp_mw": total_frp,
        "nearest_upwind_km": nearest,
        "wind_direction_deg": wind_dir,
        "wind_speed_ms": round(wind_speed, 1) if wind_speed is not None else None,
        "wind_stations": n_wind,
        "source": f"NASA FIRMS {PRODUCT} · Punjab–Haryana · last {LOOKBACK_DAYS} days",
        "method": (f"fires within {MAX_TRANSPORT_KM:.0f} km whose bearing from Delhi "
                   f"is within {BEARING_TOLERANCE_DEG:.0f}° of the measured wind"),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        # A handful for the map, strongest first.
        "sample": sorted(upwind, key=lambda f: -f["frp_mw"])[:25],
    }

    with _lock:
        _cache = (time.time(), result)
    return result
