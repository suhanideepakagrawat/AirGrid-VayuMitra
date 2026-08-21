"""Groq LLM client — the single 'called' model in Feature 4.

Groq exposes an OpenAI-compatible endpoint. This client is deliberately tiny and
DEFENSIVE: it never raises on a missing key, a timeout, or a bad response — it
returns None and lets the caller fall back to a deterministic template. That is
what keeps the demo alive with no key / no network (RULE 3).

Pattern mirrors the proven client in sviam-interview-lab (Groq-first, lazy key,
timeout -> fallback).
"""
from __future__ import annotations

import re

import requests

from .config import GROQ_URL, compose_model, fast_model, groq_api_key, reasoning_effort


_MD_BOLD = re.compile(r"\*{1,3}(.+?)\*{1,3}", re.S)
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_BULLET = re.compile(r"^\s{0,3}[-*+]\s+", re.M)


def strip_markdown(text: str) -> str:
    """Flatten markdown to plain text.

    The citizen UI escapes bot text (`esc()` in advisory_demo.html), so an LLM
    that returns "**not a diagnosis**" would literally show the asterisks — and
    the TTS voice would read them. gpt-oss reaches for markdown far more than
    the old llama models did, so every LLM reply is flattened here, centrally.
    """
    if not text:
        return text
    out = _MD_HEAD.sub("", text)
    out = _MD_BULLET.sub("", out)
    out = _MD_BOLD.sub(r"\1", out)
    out = out.replace("`", "")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _is_reasoning_model(model: str) -> bool:
    """True for Groq-hosted models that emit billed reasoning tokens."""
    m = (model or "").lower()
    return "gpt-oss" in m or "qwen3" in m


def available() -> bool:
    """True if a Groq key is configured (does not guarantee the network works)."""
    return bool(groq_api_key())


def chat(
    messages: list[dict],
    *,
    fast: bool = False,
    model: str | None = None,
    temperature: float = 0.4,
    max_tokens: int = 700,
    timeout: float = 20.0,
    json_mode: bool = False,
) -> str | None:
    """Call Groq chat/completions. Return the assistant text, or None on any failure.

    `fast=True` selects the small/quick model (good for translation + intent).
    """
    key = groq_api_key()
    if not key:
        return None

    mdl = model or (fast_model() if fast else compose_model())
    body: dict = {
        "model": mdl,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # gpt-oss (and qwen3) are REASONING models: hidden reasoning tokens are billed
    # against max_tokens. Measured on gpt-oss-120b at max_tokens=700 — effort
    # "medium" burned 698 reasoning tokens and returned an EMPTY message (the
    # advisory would silently fall back to a template); effort "low" spends ~6 and
    # answers in full, twice as fast. So we pin "low" for these models.
    if _is_reasoning_model(mdl):
        body["reasoning_effort"] = reasoning_effort()
    if json_mode:
        # Provider-enforced valid JSON — the prompt must mention "JSON".
        body["response_format"] = {"type": "json_object"}

    try:
        resp = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        if content and str(content).strip():
            text = str(content).strip()
            # Never touch json_mode payloads — stripping would corrupt the JSON.
            return text if json_mode else strip_markdown(text)
        return None
    except Exception:
        # Any error (no network, timeout, rate limit, bad key) -> caller falls back.
        return None
