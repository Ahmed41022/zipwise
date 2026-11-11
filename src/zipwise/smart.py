"""Compression level presets and the already-compressed extension list."""

from __future__ import annotations

from pathlib import PurePosixPath

from .errors import UsageError
from .model import Level

LEVEL_PRESETS: dict[str, int] = {"fast": 1, "normal": 6, "best": 9}

COMPRESSED_EXTENSIONS: frozenset[str] = frozenset(
    {
        # archives and compressed streams
        ".zip", ".7z", ".rar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz", ".zst", ".lz",
        ".lzma", ".cab", ".jar", ".apk", ".war", ".whl", ".docx", ".xlsx", ".pptx", ".odt",
        ".ods", ".odp", ".epub", ".nupkg",
        # images
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".avif", ".jxl",
        # audio and video
        ".mp3", ".aac", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".wma", ".mp4", ".m4v",
        ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv", ".mpg", ".mpeg",
        # misc
        ".pdf", ".woff", ".woff2",
    }
)  # fmt: skip


def resolve_level(level: Level) -> int:
    """Map a preset name or an int to a zlib level in 0..9."""
    if isinstance(level, bool):
        raise UsageError(f"Invalid compression level: {level!r}")
    if isinstance(level, int):
        if 0 <= level <= 9:
            return level
        raise UsageError(f"Compression level must be between 0 and 9, got {level}")
    try:
        return LEVEL_PRESETS[level]
    except KeyError:
        names = ", ".join(LEVEL_PRESETS)
        raise UsageError(
            f"Unknown compression level {level!r}", hint=f"Use one of: {names}, or 0-9"
        ) from None


def is_compressed_name(name: str) -> bool:
    return PurePosixPath(name.replace("\\", "/")).suffix.lower() in COMPRESSED_EXTENSIONS


def should_store(name: str, size: int, *, smart: bool = True) -> bool:
    """True when a file should be written uncompressed (empty, or already compressed)."""
    if size == 0:
        return True
    return smart and is_compressed_name(name)
