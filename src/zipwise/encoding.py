"""Archive file-name encoding detection.

Minimal version: strict UTF-8 when the names decode, otherwise CP437 (the historical ZIP
default). The public signatures are final; the scoring detector replaces the body later.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EncodingGuess:
    encoding: str
    confidence: float
    alternatives: tuple[str, ...] = ()


def detect_names(raw_names: list[bytes], hint_locale: str | None = None) -> EncodingGuess:
    """Guess the encoding shared by ``raw_names``."""
    if all(all(b < 0x80 for b in raw) for raw in raw_names):
        return EncodingGuess("ascii", 1.0)
    try:
        for raw in raw_names:
            raw.decode("utf-8")
    except UnicodeDecodeError:
        return EncodingGuess("cp437", 0.3)
    return EncodingGuess("utf-8", 0.9, ("cp437",))


def decode_name(raw: bytes, encoding: str) -> str:
    """Decode one name; undecodable bytes become U+FFFD instead of failing the archive."""
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        return raw.decode(encoding, errors="replace")
