"""File-name rules: Windows-invalid names, case clashes, unique renames."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence

RESERVED_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
    | {f"COM{c}" for c in "¹²³"}
    | {f"LPT{c}" for c in "¹²³"}
)
INVALID_CHARS = frozenset('<>:"|?*')


def is_reserved(name: str) -> bool:
    """True for CON, aux.txt, "NUL ." and friends: the device is addressed whatever follows."""
    stem = name.split(".", 1)[0].rstrip(" ")
    return stem.upper() in RESERVED_NAMES


def sanitize_component(name: str, windows: bool = True) -> str:
    """Make one path component valid on Windows (or just free of NUL when ``windows`` is False)."""
    if not windows:
        return name.replace("\0", "_") or "_"
    cleaned = "".join("_" if (c in INVALID_CHARS or ord(c) < 32) else c for c in name)
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        return "_"
    if is_reserved(cleaned):
        cleaned = "_" + cleaned
    return cleaned


def numbered_name(name: str, number: int) -> str:
    """``report.txt`` -> ``report (2).txt``; dot-files keep their dot in front."""
    dot = name.rfind(".")
    if dot <= 0:
        return f"{name} ({number})"
    return f"{name[:dot]} ({number}){name[dot:]}"


def unique_name(name: str, exists: Callable[[str], bool]) -> str:
    """First of ``name``, ``name (2)``, ``name (3)``... for which ``exists`` is false."""
    if not exists(name):
        return name
    n = 2
    while exists(numbered_name(name, n)):
        n += 1
    return numbered_name(name, n)


def fold(text: str) -> str:
    """Key under which a case-insensitive file system sees two names as equal."""
    return unicodedata.normalize("NFC", text).casefold()


class CaseClashResolver:
    """Detects entries that differ only by case (or Unicode normalisation).

    On a case-sensitive target nothing changes. On a case-insensitive one, directories
    merge into the first spelling seen and a clashing *file* becomes ``name (2).ext``.
    """

    def __init__(self, case_insensitive: bool) -> None:
        self.case_insensitive = case_insensitive
        self._dirs: dict[tuple[str, ...], str] = {}
        self._files: dict[tuple[str, ...], set[str]] = {}

    def resolve(self, parts: Sequence[str], *, is_dir: bool = False) -> tuple[str, ...]:
        """Return the (possibly rewritten) components for an entry."""
        if not self.case_insensitive:
            return tuple(parts)
        out: list[str] = []
        key: list[str] = []
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            key.append(fold(part))
            tkey = tuple(key)
            if not last or is_dir:
                canonical = self._dirs.setdefault(tkey, part)
                out.append(canonical)
                continue
            seen = self._files.setdefault(tuple(key[:-1]) + (key[-1],), set())
            if part in seen or not seen:
                seen.add(part)
                out.append(part)
                continue
            candidate = part
            n = 2
            while True:
                candidate = numbered_name(part, n)
                ckey = tuple(key[:-1]) + (fold(candidate),)
                if ckey not in self._files:
                    break
                n += 1
            self._files[ckey] = {candidate}
            out.append(candidate)
        return tuple(out)
