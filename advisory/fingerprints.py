"""Pollutant fingerprints — separating sources by chemistry and daily rhythm.

The geospatial engine answers *what sits upwind of this cell*. It cannot tell a busy
arterial road from a quiet one, because OSM records where roads are, not what is on
them. This module adds the second, independent axis: **what the air is actually made
of, and how it changes through the day**.

Three signatures, each from real measured pollutants:

    TRAFFIC       NO2 well above the citywide norm. Vehicles are the dominant NO2
                  source in Delhi, and NO2 is short-lived, so a high reading points
                  at something emitting close by rather than drifting in.

    INDUSTRY      SO2 well above the citywide norm. SO2 comes from burning
                  sulphur-bearing fuel — coal, heavy oil, brick kilns. Since BS-VI
                  fuel, vehicles emit very little of it, which is what makes it a
                  useful industrial marker.

    CONSTRUCTION  A high PM10/PM2.5 ratio. Mechanical processes (digging, crushing,
                  road dust) throw *coarse* particles; combustion makes *fine* ones.
                  A coarse-heavy mix is the standard dust indicator.

HOW WE NORMALISE, AND WHY IT IS NOT THE OBVIOUS WAY
---------------------------------------------------
The intuitive approach is to look for a twin-peak daily curve — NO2 rising at 9am and
7pm — and call that traffic. **Delhi's real data says otherwise.** Pulling seven days
of hourly NO2 for a central station gives a median of 79 ug/m3 at midnight falling to
27 by late morning: the maximum is at *night*, when nobody is commuting.

That is meteorology, not emissions. After sunset the boundary layer collapses to a few
hundred metres and every ground-level pollutant concentrates into it; after sunrise it
lifts and dilutes them. The daily curve at a Delhi station is dominated by that
breathing, and a rush-versus-quiet ratio would mostly measure the weather.

So we normalise **spatially, at a single instant**: compare each station against the
citywide median *at the same moment*. Every station shares the same mixing height at
that moment, so the meteorological term largely cancels and what remains is local
emission strength — which is exactly the question "is there a traffic source here?".

This is also why the fingerprints need no history to work: the comparison is between
places at one time, not between times at one place. Diurnal profiles are still
computed where enough history exists, but only as supporting colour, never as the
basis of a call.

HONEST LIMITS — state these before anyone asks
----------------------------------------------
* These are **indicators, not apportionment**. Real source apportionment needs filter
  sampling and receptor modelling (PMF/CMB). We measure correlates and say so.
* NO2 also comes from diesel gensets and any high-temperature combustion.
* SO2 is a good industrial marker precisely because vehicles emit little of it since
  BS-VI fuel, but it is not exclusive to industry.
* A high PM10/PM2.5 ratio means coarse dust; it does not distinguish a construction
  site from an unpaved road or a windblown field.
* Baselines need history. A station with too few hours yields no signal rather than a
  guessed one.

Defensive throughout: any failure yields an empty signal, never an exception.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import statistics
import threading
import time

from . import openaq

# Local clock matters: "rush hour" is a human schedule, not a UTC one.
IST_OFFSET_HOURS = 5.5

# Delhi commuting peaks and the quiet window they are measured against.
MORNING_RUSH = (8, 9, 10)
EVENING_RUSH = (18, 19, 20, 21)
QUIET_HOURS = (1, 2, 3, 4)

# Dust is a daytime, wind-driven phenomenon.
DUST_HOURS = (12, 13, 14, 15, 16)

BASELINE_DAYS = 7
_BASELINE_TTL = 6 * 3600.0      # diurnal shape moves slowly; refresh a few times a day
_MAX_WORKERS = 8

# A coarse/fine ratio at or above this reads as mechanically-generated dust.
# Delhi combustion-dominated air sits near 1.5-2.5; construction pushes past 3.
DUST_RATIO_STRONG = 3.0
DUST_RATIO_MILD = 2.2

# How far above a station's own same-hour median counts as "elevated".
ELEVATED_STRONG = 1.6
ELEVATED_MILD = 1.25

# A twin-peak rush/quiet contrast at or above this is a traffic rhythm.
RUSH_RATIO_STRONG = 1.8
RUSH_RATIO_MILD = 1.35

_lock = threading.Lock()
_baseline_cache: tuple[float, dict] | None = None


def _local_hour(iso: str | None) -> int | None:
    """Hour of day in IST for a UTC timestamp."""
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return int((t + dt.timedelta(hours=IST_OFFSET_HOURS)).hour)
    except Exception:
        return None


def _fetch_sensor_hours(sensor_id: int, factor: float) -> list[tuple[int, float]]:
    """(local_hour, value_ug_m3) for the last BASELINE_DAYS of one sensor."""
    since = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(days=BASELINE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = openaq._get(f"/sensors/{sensor_id}/hours",
                       {"datetime_from": since, "limit": 400})
    if not data or "results" not in data:
        return []
    out = []
    for row in data["results"]:
        val = row.get("value")
        if val is None or float(val) < 0:
            continue
        stamp = ((row.get("period") or {}).get("datetimeFrom") or {}).get("utc")
        hr = _local_hour(stamp)
        if hr is None:
            continue
        out.append((hr, float(val) * factor))
    return out


def _profile(series: list[tuple[int, float]]) -> dict | None:
    """Reduce an hourly series to the two numbers the fingerprints need.

    `by_hour` is the median for each hour of day, which is what a live reading is
    compared against. `rush_ratio` contrasts commuting hours with the quiet small
    hours — the shape that separates traffic from a steady industrial source.
    Medians throughout so one spike cannot define a baseline.
    """
    if len(series) < 24:
        return None
    buckets: dict[int, list[float]] = {}
    for hr, val in series:
        buckets.setdefault(hr, []).append(val)
    by_hour = {h: statistics.median(v) for h, v in buckets.items() if v}
    if len(by_hour) < 8:
        return None

    rush = [by_hour[h] for h in MORNING_RUSH + EVENING_RUSH if h in by_hour]
    quiet = [by_hour[h] for h in QUIET_HOURS if h in by_hour]
    rush_ratio = None
    if rush and quiet:
        q = statistics.mean(quiet)
        if q > 0:
            rush_ratio = round(statistics.mean(rush) / q, 2)

    day = [by_hour[h] for h in DUST_HOURS if h in by_hour]
    day_ratio = None
    if day and quiet:
        q = statistics.mean(quiet)
        if q > 0:
            day_ratio = round(statistics.mean(day) / q, 2)

    return {
        "by_hour": {str(h): round(v, 1) for h, v in sorted(by_hour.items())},
        "median": round(statistics.median([v for _, v in series]), 1),
        "rush_ratio": rush_ratio,
        "day_ratio": day_ratio,
        "hours": len(series),
    }


def station_baselines(force: bool = False) -> dict:
    """Per-station diurnal profiles for NO2, SO2 and PM10, keyed by station id.

    Runs from the background refresher, never the request path: this is ~2 calls per
    station. Cached for six hours because a daily rhythm does not change hourly.
    """
    global _baseline_cache
    with _lock:
        if not force and _baseline_cache and time.time() - _baseline_cache[0] < _BASELINE_TTL:
            return _baseline_cache[1]

    locs = openaq.fetch_locations()
    if not locs:
        return {}

    jobs: list[tuple[int, str, int, float]] = []
    for loc in locs:
        for sensor_id, (name, factor) in loc["sensors"].items():
            if name in ("no2", "so2", "pm10", "pm25"):
                jobs.append((loc["id"], name, sensor_id, factor))

    results: dict[int, dict] = {}

    def _one(job):
        sid, name, sensor_id, factor = job
        return sid, name, _profile(_fetch_sensor_hours(sensor_id, factor))

    with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        for sid, name, prof in pool.map(_one, jobs):
            if prof:
                results.setdefault(sid, {})[name] = prof

    with _lock:
        _baseline_cache = (time.time(), results)
    return results


def _level(value: float | None, strong: float, mild: float) -> str:
    if value is None:
        return "unknown"
    if value >= strong:
        return "strong"
    if value >= mild:
        return "moderate"
    return "weak"


def city_context(stations: list[dict]) -> dict:
    """Citywide median for each pollutant at this instant — the normaliser.

    Every station shares the same mixing height right now, so dividing by these
    medians cancels most of the meteorology and leaves local emission strength.
    """
    ctx: dict[str, float] = {}
    for name in ("no2", "so2", "pm10", "pm25"):
        vals = [s["pollutants"][name] for s in stations
                if (s.get("pollutants") or {}).get(name) is not None]
        if len(vals) >= 5:
            ctx[name] = round(statistics.median(vals), 2)
    return ctx


def fingerprint_station(station: dict, context: dict, baselines: dict | None = None) -> dict:
    """Source signatures for one station, normalised against the city at this instant.

    Returns {traffic, industry, construction}, each with a level, the numbers behind
    it, and a plain-language `evidence` string safe to show a citizen. A pollutant we
    do not have yields "unknown" rather than a guess.
    """
    pol = station.get("pollutants") or {}
    base = (baselines or {}).get(station.get("station_id"), {})
    now_hour = _local_hour(station.get("observed_at"))
    in_rush = now_hour in (MORNING_RUSH + EVENING_RUSH) if now_hour is not None else False

    out: dict = {"observed_hour_ist": now_hour, "in_rush_hour": in_rush,
                 "normalised_against": "citywide median at the same hour"}

    def _ratio(name):
        v, med = pol.get(name), context.get(name)
        return (round(v / med, 2) if (v is not None and med) else None), v

    # ---- TRAFFIC: NO2 above the citywide norm ----
    t_ratio, no2 = _ratio("no2")
    t_level = _level(t_ratio, ELEVATED_STRONG, ELEVATED_MILD)
    bits = []
    if no2 is not None:
        bits.append(f"NO₂ {no2:.0f} µg/m³")
    if t_ratio:
        bits.append(f"{t_ratio}× the Delhi median right now")
    if in_rush and t_level in ("strong", "moderate"):
        bits.append("during rush hour")
    prof = (base.get("no2") or {}).get("rush_ratio")
    if prof:
        bits.append(f"local rush/overnight shape {prof}×")
    out["traffic"] = {"level": t_level, "no2": no2, "city_ratio": t_ratio,
                      "diurnal_rush_ratio": prof,
                      "evidence": "; ".join(bits) or "no NO₂ reading available"}

    # ---- INDUSTRY: SO2 above the citywide norm ----
    i_ratio, so2 = _ratio("so2")
    i_level = _level(i_ratio, ELEVATED_STRONG, ELEVATED_MILD)
    bits = []
    if so2 is not None:
        bits.append(f"SO₂ {so2:.0f} µg/m³")
    if i_ratio:
        bits.append(f"{i_ratio}× the Delhi median right now")
    out["industry"] = {"level": i_level, "so2": so2, "city_ratio": i_ratio,
                       "evidence": "; ".join(bits) or "no SO₂ reading available"}

    # ---- CONSTRUCTION / DUST: coarse-to-fine ratio ----
    # No normalisation needed: PM10 and PM2.5 share the same air, so the ratio is
    # already meteorology-independent.
    pm10, pm25 = pol.get("pm10"), pol.get("pm25")
    ratio = round(pm10 / pm25, 2) if (pm10 and pm25) else None
    c_level = _level(ratio, DUST_RATIO_STRONG, DUST_RATIO_MILD)
    bits = []
    if ratio:
        bits.append(f"PM10/PM2.5 ratio {ratio}")
        bits.append("coarse dust dominant" if ratio >= DUST_RATIO_STRONG
                    else "mixed dust and combustion" if ratio >= DUST_RATIO_MILD
                    else "combustion-dominated, little coarse dust")
    out["construction"] = {"level": c_level, "pm10": pm10, "pm25": pm25,
                           "ratio": ratio,
                           "evidence": "; ".join(bits) or "needs both PM10 and PM2.5"}

    # The strongest signal, for a one-word label. "unknown" when nothing reaches
    # moderate — we would rather say nothing than pick a source at random.
    rank = {"strong": 3, "moderate": 2, "weak": 1, "unknown": 0}
    best = max(("traffic", "industry", "construction"),
               key=lambda k: rank[out[k]["level"]])
    out["dominant"] = best if rank[out[best]["level"]] >= 2 else None
    return out


def fingerprint_all(stations: list[dict], baselines: dict | None = None) -> list[dict]:
    """Attach a `fingerprint` to each live station record.

    Needs only the current snapshot: the citywide medians come from `stations`
    itself, so this is cheap enough to run on every refresh.

    IMPORTANT — pass **quality-filtered** stations (advisory.live._clean_pollutants),
    never the raw feed. Run on raw data this happily reports a broken sensor as
    "NO2 5.1x the Delhi median" and a PM10/PM2.5 ratio of 14 as "coarse dust
    dominant", which is how a faulty instrument becomes a confident false claim about
    a real neighbourhood. Bad readings also drag the citywide medians that every
    other station is normalised against.
    """
    ctx = city_context(stations)
    return [{**st, "fingerprint": fingerprint_station(st, ctx, baselines)}
            for st in stations]
