"""Extraction safety: path containment, bomb limits, and byte budgets.

``plan_extraction`` runs before any byte is written. It maps every archive entry to a
validated relative path or to a blocking :class:`Issue`.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import fsutil
from .errors import BombDetected, IOFailure
from .model import ArchiveEntry, EntryType, Issue

MiB = 1024 * 1024
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:(?:[\\/]|$)")


@dataclass(frozen=True)
class SafetyPolicy:
    """Limits applied to every extraction. All of them can be raised by the caller."""

    max_total_bytes: int | None = None  # None: whatever fits in the free disk space
    max_ratio: float = 250
    ratio_min_bytes: int = 256 * MiB
    max_entries: int = 200_000
    max_entry_bytes: int | None = None
    allow_symlinks: bool = True
    allow_absolute: bool = False
    sanitize_windows_names: Literal["auto", "always", "never"] = "auto"

    def sanitize_windows(self, *, is_windows: bool | None = None) -> bool:
        if self.sanitize_windows_names == "always":
            return True
        if self.sanitize_windows_names == "never":
            return False
        return fsutil.IS_WINDOWS if is_windows is None else is_windows


@dataclass(frozen=True)
class PlannedEntry:
    entry: ArchiveEntry
    parts: tuple[str, ...]
    link_parts: tuple[str, ...] | None = None  # hardlink target, relative to the destination


@dataclass
class ExtractionPlan:
    planned: list[PlannedEntry] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    skipped: int = 0
    total_bytes: int = 0

    def by_index(self) -> dict[int, PlannedEntry]:
        return {p.entry.index: p for p in self.planned}


def _issue(severity: Literal["warning", "error"], code: str, name: str, message: str) -> Issue:
    return Issue(severity, code, name, message)


def split_entry_name(
    name: str, *, allow_absolute: bool = False, strict_drive: bool = True
) -> tuple[tuple[str, ...] | None, str, str]:
    """Normalise an archive path into components.

    Returns ``(parts, error_code, message)``; ``parts`` is None when the name must be blocked.
    Backslashes count as separators, ``.`` components vanish, ``..`` is never accepted.
    A drive root such as ``C:/x`` is always absolute. With ``strict_drive`` a drive-relative
    ``D:x`` is blocked too; otherwise it is an ordinary name that the Windows renaming step
    makes safe.
    """
    if "\0" in name:
        return None, "invalid-name", "name contains a NUL byte"
    text = name.replace("\\", "/")
    raw = text.split("/")
    absolute = text.startswith("/")
    drive = not absolute and bool((_DRIVE_RE if strict_drive else _DRIVE_ROOT_RE).match(raw[0]))
    if absolute or drive:
        if not allow_absolute:
            return None, "absolute-path", "absolute path"
        if drive:
            raw[0] = raw[0][2:]
    parts = tuple(p for p in raw if p not in ("", "."))
    if ".." in parts:
        return None, "path-traversal", "path escapes the destination"
    if not parts:
        return None, "empty-name", "empty name"
    return parts, "", ""


def _link_destination(
    link_parts: tuple[str, ...], target: str, symlink_paths: set[tuple[str, ...]]
) -> tuple[bool, str]:
    """Check that a symlink target stays inside the destination, lexically and without
    walking through another symlink. Returns ``(ok, reason)``."""
    text = target.replace("\\", "/")
    if not text or text.startswith("/") or _DRIVE_RE.match(text):
        return False, "absolute or empty link target"
    stack = list(link_parts[:-1])
    for comp in text.split("/"):
        if comp in ("", "."):
            continue
        if stack and tuple(stack) in symlink_paths:
            return False, "link target passes through another symlink"
        if comp == "..":
            if not stack:
                return False, "link target leaves the destination"
            stack.pop()
        else:
            stack.append(comp)
    return True, ""


def plan_extraction(
    entries: Sequence[ArchiveEntry],
    dest: Path,
    policy: SafetyPolicy,
    *,
    free_space: int | None = None,
) -> ExtractionPlan:
    """Validate every entry before anything is written.

    Raises :class:`BombDetected` when declared sizes break a limit and :class:`IOFailure`
    when the declared total does not fit on disk. Everything else becomes an Issue.
    """
    if len(entries) > policy.max_entries:
        raise BombDetected(
            f"Archive has {len(entries)} entries, more than the limit of {policy.max_entries}",
            hint="Raise the limit if you trust this archive.",
            code="too-many-entries",
        )

    strict_drive = fsutil.IS_WINDOWS and not policy.sanitize_windows()
    symlink_paths: set[tuple[str, ...]] = set()
    for e in entries:
        if e.type is EntryType.SYMLINK:
            parts, _, _ = split_entry_name(
                e.name, allow_absolute=policy.allow_absolute, strict_drive=strict_drive
            )
            if parts:
                symlink_paths.add(parts)

    plan = ExtractionPlan()
    for entry in entries:
        parts, code, message = split_entry_name(
            entry.name, allow_absolute=policy.allow_absolute, strict_drive=strict_drive
        )
        if parts is None:
            if code == "empty-name" and entry.type is EntryType.DIR:
                continue  # "./" style root entries carry nothing
            plan.issues.append(_issue("error", code, entry.name, f"blocked: {message}"))
            plan.skipped += 1
            continue
        if entry.type is EntryType.OTHER:
            plan.issues.append(
                _issue("warning", "special-file", entry.name, "skipped: device, FIFO or socket")
            )
            plan.skipped += 1
            continue
        if any(parts[:i] in symlink_paths for i in range(1, len(parts))):
            plan.issues.append(
                _issue(
                    "error",
                    "symlink-traversal",
                    entry.name,
                    "blocked: path passes through a symlink",
                )
            )
            plan.skipped += 1
            continue

        link_parts: tuple[str, ...] | None = None
        if entry.type is EntryType.SYMLINK:
            if not policy.allow_symlinks:
                plan.issues.append(
                    _issue(
                        "warning",
                        "symlink-disallowed",
                        entry.name,
                        "skipped: symlinks are disabled",
                    )
                )
                plan.skipped += 1
                continue
            ok, reason = _link_destination(parts, entry.link_target or "", symlink_paths)
            if not ok:
                plan.issues.append(
                    _issue("error", "symlink-escape", entry.name, f"blocked: {reason}")
                )
                plan.skipped += 1
                continue
        elif entry.type is EntryType.HARDLINK:
            link_parts, lcode, lmsg = split_entry_name(entry.link_target or "")
            if link_parts is None or any(
                link_parts[:i] in symlink_paths for i in range(1, len(link_parts) + 1)
            ):
                plan.issues.append(
                    _issue(
                        "error",
                        "hardlink-escape",
                        entry.name,
                        "blocked: hardlink target is unsafe "
                        f"({lmsg or 'passes through a symlink'})",
                    )
                )
                plan.skipped += 1
                continue

        plan.planned.append(PlannedEntry(entry, parts, link_parts))
        if entry.type is EntryType.FILE and entry.size > 0:
            plan.total_bytes += entry.size

    _check_declared_sizes(plan, dest, policy, free_space)
    return plan


def _check_declared_sizes(
    plan: ExtractionPlan, dest: Path, policy: SafetyPolicy, free_space: int | None
) -> None:
    compressed_total = 0
    for p in plan.planned:
        e = p.entry
        if e.type is not EntryType.FILE:
            continue
        if policy.max_entry_bytes is not None and e.size > policy.max_entry_bytes:
            raise BombDetected(
                f"Entry {e.name!r} declares {e.size} bytes, above the per-file limit",
                code="entry-too-large",
            )
        if e.compressed_size is not None:
            compressed_total += e.compressed_size
            if e.size >= policy.ratio_min_bytes and e.size > policy.max_ratio * max(
                e.compressed_size, 1
            ):
                raise BombDetected(
                    f"Entry {e.name!r} expands by more than {policy.max_ratio:g}x",
                    hint="This looks like a decompression bomb.",
                    code="ratio",
                )
    if plan.total_bytes >= policy.ratio_min_bytes and compressed_total > 0:  # noqa: SIM102
        if plan.total_bytes > policy.max_ratio * compressed_total:
            raise BombDetected(
                f"Archive expands by more than {policy.max_ratio:g}x",
                hint="This looks like a decompression bomb.",
                code="ratio",
            )
    if policy.max_total_bytes is not None:
        if plan.total_bytes > policy.max_total_bytes:
            raise BombDetected(
                f"Archive declares {plan.total_bytes} bytes, above the limit of "
                f"{policy.max_total_bytes}",
                code="size-limit",
            )
        return
    available = fsutil.free_space(dest) if free_space is None else free_space
    if plan.total_bytes > available:
        raise IOFailure(
            f"Not enough free disk space: need {plan.total_bytes} bytes, {available} available",
            hint="Free some space or extract to another drive.",
            code="disk-full",
        )


class ByteBudget:
    """Counts bytes actually written, because headers can lie about sizes."""

    def __init__(self, max_total: int | None, max_entry: int | None = None) -> None:
        self.max_total = max_total
        self.max_entry = max_entry
        self.total = 0
        self._entry_total = 0
        self._entry_limit: int | None = None
        self._entry_name = ""

    def begin(self, entry: ArchiveEntry) -> None:
        self._entry_total = 0
        self._entry_name = entry.name
        limit = entry.size if entry.size >= 0 else None
        if self.max_entry is not None:
            limit = self.max_entry if limit is None else min(limit, self.max_entry)
        self._entry_limit = limit

    def add(self, nbytes: int) -> None:
        self._entry_total += nbytes
        self.total += nbytes
        if self._entry_limit is not None and self._entry_total > self._entry_limit:
            raise BombDetected(
                f"Entry {self._entry_name!r} produced more data than its header declares",
                hint="The archive header is lying about the size; extraction was stopped.",
                code="size-overrun",
            )
        if self.max_total is not None and self.total > self.max_total:
            raise BombDetected(
                f"Extraction exceeded the total size limit of {self.max_total} bytes",
                code="size-limit",
            )


def with_name(entry: ArchiveEntry, name: str) -> ArchiveEntry:
    """Copy of ``entry`` under another name (the index is kept for the reader)."""
    return dataclasses.replace(entry, name=name)
