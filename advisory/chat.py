"""Conversational citizen bot — the WhatsApp-style slice.

A citizen asks e.g. "can my child play outside this evening?" and gets a grounded,
forecast-backed, health-band-cited reply in their language. The orchestration:

    question -> (persona, horizon, intent) -> assess zone forecast -> grounded reply

Groq generates the natural reply when available; a deterministic intent+template
path answers the same question offline (RULE 3). Every reply is anchored to the
real assessment object, so the bot can never contradict the numbers.
"""
from __future__ import annotations

from . import llm, sources, translate
from .advisory_engine import assess
from .data import find_zone_by_text, get_zone
from .health_bands import band_for_aqi
from .personas import detect_persona, get_persona

# Keyword buckets for the offline intent router.
_OUTDOOR = ("outside", "outdoor", "out for", "go out", "play", "walk", "run",
            "jog", "exercise", "cycle", "market", "बाहर", "खेल", "टहल")
_MASK = ("mask", "n95", "मास्क")
_WHEN = {"tomorrow": "48", "day after": "72", "48": "48", "72": "72",
         "कल": "48", "परसों": "72"}


def _route(message: str, default_horizon: str) -> tuple[str, str]:
    """(intent, horizon) from free text — offline, deterministic."""
    low = message.lower()
    horizon = default_horizon
    for kw, h in _WHEN.items():
        if kw in low:
            horizon = h
            break
    if any(k in low for k in _MASK):
        return "mask", horizon
    if any(k in low for k in _OUTDOOR):
        return "outdoor", horizon
    return "general", horizon


def _template_reply(a: dict, persona, intent: str) -> str:
    g = a["guidance"]
    band = a["band"]
    head = (f"In {a['zone_name']} the forecast is AQI {a['aqi']} "
            f"({band['label_en']}, CPCB {band['range']}) over the next "
            f"{a['horizon_hours']}h.")
    cite = f"(CPCB: {band['cpcb_note_en']})"

    if intent == "outdoor":
        yn = "Yes" if g["outdoor_ok"] else "No"
        who = persona.profile_en
        if g["outdoor_ok"]:
            body = (f"{yn} — for {who} this level is acceptable, though "
                    f"{'limit long, intense activity' if a['aqi'] >= 101 else 'enjoy it'}.")
        else:
            body = (f"{yn} — it's best to stay indoors. For {who} this air is "
                    f"risky right now.")
        tail = "Wear an N95 mask if you must step out." if g["mask"] else ""
    elif intent == "mask":
        body = ("Yes, wear a well-fitted N95 mask outdoors."
                if g["mask"] else
                "A mask isn't essential at this level, but sensitive people may still prefer one.")
        tail = ""
    else:
        actions = []
        if not g["outdoor_ok"]:
            actions.append("avoid outdoor activity")
        if g["mask"]:
            actions.append("wear an N95 outdoors")
        if g["windows_closed"]:
            actions.append("keep windows closed")
        body = ("Advice: " + ", ".join(actions) + "."
                if actions else "Conditions are okay; carry on normally.")
        tail = ""

    return " ".join(p for p in [head, body, tail, cite,
                                "This is health guidance, not a medical diagnosis."] if p)


def _llm_reply(a: dict, persona, question: str, history: list[dict] | None) -> str | None:
    g = a["guidance"]
    grounding = sources.grounding_context(band_for_aqi(a["aqi"]), persona)
    facts = (
        f"Area: {a['zone_name']}. Forecast AQI (next {a['horizon_hours']}h): "
        f"{a['aqi']} ({a['band']['label_en']}, CPCB range {a['band']['range']}). "
        f"CPCB health note: {a['band']['cpcb_note_en']} "
        f"Person asking about: {persona.profile_en}. "
        f"Deterministic assessment -> outdoor: "
        f"{'OK' if g['outdoor_ok'] else 'AVOID'}; N95 mask: "
        f"{'yes' if g['mask'] else 'no'}; windows closed: "
        f"{'yes' if g['windows_closed'] else 'no'}. "
        f"Dominant pollution source: {a.get('dominant_source')} "
        f"({a['dominant_source_pct']}%).\n"
        f"AUTHENTIC GROUNDING (do not contradict / do not add numbers): {grounding}"
    )
    msgs = [
        {"role": "system", "content": (
            "You are a calm, trustworthy air-quality health assistant for citizens "
            "in India. Answer the user's specific question directly (if yes/no, say "
            "so first). Ground every answer ONLY in the FACTS block (CPCB National "
            "AQI, WHO 2021, CAQM GRAP): cite the AQI number and CPCB band once. "
            "Respect the assessment (never contradict the outdoor/mask flags). 2-4 "
            "sentences, plain language, no diagnosis, no invented thresholds, and a "
            "short 'guidance, not diagnosis' note."
        )},
        {"role": "system", "content": f"FACTS: {facts}"},
    ]
    for turn in (history or [])[-4:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        msgs.append({"role": role, "content": str(turn.get("content", ""))})
    msgs.append({"role": "user", "content": question})
    return llm.chat(msgs, temperature=0.4, max_tokens=350)


def answer(
    zone_id: str,
    message: str,
    persona_key: str | None = None,
    lang: str = "en",
    horizon: str | int = "24",
    history: list[dict] | None = None,
    city_key: str | None = None,
) -> dict:
    """Answer a citizen question, grounded in the zone forecast."""
    # If the user named a different area in their message, follow them there.
    switched = find_zone_by_text(message, city_key)
    zone = switched or get_zone(zone_id, city_key)
    if zone is None:
        raise KeyError(f"unknown zone '{zone_id}'")

    # Persona: explicit selection wins; otherwise infer from the question.
    persona = get_persona(persona_key) if persona_key else detect_persona(message)
    intent, horizon = _route(message, str(horizon or "24"))
    if str(horizon) not in ("24", "48", "72"):
        horizon = "24"

    a = assess(zone, persona, horizon)

    # Try the grounded agent pipeline first: it retrieves regulatory passages and the
    # ward's evidence chain, drafts from those only, and has a verifier that can reject
    # its own draft. `used` is false for every failure mode, and we fall back to the
    # single-call path that shipped before.
    agent: dict = {"used": False}
    try:
        from advisory import agents as agents_mod
        agent = agents_mod.answer(message, zone, persona.key, str(horizon))
    except Exception:
        agent = {"used": False, "reason": "agents unavailable"}

    if agent.get("used"):
        reply_en = agent["reply"]
    else:
        reply_en = _llm_reply(a, persona, message, history) or _template_reply(a, persona, intent)
    reply_local = reply_en if lang == "en" else translate.translate(reply_en, lang)

    return {
        "zone_id": a["zone_id"],
        "zone_name": a["zone_name"],
        "zone_switched": bool(switched),
        "persona": persona.key,
        "intent": intent,
        "horizon_hours": a["horizon_hours"],
        "aqi": a["aqi"],
        "band": a["band"],
        "citation": a["citation"],
        "sources": a["sources"],
        "provenance": a["provenance"],
        "grap_stage": a["grap_stage"],
        "outdoor_ok": a["guidance"]["outdoor_ok"],
        "reply_en": reply_en,
        "reply": reply_local,
        "lang": lang,
        "llm_used": llm.available(),
        # How the reply was produced, so the caller can show its provenance rather than
        # having to trust it.
        "reasoning": {
            "mode": "agentic_rag" if agent.get("used") else "single_pass",
            "verified": bool(agent.get("verified")),
            "citations": agent.get("citations") or [],
            "evidence_chain": agent.get("evidence_chain"),
            "agents": [t.get("agent") for t in agent.get("trace", [])],
            "fallback_reason": None if agent.get("used") else agent.get("reason"),
        },
    }
