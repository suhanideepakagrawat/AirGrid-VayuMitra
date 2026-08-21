"""The advisory engine — forecast + persona -> structured, cited, composed advice.

Pipeline:
  1. assess()        deterministic: band, persona-escalated risk, guidance flags,
                     CPCB health-band citation.  (Always runs, no LLM.)
  2. compose_message LLM phrasing (Groq) with a deterministic template fallback.
  3. build_advisory  the full object the API/UI consume, in EN + the chosen lang.

Honesty: sensitive personas are escalated ONE band at Moderate+ and told so; every
message ends with a "guidance, not diagnosis" disclaimer (low false-positive tone).
"""
from __future__ import annotations

from . import llm, sources, translate
from .data import get_zone
from .health_bands import Band, band_by_index, band_for_aqi
from .personas import Persona, get_persona

_HORIZONS = ("24", "48", "72")
_DISCLAIMER_EN = "This is health guidance, not a medical diagnosis."


def _pick_horizon(h: str | int | None) -> str:
    h = str(h or "24")
    return h if h in _HORIZONS else "24"


def _guidance(band: Band, persona: Persona) -> dict:
    """Deterministic guidance flags + a persona-escalated effective band."""
    idx = band.index
    sensitive = persona.extra_sensitivity >= 2
    semi = persona.extra_sensitivity == 1  # outdoor workers

    # Escalate the *felt* severity for sensitive groups at Moderate+ (honest bump).
    eff_idx = idx
    if idx >= 2 and persona.extra_sensitivity:
        eff_idx = min(5, idx + (1 if sensitive else 1 if semi and idx >= 3 else 0))
    eff_band = band_by_index(eff_idx)

    # Outdoor recommendation.
    if sensitive:
        outdoor_ok = idx <= 1              # only Good/Satisfactory
    elif semi:
        outdoor_ok = idx <= 2              # up to Moderate, with limits
    else:
        outdoor_ok = idx <= 2              # general public: fine up to Moderate

    if outdoor_ok and idx == 2 and (semi or not sensitive):
        outdoor_en = "Limit prolonged outdoor exertion"
    elif outdoor_ok:
        outdoor_en = "Outdoor activity is generally safe"
    else:
        outdoor_en = "Avoid outdoor activity"

    mask = (sensitive and idx >= 2) or (not sensitive and idx >= 3)
    windows_closed = (sensitive and idx >= 2) or idx >= 3
    meds_handy = persona.key == "respiratory" and idx >= 2

    return {
        "risk_index": idx,
        "effective_index": eff_idx,
        "effective_band_key": eff_band.key,
        "outdoor_ok": outdoor_ok,
        "outdoor_advice_en": outdoor_en,
        "mask": mask,
        "windows_closed": windows_closed,
        "meds_handy": meds_handy,
        "sensitive": bool(persona.extra_sensitivity),
    }


def assess(zone: dict, persona: Persona, horizon: str) -> dict:
    """Deterministic core assessment (no LLM). The single source of truth."""
    aqi = float(zone["forecast"].get(horizon, zone.get("current_aqi", 0)))
    band = band_for_aqi(aqi)
    g = _guidance(band, persona)
    return {
        "zone_id": zone["zone_id"],
        "zone_name": zone.get("name", zone["zone_id"]),
        "lat": zone.get("lat"),
        "lon": zone.get("lon"),
        "horizon_hours": int(horizon),
        "aqi": round(aqi),
        "band": {
            "key": band.key,
            "label_en": band.label_en,
            "label_hi": band.label_hi,
            "range": band.range_str(),
            "color": band.color,
            "cpcb_note_en": band.note_en,
            "cpcb_note_hi": band.note_hi,
        },
        "persona": {"key": persona.key, "label_en": persona.label_en,
                    "label_hi": persona.label_hi, "icon": persona.icon},
        "dominant_source": zone.get("dominant_source"),
        "dominant_source_pct": round(zone.get("dominant_source_pct", 0)),
        "confidence": zone.get("confidence"),
        "confidence_label": zone.get("confidence_label"),
        "guidance": g,
        # The explicit citation object — this is the "RAG-cited health band".
        "citation": {
            "authority": "CPCB National Air Quality Index",
            "band": band.label_en,
            "range": band.range_str(),
            "health_note": band.note_en,
        },
        # Authentic documents this advisory stands on.
        "sources": sources.sources_for(band.key, persona.key),
        "grap_stage": sources.grap_stage(band.key),
        "provenance": sources.citation_line(band),
    }


def _template_message(a: dict, persona: Persona) -> str:
    """Deterministic English advisory sentence (the zero-key fallback)."""
    g = a["guidance"]
    parts = [
        f"Air quality advisory for {a['zone_name']}.",
        f"The forecast air quality is AQI {a['aqi']} "
        f"({a['band']['label_en']}, CPCB {a['band']['range']}) "
        f"over the next {a['horizon_hours']}h, which is "
        f"{a['band']['cpcb_note_en'].rstrip('.').lower()}.",
    ]
    if persona.extra_sensitivity:
        parts.append(
            f"As {persona.profile_en}, you are more sensitive to polluted air."
        )
    parts.append(g["outdoor_advice_en"] + ".")
    extra = []
    if g["mask"]:
        extra.append("Wear an N95 mask outdoors")
    if g["windows_closed"]:
        extra.append("keep windows closed")
    if g["meds_handy"]:
        extra.append("keep quick-relief medication handy")
    if extra:
        parts.append("; ".join(extra).capitalize() + ".")
    if a.get("dominant_source"):
        parts.append(
            f"The dominant pollution source here is {a['dominant_source']} "
            f"({a['dominant_source_pct']}%)."
        )
    parts.append(_DISCLAIMER_EN)
    return " ".join(parts)


def compose_message(a: dict, persona: Persona) -> str:
    """Natural EN advisory — Groq if available, deterministic template otherwise.

    The LLM is fed AUTHENTIC grounding facts (CPCB/WHO/GRAP) and told to phrase
    only what's given — it never sets its own health thresholds.
    """
    g = a["guidance"]
    grounding = sources.grounding_context(band_for_aqi(a["aqi"]), persona)
    facts = (
        f"Area: {a['zone_name']}. Forecast AQI: {a['aqi']} "
        f"({a['band']['label_en']}, CPCB range {a['band']['range']}). "
        f"CPCB note: {a['band']['cpcb_note_en']} "
        f"Horizon: next {a['horizon_hours']} hours. "
        f"Person: {persona.profile_en}. "
        f"Outdoor: {'OK' if g['outdoor_ok'] else 'avoid'}. "
        f"N95 mask: {'yes' if g['mask'] else 'no'}. "
        f"Windows closed: {'yes' if g['windows_closed'] else 'no'}. "
        f"Keep relief meds handy: {'yes' if g['meds_handy'] else 'no'}. "
        f"Dominant source: {a.get('dominant_source')} ({a['dominant_source_pct']}%).\n"
        f"AUTHENTIC GROUNDING (do not contradict, do not add new numbers): {grounding}"
    )
    out = llm.chat(
        [
            {"role": "system", "content": (
                "You write short, calm, actionable public-health air-quality "
                "advisories for ordinary citizens, grounded ONLY in the official "
                "facts provided (CPCB National AQI, WHO 2021, CAQM GRAP). Rules: "
                "3-5 sentences; plain language; cite the AQI number and CPCB band "
                "once; give concrete actions for THIS person; never diagnose; never "
                "invent thresholds or numbers beyond the facts given; end with a "
                "one-line 'health guidance, not a medical diagnosis' style note. "
                "Write PLAIN TEXT only — no markdown, no asterisks, no bullet "
                "points, no headings (the text is shown in a chat bubble and read "
                "aloud by a speech engine)."
            )},
            {"role": "user", "content": facts},
        ],
        temperature=0.4,
        max_tokens=400,
    )
    return out or _template_message(a, persona)


def build_advisory(
    zone_id: str,
    persona_key: str = "general",
    horizon: str | int = "24",
    lang: str = "en",
    city_key: str | None = None,
) -> dict:
    """Full advisory object for the API/UI: assessment + EN message + translation."""
    zone = get_zone(zone_id, city_key)
    if zone is None:
        raise KeyError(f"unknown zone '{zone_id}'")
    persona = get_persona(persona_key)
    horizon = _pick_horizon(horizon)

    a = assess(zone, persona, horizon)
    message_en = compose_message(a, persona)
    message_local = message_en if lang == "en" else translate.translate(message_en, lang)

    a["message_en"] = message_en
    a["message"] = message_local
    a["lang"] = lang
    a["llm_used"] = llm.available()
    return a
