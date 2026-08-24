"""CPCB AQI health-band logic — the grounding layer for every advisory.

Bands + official CPCB health notes come from config/city.yaml so the citizen
message can *cite* the band (name, numeric range, official health note). This
is the "RAG-cited health band" requirement, kept honest and offline.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import city_config

# Ordered severity index — higher = worse. Used for persona escalation.
_ORDER = ["good", "satisfactory", "moderate", "poor", "very_poor", "severe"]


@dataclass(frozen=True)
class Band:
    key: str
    index: int
    label_en: str
    label_hi: str
    lower: int
    upper: int
    color: str
    note_en: str
    note_hi: str

    def label(self, lang: str = "en") -> str:
        return self.label_hi if lang == "hi" else self.label_en

    def note(self, lang: str = "en") -> str:
        return self.note_hi if lang == "hi" else self.note_en

    def range_str(self) -> str:
        return f"{self.lower}-{self.upper if self.upper < 999 else '500+'}"


def _bands() -> list[Band]:
    out: list[Band] = []
    for raw in city_config().get("health_bands", []):
        key = raw["key"]
        lo, hi = raw["range"]
        out.append(
            Band(
                key=key,
                index=_ORDER.index(key) if key in _ORDER else len(out),
                label_en=raw.get("label_en", key.title()),
                label_hi=raw.get("label_hi", key),
                lower=int(lo),
                upper=int(hi),
                color=raw.get("color", "#888888"),
                note_en=raw.get("cpcb_health_note_en", ""),
                note_hi=raw.get("cpcb_health_note_hi", ""),
            )
        )
    return out


# Fallback bands so the module works even if city.yaml is absent.
_FALLBACK = [
    Band("good", 0, "Good", "अच्छा", 0, 50, "#009966",
         "Minimal impact.", "बहुत कम प्रभाव।"),
    Band("satisfactory", 1, "Satisfactory", "संतोषजनक", 51, 100, "#84cf33",
         "Minor breathing discomfort to sensitive people.",
         "संवेदनशील लोगों को हल्की साँस की तकलीफ़।"),
    Band("moderate", 2, "Moderate", "मध्यम", 101, 200, "#ffde33",
         "Breathing discomfort to people with lung, asthma and heart diseases.",
         "फेफड़े, अस्थमा और हृदय रोग वाले लोगों को साँस लेने में तकलीफ़।"),
    Band("poor", 3, "Poor", "ख़राब", 201, 300, "#ff9933",
         "Breathing discomfort to most people on prolonged exposure.",
         "लंबे समय तक रहने पर अधिकांश लोगों को साँस की तकलीफ़।"),
    Band("very_poor", 4, "Very Poor", "बहुत ख़राब", 301, 400, "#cc0033",
         "Respiratory illness on prolonged exposure.",
         "लंबे समय तक रहने पर श्वसन संबंधी बीमारी।"),
    Band("severe", 5, "Severe", "गंभीर", 401, 999, "#7e0023",
         "Affects healthy people and seriously impacts those with existing diseases.",
         "स्वस्थ लोगों को भी प्रभावित करता है और बीमार लोगों पर गंभीर असर।"),
]


def all_bands() -> list[Band]:
    bands = _bands()
    return bands if bands else _FALLBACK


def band_for_aqi(aqi: float) -> Band:
    """Map an AQI value to its CPCB band (clamped to the severe band above 500).

    Selected on the LOWER bound only, deliberately. The published CPCB ranges are
    integer and contiguous only over integers - 0-50, then 51-100, then 101-200 - so a
    fractional value sitting between an upper bound and the next lower bound (50.4,
    100.5, 200.7) matched no band at all under a `lower <= aqi <= upper` test and fell
    through to the "above the top range" case, returning **Severe**.

    That was not cosmetic: interpolated ward AQI is fractional by construction, and a
    ward reading 50.4 was being handed Severe health guidance. Two of 209 wards were in
    that state when this was found.
    """
    bands = sorted(all_bands(), key=lambda b: b.lower)
    # NaN compares False against everything, so guard it rather than letting it pick a
    # band by accident. Missing data must not read as clean air on a health advisory.
    if aqi != aqi:
        return max(bands, key=lambda b: b.index)
    chosen = bands[0]
    for b in bands:
        if aqi >= b.lower:
            chosen = b
    return chosen


def band_by_index(index: int) -> Band:
    bands = all_bands()
    index = max(0, min(index, len(bands) - 1))
    return sorted(bands, key=lambda b: b.index)[index]
