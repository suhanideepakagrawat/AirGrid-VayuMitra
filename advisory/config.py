"""Config + env loading for the advisory feature.

Dependency-free .env loader (no python-dotenv needed) and a cached reader for
config/city.yaml. Everything is resolved lazily so imports never crash when a
key or file is missing.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

# Repo root = two levels up from this file (advisory/config.py -> repo/).
REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "city.yaml"


def load_env(dotenv_path: str | os.PathLike | None = None) -> None:
    """Populate os.environ from a .env file WITHOUT overwriting existing vars.

    Called once at process start (backend + tests). Safe to call repeatedly.
    Format: KEY=VALUE per line; blank lines and '#' comments ignored.
    """
    path = Path(dotenv_path) if dotenv_path else (REPO_ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# Load .env eagerly (idempotent, no-op if absent) so `import advisory.*` works
# in scripts, tests, and uvicorn identically.
load_env()


@lru_cache(maxsize=1)
def city_config() -> dict:
    """Parsed config/city.yaml (cached). Empty dict if the file is missing."""
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def get_city(city_key: str | None = None) -> dict:
    """Return one city's config block; defaults to active_city."""
    cfg = city_config()
    cities = cfg.get("cities", {})
    key = city_key or cfg.get("active_city") or (next(iter(cities), None))
    return {**cities.get(key, {}), "key": key} if key else {}


def languages() -> list[dict]:
    return city_config().get("languages", [{"code": "en", "label": "English"}])


# ---- LLM settings (Groq is OpenAI-compatible; primary provider) -------------
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# NOTE (Aug 2026): Groq decommissioned the llama-3.x line. These are the current
# hosted models on the free tier — gpt-oss-120b composes, gpt-oss-20b translates.
# Avoid qwen3.6-27b here: it emits <think> blocks that leak into citizen copy.
DEFAULT_COMPOSE_MODEL = "openai/gpt-oss-120b"   # rich persona composition
DEFAULT_FAST_MODEL = "openai/gpt-oss-20b"       # translation / intent


def groq_api_key() -> str:
    return os.getenv("GROQ_API_KEY", "").strip()


def compose_model() -> str:
    return os.getenv("GROQ_MODEL", DEFAULT_COMPOSE_MODEL)


def fast_model() -> str:
    return os.getenv("GROQ_FAST_MODEL", DEFAULT_FAST_MODEL)


def reasoning_effort() -> str:
    """Reasoning budget for gpt-oss models. Keep "low" — see advisory/llm.py."""
    return os.getenv("GROQ_REASONING_EFFORT", "low")
