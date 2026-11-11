"""User settings stored as one JSON file."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .fsutil import AtomicFile
from .model import ConflictAction, FormatId
from .safety import SafetyPolicy


@dataclass
class Settings:
    language: str = "en"
    default_format: str = FormatId.ZIP.value
    default_level: str = "normal"
    smart_store: bool = True
    verify_after_create: bool = True
    on_conflict: str = ConflictAction.ASK.value
    sanitize_windows_names: str = "auto"
    max_ratio: float = 250.0
    max_entries: int = 200_000
    max_total_bytes: int | None = None
    max_entry_bytes: int | None = None
    allow_symlinks: bool = True

    def to_policy(self) -> SafetyPolicy:
        mode = self.sanitize_windows_names
        if mode not in ("auto", "always", "never"):
            mode = "auto"
        return SafetyPolicy(
            max_total_bytes=self.max_total_bytes,
            max_ratio=self.max_ratio,
            max_entries=self.max_entries,
            max_entry_bytes=self.max_entry_bytes,
            allow_symlinks=self.allow_symlinks,
            sanitize_windows_names=mode,  # type: ignore[arg-type]
        )


def settings_path(*, windows: bool | None = None) -> Path:
    """``%APPDATA%\\Zipwise\\settings.json`` or ``$XDG_CONFIG_HOME/zipwise/settings.json``."""
    if (os.name == "nt") if windows is None else windows:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Zipwise" / "settings.json"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "zipwise" / "settings.json"


def _type_matches(value: Any, default: Any) -> bool:
    if default is None:
        return value is None or (isinstance(value, int) and not isinstance(value, bool))
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, type(default))


def load_settings(path: Path | None = None) -> Settings:
    """Read the settings file; a missing, unreadable or malformed file yields the defaults.

    Unknown keys and values of the wrong type are ignored one by one.
    """
    target = path or settings_path()
    result = Settings()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result
    if not isinstance(data, dict):
        return result
    for f in fields(Settings):
        if f.name in data and _type_matches(data[f.name], getattr(result, f.name)):
            setattr(result, f.name, data[f.name])
    return result


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Write the settings atomically and return the file path."""
    target = path or settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(settings), indent=2, ensure_ascii=False) + "\n"
    atomic = AtomicFile(target, ".tmp")
    with atomic:
        atomic.stream.write(payload.encode("utf-8"))
        atomic.commit()
    return target
