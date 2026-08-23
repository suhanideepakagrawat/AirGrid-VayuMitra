"""The "right now" layer — live ward AQI from real government station readings.

This answers the question a judge asks first: *is this live?* It is, and it is real
instrument data, not a model estimate. OpenAQ republishes the CPCB / DPCC / IMD /
IITM / HSPCB feeds, so these numbers agree with the CPCB app by construction.

Kept strictly separate from the forecast layer:

    /live   -> what the instruments read in the last few hours   (this module)
    /wards  -> what our trained XGBoost models predict for +24/48/72 h

WHY INVERSE-DISTANCE WEIGHTING AND NOT THE TRAINED SPATIAL ESTIMATOR
-------------------------------------------------------------------
`models/spatial_estimator.json` is the better interpolator. IDW was chosen because
xgboost was not a dependency of the advisory service and the gap was small: the
model's leave-one-station-out RMSE was 85.78 against IDW's 93.28, about 8%.

**Both halves of that reasoning have since weakened, and this should be revisited.**
Krishna retrained the estimator (23 Aug): LOSO RMSE is now **72.79 against IDW's
85.39 — a 14.8% gap, not 8%**. And xgboost is now installed anyway, for the forecast
refresh subprocess, so the dependency argument no longer applies either.

It is left on IDW for the finale only because this path is deployed and verified,
and swapping a working component two days out is the larger risk. Every ward still
ships `nearest_station_km` and `n_stations` so the uncertainty is visible. Switching
to the trained estimator is the first thing to do afterwards.

With ~60 live stations spread across Delhi, IDW over the nearest few is a sound,
transparent choice, and every ward ships `nearest_station_km` and `n_stations` so a
reader can judge for themselves how much to trust a given ward. The trained models
remain the differentiator where they matter — the 24/48/72 h forecast.

Degrades gracefully everywhere: no key, no network, or no fresh station simply
yields {"available": false, ...} and the UI falls back to the forecast layer.
"""
from __future__ import annotations

import datetime as dt
import math

from . import fingerprints as fp
from . import fire as fire_mod
from . import openaq
from .health_bands import band_for_aqi

# CPCB National AQI breakpoints (CPCB, 2014): concentration low/high -> index low/high.
# Mirrors AQI_BP in ml_pipeline/fetch_real_data.py. Duplicated rather than imported so
# the advisory service stays decoupled from the pandas/numpy pipeline; it is a
# published constant that does not drift.
AQI_BP: dict[str, list[tuple[float, float, float, float]]] = {
    "pm25": [(0, 30, 0, 50), (30, 60, 51, 100), (60, 90, 101, 200),
             (90, 120, 201, 300), (120, 250, 301, 400), (250, 500, 401, 500)],
    "pm10": [(0, 50, 0, 50), (50, 100, 51, 100), (100, 250, 101, 200),
             (250, 350, 201, 300), (350, 430, 301, 400), (430, 600, 401, 500)],
    "no2":  [(0, 40, 0, 50), (40, 80, 51, 100), (80, 180, 101, 200),
             (180, 280, 201, 300), (280, 400, 301, 400), (400, 800, 401, 500)],
    "so2":  [(0, 40, 0, 50), (40, 80, 51, 100), (80, 380, 101, 200),
             (380, 800, 201, 300), (800, 1600, 301, 400), (1600, 2100, 401, 500)],
    "o3":   [(0, 50, 0, 50), (50, 100, 51, 100), (100, 168, 101, 200),
             (168, 208, 201, 300), (208, 748, 301, 400), (748, 1000, 401, 500)],
}

PRETTY = {"pm25": "PM2.5", "pm10": "PM10", "no2": "NO₂", "so2": "SO₂", "o3": "O₃"}

IDW_K = 3            # nearest stations blended per ward
IDW_POWER = 2.0      # matches IDW_POWER in ml_pipeline/train_spatial_estimator.py
MAX_STATION_KM = 25.0


def sub_index(conc: float, pollutant: str) -> float | None:
    """CPCB sub-index for one pollutant, or None if out of table range."""
    for clo, chi, ilo, ihi in AQI_BP.get(pollutant, []):
        if clo <= conc <= chi:
            return ilo + (ihi - ilo) / (chi - clo) * (conc - clo)
    return None


def station_aqi(pollutants: dict[str, float]) -> tuple[float, str] | None:
    """CPCB AQI for a station: the WORST sub-index, and which pollutant drove it.

    Taking the max (not an average) is the CPCB definition — the index reports the
    most dangerous pollutant present, which is also what makes the "dominant
    pollutant" label meaningful.
    """
    best: tuple[float, str] | None = None
    for name, conc in pollutants.items():
        si = sub_index(float(conc), name)
        if si is None:
            continue
        if best is None or si > best[0]:
            best = (si, name)
    return best


# CPCB averaging windows. The National AQI is defined on a 24-hour average for
# PM2.5, PM10, NO2 and SO2, and on the highest 8-hour rolling mean for O3 and CO.
# Pushing a spot reading through the same breakpoints is a different index with the
# same name - and it runs high at night, when the boundary layer collapses and the
# hourly value sits far above the day's mean.
AVG_HOURS = {"pm25": 24, "pm10": 24, "no2": 24, "so2": 24, "o3": 8}

# Fraction of stations that must have a usable history before we switch the whole
# network onto CPCB windows.
WINDOW_COVERAGE_MIN = 0.6


# CPCB will not publish a sub-index from a sparse day: a 24-hour average needs at
# least 16 hourly values. We relax it to two thirds of the window, because OpenAQ
# mirrors CPCB with gaps of its own and a 16-hour floor would drop good stations -
# but a "24-hour mean" computed from two readings is not one, and those fall back
# to the spot value instead.
MIN_WINDOW_FRACTION = 2 / 3


def _cpcb_average(values: list[float], hours: int) -> float | None:
    """The concentration CPCB would index: a 24-hour mean, or for O3 the highest
    8-hour rolling mean in the window (CPCB takes the worst 8-hour block, not the
    latest one). None when the window is too sparse to average honestly."""
    vals = [v for v in values if v is not None]
    if len(vals) < max(2, int(hours * MIN_WINDOW_FRACTION)):
        return None
    if hours >= 24 or len(vals) <= hours:
        return sum(vals) / len(vals)
    blocks = [sum(vals[i:i + hours]) / hours for i in range(len(vals) - hours + 1)]
    return max(blocks) if blocks else sum(vals) / len(vals)


def _apply_cpcb_windows(stations: list[dict]) -> tuple[list[dict], int]:
    """Swap each station's spot readings for the CPCB averaging window.

    Falls back to the spot reading per pollutant when no window is available, so a
    slow sensor degrades one number rather than dropping the station. Returns the
    stations and how many pollutant values were actually averaged.
    """
    try:
        windows = openaq.cached_station_windows()
    except Exception:
        return stations, 0
    if not windows:
        return stations, 0

    # Never mix bases. A field where some stations carry a 24-hour mean and others a
    # spot reading is not a measurement of anything - the spatial contrast between two
    # wards would then partly reflect which station happened to have history. Either
    # most of the network is averaged or none of it is.
    covered = sum(1 for st in stations if windows.get(st.get("station_id")))
    if not stations or covered / len(stations) < WINDOW_COVERAGE_MIN:
        return stations, 0

    averaged = 0
    for st in stations:
        w = windows.get(st.get("station_id"))
        if not w:
            continue
        pol = dict(st.get("pollutants") or {})
        for name, series in w.items():
            if name not in AVG_HOURS:
                continue
            mean = _cpcb_average(series, AVG_HOURS[name])
            if mean is not None:
                pol[name] = round(mean, 2)
                averaged += 1
        st["pollutants"] = pol
        st["averaging"] = "cpcb_window"
    return stations, averaged


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# Physically plausible ambient ranges for Delhi (ug/m3). Anything outside is a
# malfunctioning sensor, not weather.
PLAUSIBLE: dict[str, tuple[float, float]] = {
    "pm25": (1.0, 1000.0),
    "pm10": (1.0, 2000.0),
    "no2":  (1.0, 400.0),
    "so2":  (1.0, 300.0),
    "o3":   (0.0, 400.0),
}

# A coarse/fine ratio above this is not achievable in real aerosol — it means the
# PM2.5 channel has failed. Real Delhi values sit near 1.5-4.
MAX_PM_RATIO = 8.0

# Robust outlier gate, in median-absolute-deviations from the citywide median.
MAD_LIMIT = 6.0


def _clean_pollutants(stations: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop individual bad readings before they can distort a ward.

    Faulty stations are not hypothetical: one Delhi station was live-reporting NO2 at
    238 ug/m3 against a citywide median of 48, while simultaneously reporting PM2.5 at
    5.0 with PM10 at 69.9 — a 14:1 coarse/fine ratio that cannot occur physically.
    Left alone it pushed a whole ward to AQI 259 off a single broken instrument.

    Filtering is per-pollutant, not per-station, so a station with one bad channel
    still contributes its good ones. Returns (cleaned, notes-for-transparency).
    """
    notes: list[str] = []

    # Pass 1 — absolute plausibility and cross-channel consistency.
    cleaned: list[dict] = []
    for s in stations:
        pol = dict(s.get("pollutants") or {})
        for name in list(pol):
            lo, hi = PLAUSIBLE.get(name, (float("-inf"), float("inf")))
            if not (lo <= pol[name] <= hi):
                notes.append(f"{s['station']}: {name}={pol[name]} outside plausible range")
                pol.pop(name)
        pm25, pm10 = pol.get("pm25"), pol.get("pm10")
        if pm25 and pm10 and pm10 / pm25 > MAX_PM_RATIO:
            notes.append(f"{s['station']}: PM10/PM2.5={pm10 / pm25:.1f} implausible, dropping pm25")
            pol.pop("pm25")
        if pol:
            cleaned.append({**s, "pollutants": pol})

    # Pass 2 — robust citywide outlier rejection (median +/- MAD_LIMIT * MAD).
    # Median/MAD rather than mean/sd so a handful of broken sensors cannot widen the
    # gate that is meant to catch them.
    for name in PLAUSIBLE:
        vals = sorted(s["pollutants"][name] for s in cleaned if name in s["pollutants"])
        if len(vals) < 8:
            continue                       # too few to judge — leave them alone
        med = vals[len(vals) // 2]
        devs = sorted(abs(v - med) for v in vals)
        mad = devs[len(devs) // 2]
        if mad <= 0:
            continue
        for s in cleaned:
            v = s["pollutants"].get(name)
            if v is not None and abs(v - med) > MAD_LIMIT * mad:
                notes.append(f"{s['station']}: {name}={v} is a citywide outlier "
                             f"(median {med:.0f}), dropped")
                s["pollutants"].pop(name)

    return [s for s in cleaned if s["pollutants"]], notes


def _scored_stations(stations: list[dict]) -> tuple[list[dict], list[str]]:
    """Attach a CPCB AQI to each station; drop any we cannot index."""
    cleaned, notes = _clean_pollutants(stations)
    out = []
    for s in cleaned:
        res = station_aqi(s.get("pollutants") or {})
        if res is None:
            continue
        aqi, driver = res
        out.append({**s, "aqi": round(aqi, 1), "dominant_pollutant": driver})
    return out, notes


def _interpolate(ward_lat: float, ward_lon: float, scored: list[dict]) -> dict | None:
    """IDW the k nearest live stations onto one ward centroid."""
    near = sorted(
        ((_haversine_km(ward_lat, ward_lon, s["lat"], s["lon"]), s) for s in scored),
        key=lambda p: p[0],
    )[:IDW_K]
    near = [(d, s) for d, s in near if d <= MAX_STATION_KM]
    if not near:
        return None

    # A ward centroid can sit essentially on top of a station — use it directly
    # rather than dividing by ~zero.
    if near[0][0] < 0.5:
        d, s = near[0]
        return {"aqi": s["aqi"], "dominant_pollutant": s["dominant_pollutant"],
                "pollutants": dict(s.get("pollutants") or {}),
                "nearest_station": s["station"], "nearest_station_km": round(d, 2),
                "n_stations": 1, "observed_at": s.get("observed_at"),
                "fingerprint": s.get("fingerprint")}

    wsum = 0.0
    aqi_acc = 0.0
    pol_acc: dict[str, float] = {}
    pol_w: dict[str, float] = {}
    for d, s in near:
        # Distance is not the only thing that makes a reading relevant. Stations
        # report on their own schedules, so a ward can sit between one reading taken
        # minutes ago and another taken hours ago - and across a weather change (a
        # thunderstorm, a wind shift) the older one is not merely less precise, it
        # describes a different atmosphere. Recency joins distance in the weight.
        age = float(s.get("age_hours") or 0.0)
        w = (1.0 / (d ** IDW_POWER)) * (1.0 / (1.0 + age))
        wsum += w
        aqi_acc += w * s["aqi"]
        for p, v in (s.get("pollutants") or {}).items():
            pol_acc[p] = pol_acc.get(p, 0.0) + w * float(v)
            pol_w[p] = pol_w.get(p, 0.0) + w

    pollutants = {p: round(pol_acc[p] / pol_w[p], 1) for p in pol_acc}

    # Interpolate the CONCENTRATIONS, then apply the CPCB formula once - do not
    # interpolate the AQI itself. AQI is a max-of-sub-indices, so it is not linear
    # in concentration, and each pollutant carries its own weight sum here (a
    # station missing PM10 contributes to PM2.5 but not to PM10). Blending the two
    # independently let a ward report "AQI 36, driven by PM10" next to a PM10 of
    # 144 ug/m3, which is 130 on the CPCB scale. 56 of 209 wards disagreed with
    # their own pollutant panel by more than 15 points, up to 117.
    driver = station_aqi(pollutants)
    aqi = driver[0] if driver else aqi_acc / wsum
    return {"aqi": round(aqi, 1),
            "dominant_pollutant": driver[1] if driver else near[0][1]["dominant_pollutant"],
            "pollutants": pollutants,
            "nearest_station": near[0][1]["station"],
            "nearest_station_km": round(near[0][0], 2),
            "n_stations": len(near),
            "observed_at": near[0][1].get("observed_at"),
            "fingerprint": near[0][1].get("fingerprint")}


_result_cache: tuple[float, dict] | None = None
_RESULT_TTL = 900.0     # a shade longer than the refresher's interval


def cached_live_wards(zones: list[dict]) -> dict:
    """Non-blocking read for the request path — cache, or a 'warming' state.

    Building the live layer takes ~15 s because every station needs its own /latest
    call. A request must never wait for that: on a cold free-tier instance the first
    visitor would sit through a 20 s block, and a proxy may cut them off first. The
    background refresher in backend/advisory_api.py fills the cache; this returns
    whatever is ready.

    Serving slightly stale data with its age attached is always better than blocking,
    and strictly better than failing.
    """
    import time as _time

    if not openaq.available():
        return {"available": False, "reason": "OPENAQ_API_KEY not configured",
                "wards": [], "stations": 0}
    if _result_cache and _time.time() - _result_cache[0] < _RESULT_TTL:
        return _result_cache[1]
    return {"available": False, "state": "warming",
            "reason": "live station data is being fetched — retry shortly",
            "wards": [], "stations": 0}


def live_wards(zones: list[dict], force: bool = False) -> dict:
    """Live AQI for every ward. Always returns the {available, ...} contract.

    Blocking — call from the background refresher, not the request path.
    """
    if not openaq.available():
        return {"available": False,
                "reason": "OPENAQ_API_KEY not configured",
                "wards": [], "stations": 0}

    stations = openaq.live_stations(force=force)
    # Index the CPCB-defined average, not the latest hour. Measured against our own
    # stations this is worth ~50 AQI on the city mean and 157 at Anand Vihar, and it
    # is the difference between matching what CPCB publishes and not.
    stations, averaged = _apply_cpcb_windows(stations)
    scored, quality_notes = _scored_stations(stations)
    # Fingerprints run on the CLEANED set only — see fingerprint_all's docstring.
    scored = fp.fingerprint_all(scored)
    if not scored:
        return {"available": False,
                "reason": "no station reported within the freshness window",
                "wards": [], "stations": 0}

    wards = []
    for z in zones:
        got = _interpolate(z["lat"], z["lon"], scored)
        if not got:
            continue
        band = band_for_aqi(got["aqi"])
        wards.append({
            "zone_id": z["zone_id"], "name": z["name"],
            "lat": z["lat"], "lon": z["lon"],
            "aqi": round(got["aqi"]),
            "band": band.key, "band_label": band.label_en, "color": band.color,
            "dominant_pollutant": PRETTY.get(got["dominant_pollutant"],
                                             got["dominant_pollutant"]),
            "pollutants": got["pollutants"],
            "nearest_station": got["nearest_station"],
            "nearest_station_km": got["nearest_station_km"],
            "n_stations": got["n_stations"],
            "observed_at": got["observed_at"],
            # Source signature measured at the ward's nearest station. Attached from
            # that one station rather than blended: a fingerprint is a statement
            # about a place, and averaging three of them would blur the very local
            # contrast it exists to detect.
            "fingerprint": got.get("fingerprint"),
        })

    ages = sorted(s.get("age_hours") for s in stations if s.get("age_hours") is not None)
    stamps = sorted(s.get("observed_at") or "" for s in stations if s.get("observed_at"))
    newest = stamps[-1] if stamps else None
    # The layer is only as current as its TYPICAL station, not its luckiest one.
    # Reporting min(ages) stamped the whole city "measured 26m ago" while half the
    # readings were over three hours old - which matters enormously when the
    # weather turns between the two.
    typical = stamps[len(stamps) // 2] if stamps else None
    result = {
        "available": True,
        "source": "OpenAQ v3 — CPCB / DPCC / IMD / IITM / HSPCB ground stations",
        "method": (f"CPCB National AQI per station, then inverse-distance weighting "
                   f"(k={IDW_K}, power={IDW_POWER:g}) onto ward centroids"),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "observed_at": typical,
        "observed_at_newest": newest,
        "data_age_hours": round(ages[len(ages) // 2], 2) if ages else None,
        "data_age_hours_newest": round(ages[0], 2) if ages else None,
        "data_age_hours_oldest": round(ages[-1], 2) if ages else None,
        "stations": len(scored),
        "stations_fetched": len(stations),
        "averaging": ("CPCB windows: 24 h mean for PM2.5/PM10/NO2/SO2, highest 8 h "
                      "mean for O3" if averaged else
                      "latest hourly reading - 24 h history still loading, so this "
                      "cycle reads high at night"),
        "values_averaged": averaged,
        # Surfaced rather than hidden: a reader can see exactly which readings we
        # rejected and why, which is the honest way to run a quality filter.
        "quality_filtered": quality_notes,
        # Regional biomass burning is a CITY-level signal, not a per-ward one: a
        # plume arriving from 200 km upwind covers the whole of Delhi, so pinning it
        # to individual wards would invent precision we do not have.
        "regional_burning": fire_mod.burning_signal(stations),
        "wards": wards,
    }

    global _result_cache
    import time as _time
    _result_cache = (_time.time(), result)
    return result
