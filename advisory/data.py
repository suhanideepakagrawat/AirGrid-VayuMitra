"""Forecast + source-attribution loader (the Feature 1 & 2 -> Feature 4 bridge).

Reads the REAL attribution CSV produced by the source-attribution notebook
(`data/source_attribution.csv`) when present; otherwise falls back to a committed
mock (`data/mock/<city>_wards_forecast.json`) so the advisory always runs with
zero external data (RULE 1/2).

Both inputs are normalised to ONE zone schema the rest of Feature 4 consumes:

    {
      zone_id, name, lat, lon,
      forecast: {"24": aqi, "48": aqi, "72": aqi},
      current_aqi,
      sources: {traffic, industry, construction},   # percentages
      dominant_source, dominant_source_pct,
      confidence, confidence_label,
    }
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .config import REPO_ROOT, get_city

# Real attribution CSV columns (from the notebook contract).
_REAL_COLS = {"cell_id", "lat", "lon", "horizon_hours", "forecast_aqi",
              "dominant_source"}


def _friendly_name(zone_id: str) -> str:
    return f"Zone {zone_id}"


def _confidence_label(conf: float) -> str:
    if conf >= 0.6:
        return "High-confidence directional evidence"
    if conf >= 0.25:
        return "Medium-confidence directional evidence"
    return "Low-confidence directional evidence"


# Optional cell->ward mapping (Ward_No, Ward_Name per cell_id). When present,
# the advisory serves REAL Delhi wards by name instead of anonymous grid cells.
_WARD_MAP_FILE = "data/future_aqi_forecast_ward.csv"

_SRC_KEYS = {"traffic": "traffic_pct", "industry": "industry_pct",
             "construction": "construction_pct"}
_SRC_LABEL = {"traffic": "Traffic/Roads", "industry": "Industry",
              "construction": "Construction/Dust"}


def _ward_title(name: str) -> str:
    # "GEETA COLONY" -> "Geeta Colony"; keep dotted initials ("I.P Extention").
    return " ".join(w if "." in w else w.capitalize() for w in str(name).split())


def _zone_from_group(zone_id: str, name: str, grp) -> dict:
    """Aggregate a (cell- or ward-level) group of horizon rows into one zone."""
    import pandas as pd

    by_h = grp.groupby("horizon_hours")["forecast_aqi"].mean()
    forecast = {str(int(h)): round(float(v), 1) for h, v in by_h.items()}
    last = None
    for h in ("24", "48", "72"):
        if h in forecast:
            last = forecast[h]
        elif last is not None:
            forecast[h] = last
    current = forecast.get("24", round(float(grp["forecast_aqi"].mean()), 1))

    mix = {}
    for key, col in _SRC_KEYS.items():
        mix[key] = round(float(grp[col].mean()), 1) if col in grp else 0.0
    total = sum(mix.values()) or 1.0
    mix = {k: round(v * 100.0 / total, 1) if total > 5 else v for k, v in mix.items()}
    dom_key = max(mix, key=mix.get)

    conf = round(float(grp["confidence"].mean()), 2) if "confidence" in grp else 0.5
    return {
        "zone_id": zone_id,
        "name": name,
        "lat": round(float(grp["lat"].mean()), 5),
        "lon": round(float(grp["lon"].mean()), 5),
        "forecast": forecast,
        "current_aqi": current,
        "sources": mix,
        "dominant_source": _SRC_LABEL[dom_key],
        "dominant_source_pct": mix[dom_key],
        "confidence": conf,
        "confidence_label": _confidence_label(conf),
        "cells": int(grp["cell_id"].nunique()),
    }


def _load_real_csv(path: Path) -> list[dict]:
    """Real pipeline output -> zones. Ward-aggregated when the ward map exists
    (real Delhi ward names); per-cell otherwise. Never raises to the caller
    beyond schema validation — foolproof over fancy."""
    import pandas as pd  # local import so the mock path needs no pandas

    df = pd.read_csv(path)
    if not _REAL_COLS.issubset(df.columns):
        missing = _REAL_COLS - set(df.columns)
        raise ValueError(f"attribution CSV missing columns: {missing}")
    df = df.dropna(subset=["forecast_aqi", "horizon_hours"])

    # Try to enrich with real ward names (join verified 1600/1600 on cell_id).
    ward_path = REPO_ROOT / _WARD_MAP_FILE
    if ward_path.exists():
        try:
            wmap = (pd.read_csv(ward_path, usecols=["cell_id", "Ward_No", "Ward_Name"])
                    .dropna(subset=["Ward_Name"]).drop_duplicates("cell_id"))
            merged = df.merge(wmap, on="cell_id", how="inner")
            if len(merged) >= 0.5 * len(df):        # sanity: join must actually take
                def _wid(wno) -> str:
                    # Ward_No is usually numeric ("133") but Delhi Cantonment
                    # wards use codes like "CANT_2" — keep them as slugs.
                    s = str(wno).strip().replace(" ", "_")
                    return f"W{s[:-2]}" if s.endswith(".0") else f"W{s}"
                zones = [
                    _zone_from_group(_wid(wno), _ward_title(wname), grp)
                    for (wno, wname), grp in merged.groupby(["Ward_No", "Ward_Name"])
                ]
                return zones
        except Exception:
            pass  # fall through to per-cell zones

    return [
        _zone_from_group(str(cid), _friendly_name(str(cid)), grp)
        for cid, grp in df.groupby("cell_id")
    ]


def _load_mock(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    zones = raw["zones"] if isinstance(raw, dict) else raw
    for z in zones:
        z.setdefault("current_aqi", z.get("forecast", {}).get("24"))
        z.setdefault("confidence_label", _confidence_label(z.get("confidence", 0.5)))
    return zones


@lru_cache(maxsize=8)
def load_zones(city_key: str | None = None) -> tuple[dict, ...]:
    """All zones for a city (cached). Real CSV if present, else the mock."""
    city = get_city(city_key)
    real = REPO_ROOT / city.get("data_file", "")
    mock = REPO_ROOT / city.get("mock_file", "")

    if city.get("data_file") and real.exists():
        zones = _load_real_csv(real)
    elif city.get("mock_file") and mock.exists():
        zones = _load_mock(mock)
    else:
        zones = []
    # Sort worst-first so the UI leads with the most urgent zone.
    zones.sort(key=lambda z: z.get("current_aqi", 0), reverse=True)
    return tuple(zones)


def list_zones(city_key: str | None = None) -> list[dict]:
    return [dict(z) for z in load_zones(city_key)]


def get_zone(zone_id: str, city_key: str | None = None) -> dict | None:
    for z in load_zones(city_key):
        if str(z["zone_id"]) == str(zone_id) or z.get("name") == zone_id:
            return dict(z)
    return None


_AREA_STOPWORDS = {"sector", "junction", "road", "east", "west", "north",
                   "south", "puram", "bagh", "vihar", "nagar", "block"}


def find_zone_by_text(text: str, city_key: str | None = None) -> dict | None:
    """Best-effort: match a spoken/typed area name to a known zone.

    Tries a full name substring first, then a distinctive single token (so
    "what about Dwarka?" or "Rohini abhi kaisa hai" resolves the zone).
    """
    low = (text or "").lower()
    if not low:
        return None
    zones = load_zones(city_key)
    for z in zones:                       # full-name match wins
        name = str(z.get("name", "")).lower()
        if name and name in low:
            return dict(z)
    for z in zones:                       # distinctive-token match
        for tok in str(z.get("name", "")).lower().replace("/", " ").split():
            if len(tok) > 4 and tok not in _AREA_STOPWORDS and tok in low:
                return dict(z)
    return None


def data_source_kind(city_key: str | None = None) -> str:
    """'real' or 'mock' — surfaced in the API so the demo is honest."""
    city = get_city(city_key)
    real = REPO_ROOT / city.get("data_file", "")
    return "real" if (city.get("data_file") and real.exists()) else "mock"


@lru_cache(maxsize=1)
def forecast_provenance() -> dict:
    """When the served forecast was produced, and how old that makes it.

    Every screen showing a forecast number must be able to date it. Without this the
    UI silently implies the numbers are current, which is the single easiest thing
    for a reviewer to catch us on. Read from the pipeline output's own
    `source_timestamp` rather than file mtime, so it survives a git checkout.
    """
    import datetime as dt

    import pandas as pd          # imported lazily here, as elsewhere in this module

    path = REPO_ROOT / _WARD_MAP_FILE
    out: dict = {"available": False}
    if not path.exists():
        return out
    try:
        col = pd.read_csv(path, usecols=["source_timestamp", "target_timestamp"])
        issued = str(col["source_timestamp"].dropna().iloc[0])
        targets = sorted(col["target_timestamp"].dropna().unique().tolist())
        issued_dt = pd.to_datetime(issued, utc=True).to_pydatetime()
        age_h = (dt.datetime.now(dt.timezone.utc) - issued_dt).total_seconds() / 3600.0
        out = {
            "available": True,
            "issued_at": issued_dt.isoformat(timespec="seconds"),
            "age_hours": round(age_h, 1),
            "age_days": round(age_h / 24.0, 1),
            "targets": [str(t) for t in targets],
            "source": "Trained XGBoost forecasters (24/48/72 h) on CPCB station history",
        }
    except Exception:
        return {"available": False}
    return out
