"""Tiny message catalogue: flat JSON files with dotted keys, English as the fallback.

This module is Qt-free so the CLI can reuse it.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_LANGUAGE = "en"
RTL_LANGUAGES = frozenset({"ar", "he", "fa", "ur"})

_DIR = Path(__file__).parent
_catalogues: dict[str, dict[str, str]] = {}
_current = DEFAULT_LANGUAGE


def available_languages() -> list[str]:
    """Language codes that have a catalogue file, English first."""
    codes = sorted(p.stem for p in _DIR.glob("*.json"))
    if DEFAULT_LANGUAGE in codes:
        codes.remove(DEFAULT_LANGUAGE)
        codes.insert(0, DEFAULT_LANGUAGE)
    return codes


def load(lang: str) -> dict[str, str]:
    """Return (and cache) the catalogue for ``lang``; an unknown language yields an empty one."""
    if lang not in _catalogues:
        data: dict[str, str] = {}
        try:
            raw = json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError):
            pass
        _catalogues[lang] = data
    return _catalogues[lang]


def reload() -> None:
    """Forget cached catalogues (used by tests that edit files)."""
    _catalogues.clear()


def current_language() -> str:
    return _current


def set_language(lang: str) -> str:
    """Select the active language; unknown codes fall back to English. Returns the one used."""
    global _current
    _current = lang if lang in available_languages() else DEFAULT_LANGUAGE
    return _current


def is_rtl(lang: str | None = None) -> bool:
    return (lang or _current) in RTL_LANGUAGES


def tr(key: str, **fmt: object) -> str:
    """Translate ``key`` into the active language.

    A key missing there falls back to English, and one missing in both is returned as is.
    ``{name}`` placeholders are filled from ``fmt``; a placeholder without a value stays visible.
    """
    text = load(_current).get(key)
    if text is None:
        text = load(DEFAULT_LANGUAGE).get(key, key)
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return text
    return text
