"""Formatting of results: text, JSON and the progress bar. Knows nothing about argparse."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TextIO

from ..jobs import ProgressEvent
from ..model import ArchiveEntry, EntryType, Issue

_PHASE_LABELS = {
    "scan": "Scanning",
    "compress": "Compressing",
    "verify": "Verifying",
    "split": "Splitting",
    "extract": "Extracting",
    "test": "Testing",
    "join": "Joining",
}


@dataclass
class Result:
    """What a command produced, independent of how it will be shown."""

    command: str | None
    exit_code: int = 0
    ok: bool = True
    lines: list[str] = field(default_factory=list)  # the answer itself (always printed)
    details: list[str] = field(default_factory=list)  # extra stdout lines, dropped by --quiet
    notes: list[str] = field(default_factory=list)  # stderr diagnostics, dropped by --quiet
    summary: str | None = None
    issues: list[Issue] = field(default_factory=list)
    data: dict[str, object] = field(default_factory=dict)  # command-specific JSON keys
    error: dict[str, str | None] | None = None


def human_size(nbytes: int) -> str:
    """``1234567`` -> ``1.2 MB`` (decimal units, like the split presets)."""
    if nbytes < 1000:
        return f"{nbytes} B"
    value = float(nbytes)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if value < 1000 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{nbytes} B"  # pragma: no cover - the loop always returns


def percent(part: int, whole: int) -> str:
    return f"{round(part * 100 / whole)}%" if whole > 0 else "-"


def saving_percent(packed: int, whole: int) -> str | None:
    """``packed`` as a percentage of ``whole``, or None when packing did not make it smaller.

    Capped at 99% so a tiny saving never reads as "100%".
    """
    if whole <= 0 or packed < 0 or packed >= whole:
        return None
    return f"{min(round(packed * 100 / whole), 99)}%"


def _local_time(when: datetime) -> str:
    """Entry times are stored in UTC; people expect their own clock in a listing."""
    if when.tzinfo is not None:
        when = when.astimezone()
    return when.strftime("%Y-%m-%d %H:%M")


def arrow(stream: TextIO) -> str:
    """``→`` when the stream can encode it, else ``->``."""
    try:
        "→".encode(getattr(stream, "encoding", None) or "utf-8")
    except (UnicodeEncodeError, LookupError):
        return "->"
    return "→"


def plural(count: int, word: str) -> str:
    return (
        f"{count} {word}"
        if count == 1
        else f"{count} {word[:-1] + 'ies' if word.endswith('y') else word + 's'}"
    )


def issue_line(issue: Issue) -> str:
    prefix = f"{issue.severity}: "
    return f"{prefix}{issue.entry}: {issue.message}" if issue.entry else f"{prefix}{issue.message}"


# -- listing ------------------------------------------------------------------------------

_TYPE_FLAG = {
    EntryType.FILE: "-",
    EntryType.DIR: "d",
    EntryType.SYMLINK: "l",
    EntryType.HARDLINK: "h",
    EntryType.OTHER: "?",
}


def _entry_name(entry: ArchiveEntry) -> str:
    if entry.link_target and entry.type in (EntryType.SYMLINK, EntryType.HARDLINK):
        return f"{entry.name} -> {entry.link_target}"
    return entry.name


def format_listing(entries: Sequence[ArchiveEntry], *, long: bool) -> list[str]:
    """Names only, or a table of size, packed size, ratio, date, flags and name."""
    if not long:
        return [_entry_name(e) for e in entries]
    rows: list[tuple[str, str, str, str, str, str]] = []
    for e in entries:
        is_file = e.type is EntryType.FILE
        size = human_size(e.size) if is_file and e.size >= 0 else "-"
        packed = human_size(e.compressed_size) if is_file and e.compressed_size is not None else "-"
        ratio = (
            saving_percent(e.compressed_size, e.size) or "-"
            if is_file and e.compressed_size is not None
            else "-"
        )
        when = _local_time(e.mtime) if e.mtime else "-"
        flags = _TYPE_FLAG[e.type] + ("E" if e.encrypted else "-")
        rows.append((size, packed, ratio, when, flags, _entry_name(e)))
    header = ("Size", "Packed", "Ratio", "Modified", "Flags", "Name")
    widths = [
        max(len(header[i]), *(len(r[i]) for r in rows)) if rows else len(header[i])
        for i in range(5)
    ]

    def fmt(row: Sequence[str]) -> str:
        cells = [row[i].rjust(widths[i]) if i < 3 else row[i].ljust(widths[i]) for i in range(5)]
        return "  ".join([*cells, row[5]])

    return [fmt(header), *(fmt(r) for r in rows)]


def listing_footer(entries: Sequence[ArchiveEntry]) -> str:
    files = [e for e in entries if e.type is EntryType.FILE]
    total = sum(max(e.size, 0) for e in files)
    text = f"{plural(len(entries), 'entry')}, {human_size(total)}"
    if files and all(e.compressed_size is not None for e in files):
        packed = sum(e.compressed_size or 0 for e in files)
        text += f", {human_size(packed)} packed"
        saved = saving_percent(packed, total)
        if saved is not None:
            text += f" ({saved})"
    return text


# -- text and JSON output -----------------------------------------------------------------


def render_text(result: Result, out: TextIO, err: TextIO, *, quiet: bool) -> None:
    for line in result.lines:
        print(line, file=out)
    if not quiet:
        for line in result.details:
            print(line, file=out)
    for issue in result.issues:
        if issue.severity != "info" or not quiet:
            print(issue_line(issue), file=err)
    if not quiet:
        for line in result.notes:
            print(line, file=err)
    if result.error is not None:
        print(f"error: {result.error['message']}", file=err)
        if result.error.get("hint"):
            print(f"hint: {result.error['hint']}", file=err)
    if result.summary and not quiet:
        print(result.summary, file=out)
    out.flush()
    err.flush()


def result_document(result: Result) -> dict[str, object]:
    """The single JSON document for a result. Key order and types are stable."""
    doc: dict[str, object] = {"ok": result.ok, "command": result.command}
    if result.error is None:
        doc.update(result.data)
        doc["ok"] = result.ok
    else:
        doc.update({k: v for k, v in result.data.items() if k not in ("ok", "command")})
        doc["error"] = result.error
    doc["exit_code"] = result.exit_code
    return doc


def render_json(result: Result, out: TextIO) -> None:
    doc = result_document(result)
    try:
        out.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    except UnicodeEncodeError:
        out.write(json.dumps(doc, ensure_ascii=True, indent=2) + "\n")
    out.flush()


# -- progress ---------------------------------------------------------------------------


class ProgressBar:
    """A single line on ``stream`` that is rewritten in place with ``\\r``."""

    def __init__(self, stream: TextIO, columns: int | None = None) -> None:
        self._stream = stream
        self._columns = columns
        self._last = 0

    def _width(self) -> int:
        cols = self._columns or shutil.get_terminal_size((80, 24)).columns
        return max(20, cols - 1)

    def format(self, event: ProgressEvent) -> str:
        label = _PHASE_LABELS.get(event.phase, event.phase)
        if event.bytes_total > 0:
            pct = min(100, event.bytes_done * 100 // event.bytes_total)
            counts = f"{human_size(event.bytes_done)}/{human_size(event.bytes_total)}"
        elif event.entries_total > 0:
            pct = min(100, event.entries_done * 100 // event.entries_total)
            counts = f"{event.entries_done}/{event.entries_total}"
        else:
            pct, counts = 0, ""
        head = f"{label:<11} {pct:3d}%"
        if counts:
            head += f" {counts}"
        room = self._width() - len(head) - 1
        name = event.entry_name
        if name and room >= 8:
            if len(name) > room:
                name = "..." + name[-(room - 3) :]
            return f"{head} {name}"
        return head[: self._width()]

    def __call__(self, event: ProgressEvent) -> None:
        line = self.format(event)
        self._stream.write("\r" + line.ljust(self._last))
        self._stream.flush()
        self._last = len(line)

    def clear(self) -> None:
        if self._last:
            self._stream.write("\r" + " " * self._last + "\r")
            self._stream.flush()
            self._last = 0
