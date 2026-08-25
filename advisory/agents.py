"""A four-stage agent pipeline for grounded citizen answers.

Why this is not one LLM call. The single-call advisory works, and it has one structural
weakness we could not close from inside the prompt: nothing checks what came back. If the
model invents a threshold, softens a warning, or attributes a claim to WHO that WHO does
not make, the sentence ships. Prompt instructions reduce that; they do not detect it.

So the work is split across four stages that can each fail independently, and one of them
exists purely to catch the others:

    ROUTER      what is being asked, and what evidence would answer it
                (fast model, JSON out)
    RETRIEVER   pull that evidence - BM25 passages from the regulatory corpus, and the
                ward's evidence chain from the knowledge graph (deterministic, no model)
    ANALYST     compose an answer using only the retrieved evidence
                (composition model)
    VERIFIER    check the draft against the retrieved evidence and the deterministic
                band, and reject it if it asserts something unsupported
                (fast model, JSON out)

A rejected draft is not retried into a worse one: it falls back to the deterministic
template, which is always correct if plainer. That is the whole point - the pipeline can
only ever degrade toward the safe answer.

Every stage is individually optional. No key, a timeout, a malformed response, anything:
`answer()` returns `used=False` and the caller uses its existing path unchanged. This
module is additive and nothing depends on it succeeding.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from advisory import graph as kg
from advisory import llm, rag
from advisory.health_bands import band_by_index, band_for_aqi
from advisory.personas import PERSONAS, Persona

#: Two switches, deliberately. ENABLED decides whether the pipeline exists at all.
#: CHAT_ENABLED decides whether the citizen chat route uses it, which is a separate
#: question: the pipeline adds about four seconds to a reply, and a live demo may want
#: chat fast while /ai/pipeline, /rag/search and /graph stay genuinely up and inspectable.
#: Turning chat off never makes the API claim a capability it does not have.
ENABLED = os.getenv("AGENTS", "1") != "0"
CHAT_ENABLED = ENABLED and os.getenv("AGENTS_CHAT", "1") != "0"

_INTENTS = ("current", "forecast", "source", "action", "rule", "general")


def available() -> bool:
    return ENABLED and llm.available() and rag.available()


# ---------------------------------------------------------------------------
# 1 · Router
# ---------------------------------------------------------------------------

#: Keyword intent routing. This used to be an LLM call. Measured against the raw
#: question, the model's rewritten search query retrieved *worse* - on "Can my child play
#: outside this evening?" the raw question finds "Sensitive groups" and "Populations at
#: higher risk", while the rewrite ("Delhi child outdoor activity air quality regulation")
#: found GRAP and NCAP boilerplate instead, because generic padding words match generic
#: passages. It also cost about 1.3 s of a 6.8 s reply. Deterministic is faster, more
#: accurate here, and cannot fail.
_INTENT_WORDS: dict[str, tuple[str, ...]] = {
    "rule": ("why", "how is", "how do you", "rule", "standard", "guideline", "regulation",
             "grap", "cpcb", "who ", "average", "calculated", "measured"),
    "source": ("source", "causing", "cause", "polluting", "blame", "responsible",
               "traffic", "industry", "dust", "construction", "burning", "stubble"),
    "forecast": ("tomorrow", "forecast", "next", "later", "predict", "48", "72", "day after"),
    "action": ("should i", "can i", "can my", "is it safe", "safe to", "wear", "mask",
               "go out", "outside", "outdoor", "exercise", "run", "walk", "play"),
    "current": ("now", "right now", "today", "current", "at the moment"),
}


def _route(question: str) -> dict:
    """Intent by keyword, and the raw question as the retrieval query."""
    low = question.lower()
    intent = "general"
    # Ordered by specificity: a question about a rule stays a rule question even when it
    # also contains "should I".
    for key in ("rule", "source", "forecast", "action", "current"):
        if any(w in low for w in _INTENT_WORDS[key]):
            intent = key
            break
    return {"intent": intent, "search_query": question[:200]}


def _parse_json(raw: str | None) -> dict | None:
    """Tolerate the fence and the preamble that models add despite being told not to."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        got = json.loads(text[start:end + 1])
        return got if isinstance(got, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 2 · Retriever (deterministic)
# ---------------------------------------------------------------------------

def _retrieve(plan: dict, zone_id: str, persona_key: str) -> dict:
    # Retrieval is deterministic and free, so it always runs. The router's opinion
    # about whether regulation is "needed" only shapes the query, never whether the
    # analyst gets grounding - an ungrounded draft is one the verifier must reject,
    # and it did, for every question.
    block, passages = rag.context_block(plan["search_query"], k=3)

    try:
        chain = kg.explain_ward(zone_id, persona_key)
    except Exception:
        chain = None

    return {"passages": passages, "context": block, "chain": chain}


# ---------------------------------------------------------------------------
# 3 · Analyst
# ---------------------------------------------------------------------------

_ANALYST_SYS = (
    "You are VayuMitra, a public-health air-quality assistant for Delhi. "
    "Write 2-4 short sentences of plain, warm, practical guidance for the person asking. "
    "You may use ONLY the facts in EVIDENCE and MEASURED. "
    "Never invent a number, a threshold, a pollutant value or a regulation. "
    "Never contradict the stated band. "
    "Do not use markdown, bullets, asterisks or headings - this may be read aloud. "
    "End with: This is health guidance, not a medical diagnosis."
)


def _analyse(question: str, persona: Persona, measured: dict, ret: dict) -> str | None:
    ev = ret["context"] or "(no regulatory passage retrieved)"
    chain = ret["chain"]
    chain_line = chain["chain"] if chain else "(no evidence chain available)"

    user = (
        f"QUESTION: {question[:600]}\n\n"
        f"ASKING ON BEHALF OF: {persona.label_en}"
        + (f" - IMPORTANT: guidance for this person is taken from a band "
           f"{persona.extra_sensitivity} step(s) MORE SEVERE than the measured band. "
           f"Treat the air as WORSE than measured for them, never better. Their advice "
           f"must be at least as cautious as the general public's, never more relaxed."
           if persona.extra_sensitivity else "")
        + "\n\n"
        f"MEASURED:\n"
        f"- ward: {measured['ward']}\n"
        f"- AQI: {measured['aqi']}\n"
        f"- CPCB band: {measured['band_label']} ({measured['band_range']})\n"
        f"- dominant source: {measured.get('dominant') or 'not established'}\n\n"
        f"EVIDENCE CHAIN: {chain_line}\n\n"
        f"EVIDENCE (regulatory passages, quote or paraphrase only these):\n{ev}\n"
    )
    out = llm.chat(
        [{"role": "system", "content": _ANALYST_SYS},
         {"role": "user", "content": user}],
        temperature=0.3, max_tokens=520, timeout=22.0,
    )
    return llm.strip_markdown(out).strip() if out else None


# ---------------------------------------------------------------------------
# 4 · Verifier
# ---------------------------------------------------------------------------

def _stated_numbers(text: str) -> list[str]:
    """Numbers the draft asserts as measurements, not ordinary prose numbers.

    Only figures attached to a unit or an index word count. "one of the three" is not a
    measurement; "PM2.5 of 45 ug/m3" and "AQI 210" are.
    """
    out: list[str] = []
    for m in re.finditer(
        r"(?:aqi|pm2\.5|pm10|no2|so2|o3|index)\D{0,12}?(\d{1,3}(?:\.\d)?)"
        r"|(\d{1,3}(?:\.\d)?)\s*(?:ug/m3|µg/m³|ug/m³|micrograms)",
        text.lower(),
    ):
        out.append(m.group(1) or m.group(2))
    return out


def _verify(draft: str, measured: dict, ret: dict) -> dict:
    """Deterministic verification. No model call, and therefore no added latency.

    This began as a second LLM call. Two things were wrong with that. It cost roughly
    four seconds on every reply, which is a lot to spend on a citizen waiting for health
    advice. And a fact-checker that is itself a language model can hallucinate its own
    verdict - it approved drafts it should not have, and rejected correctly escalated
    ones, until it was given the same evidence as the analyst.

    Checking claims against the grounding facts in code is faster, free, and cannot
    itself invent anything. Every rule below is a claim we can actually adjudicate.
    """
    reasons: list[str] = []

    # (a) Numeric claims must trace to a fact we hold. The grounding surface is the
    # measured values plus the retrieved passages - anything else is invented.
    supported = {str(measured["aqi"]), str(measured["aqi"]) + ".0"}
    supported |= set(re.findall(r"\d{1,3}(?:\.\d)?", measured["band_range"]))
    supported |= set(re.findall(r"\d{1,3}(?:\.\d)?", ret.get("context") or ""))
    if ret.get("chain"):
        supported |= set(re.findall(r"\d{1,3}(?:\.\d)?", ret["chain"].get("chain") or ""))
    invented = [n for n in _stated_numbers(draft) if n not in supported]
    if invented:
        reasons.append(f"states {invented[0]} as a measurement, which is not in the evidence")

    # (b) An authority may only be named if a retrieved passage came from it.
    cited_ok = " ".join(
        (p.get("publisher") or "") + " " + (p.get("title") or "") for p in ret["passages"]
    ).lower()
    for name, token in (("WHO", "world health"), ("SAFAR", "safar"),
                        ("NCAP", "national clean air"), ("GRAP", "graded response")):
        if re.search(rf"\b{name}\b", draft) and token not in cited_ok:
            reasons.append(f"cites {name}, which no retrieved passage supports")
            break

    return {"ok": not reasons, "reason": "; ".join(reasons)[:200], "checked": True,
            "model": "deterministic"}


_BAND_WORDS = {
    "good": "good", "satisfactory": "satisfactory", "moderate": "moderate",
    "poor": "poor", "very poor": "very poor", "severe": "severe",
}


def _contradicts_band(draft: str, allowed: set[str], measured_label: str) -> str | None:
    """A deterministic backstop the verifier cannot talk its way past.

    Naming a CPCB band that is neither the measured band nor the escalated band this
    persona is guided by is a factual error, whatever the fact-checking model concluded.

    Both are allowed on purpose. For a child at Moderate, "treat this as Very Poor" is
    the rule working, not a contradiction - an earlier version of this check accepted
    only the measured band and rejected every correctly escalated draft.
    """
    low = draft.lower()
    # "good", "poor" and "moderate" are ordinary English - "a good idea", "poor
    # visibility", "moderate exercise". Matching them bare rejected correct drafts on 2
    # of 5 test sentences. A band word only counts as a band claim when it sits in the
    # vocabulary of banding.
    words = "very poor|severe|satisfactory|moderate|good|poor"
    pattern = (
        rf"(?:aqi|air quality|category|band|rated|classified|falls in(?:to)?)"
        rf"[^.]{{0,26}}?\b({words})\b"
        rf"|\b({words})\b[^.]{{0,18}}?(?:band|category)"
    )
    named = [(m.group(1) or m.group(2)) for m in re.finditer(pattern, low)]
    if not named:
        return None
    # "very poor" contains "poor"; keep the longer reading.
    if "very poor" in named:
        named = [n for n in named if n != "poor"]
    wrong = [n for n in named if n not in allowed]
    return (f"draft names band '{wrong[0]}', which is neither the measured band "
            f"'{measured_label}' nor this persona's escalated band"
            if wrong else None)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def answer(question: str, zone: dict, persona_key: str = "general",
           horizon: str = "24") -> dict:
    """Run the pipeline. Always returns a dict; `used` says whether to trust `reply`."""
    trace: list[dict] = []
    t0 = time.time()

    if not available():
        return {"used": False, "reason": "agents unavailable", "trace": trace}

    persona = PERSONAS.get(persona_key) or PERSONAS["general"]
    aqi = float(zone.get("current_aqi") or 0)
    band = band_for_aqi(aqi)
    measured = {
        "ward": zone.get("name", zone.get("zone_id")),
        "aqi": round(aqi),
        "band_label": band.label_en,
        "band_range": band.range_str(),
        "dominant": zone.get("dominant_source"),
    }

    try:
        step = time.time()
        plan = _route(question)
        trace.append({"agent": "router", "ms": int((time.time() - step) * 1000),
                      "model": "deterministic", "intent": plan["intent"]})

        step = time.time()
        ret = _retrieve(plan, zone.get("zone_id", ""), persona.key)
        trace.append({"agent": "retriever", "ms": int((time.time() - step) * 1000),
                      "passages": len(ret["passages"]),
                      "chain": bool(ret["chain"])})

        step = time.time()
        draft = _analyse(question, persona, measured, ret)
        trace.append({"agent": "analyst", "ms": int((time.time() - step) * 1000),
                      "drafted": bool(draft)})
        if not draft:
            return {"used": False, "reason": "analyst produced nothing", "trace": trace}

        step = time.time()
        verdict = _verify(draft, measured, ret)
        # The persona's escalated band is a legitimate thing for the draft to name.
        escalated = band_by_index(band.index + persona.extra_sensitivity)
        allowed = {band.label_en.lower(), escalated.label_en.lower()}
        hard = _contradicts_band(draft, allowed, band.label_en)
        if hard:
            verdict = {"ok": False, "reason": hard, "checked": True}
        trace.append({"agent": "verifier", "ms": int((time.time() - step) * 1000),
                      **verdict})

        if not verdict["ok"]:
            return {"used": False, "reason": f"rejected: {verdict['reason']}",
                    "rejected_draft": draft, "trace": trace,
                    "citations": _citations(ret)}

        return {
            "used": True,
            "reply": draft,
            "intent": plan["intent"],
            "citations": _citations(ret),
            "evidence_chain": ret["chain"]["chain"] if ret["chain"] else None,
            "verified": verdict.get("checked", False),
            "total_ms": int((time.time() - t0) * 1000),
            "trace": trace,
        }
    except Exception as exc:  # never let the pipeline break the caller
        trace.append({"agent": "orchestrator", "error": type(exc).__name__})
        return {"used": False, "reason": "pipeline error", "trace": trace}


def _citations(ret: dict) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for p in ret["passages"]:
        key = f"{p['publisher']}|{p['year']}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"publisher": p["publisher"], "year": p["year"],
                    "url": p["url"], "heading": p["heading"]})
    return out


def describe() -> dict:
    """What the pipeline is, for the API and for anyone asking how it works."""
    return {
        "available": available(),
        "enabled": ENABLED,
        "used_by_chat": CHAT_ENABLED,
        "llm_calls_per_reply": 1,
        "latency_note": (
            "one model call per reply, the same as before the pipeline existed: routing, "
            "retrieval and verification are deterministic"),
        "agents": [
            {"name": "router", "model": "deterministic",
             "job": "classify intent by keyword and set the retrieval query"},
            {"name": "retriever", "model": "deterministic",
             "job": "BM25 over the regulatory corpus plus the ward evidence chain from the knowledge graph"},
            {"name": "analyst", "model": "gpt-oss-120b",
             "job": "compose an answer from retrieved evidence only"},
            {"name": "verifier", "model": "deterministic",
             "job": "reject invented figures, unsupported citations, and any band that contradicts the measurement"},
        ],
        "on_rejection": "fall back to the deterministic CPCB template",
        "corpus": rag.stats(),
        "graph": kg.stats(),
    }
