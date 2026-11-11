"""Extraction journal: lets an interrupted extraction continue where it stopped.

The journal ``.<archive-name>.zipwise-resume.json`` lives in the destination folder. It holds
a fingerprint of the archive (size, mtime, hash of its first and last 64 KiB) and the entries
that were finished. Files are written to ``name.zwpart`` and renamed when complete, so a
journal entry always stands for a complete file.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .errors import CorruptArchive
from .fsutil import long_path, remove_quietly
from .model import Issue
from .split import MultiPartFile, find_parts

JOURNAL_VERSION = 1
SAMPLE_SIZE = 64 * 1024
FLUSH_EVERY_ENTRIES = 25
FLUSH_EVERY_SECONDS = 2.0


def journal_path(dest: Path, archive_name: str) -> Path:
    return dest / f".{archive_name}.zipwise-resume.json"


@dataclass(frozen=True)
class Fingerprint:
    size: int
    mtime_ns: int
    head: str
    tail: str

    def to_dict(self) -> dict[str, object]:
        return {"size": self.size, "mtime_ns": self.mtime_ns, "head": self.head, "tail": self.tail}

    @classmethod
    def from_dict(cls, data: object) -> Fingerprint | None:
        try:
            assert isinstance(data, dict)
            return cls(
                int(data["size"]), int(data["mtime_ns"]), str(data["head"]), str(data["tail"])
            )
        except (AssertionError, KeyError, TypeError, ValueError):
            return None


def _hash_ends(stream: BinaryIO, size: int) -> tuple[str, str]:
    stream.seek(0)
    head = hashlib.sha256(stream.read(SAMPLE_SIZE)).hexdigest()
    stream.seek(max(0, size - SAMPLE_SIZE))
    tail = hashlib.sha256(stream.read(SAMPLE_SIZE)).hexdigest()
    return head, tail


def fingerprint_archive(archive: Path, *, multipart: bool = False) -> Fingerprint:
    """Fingerprint a single archive file, or a whole set of numbered parts."""
    if multipart:
        parts = find_parts(archive).parts
        mtime = max(os.stat(long_path(p)).st_mtime_ns for p in parts)
        with MultiPartFile(parts) as joined:
            head, tail = _hash_ends(joined, joined.size)  # type: ignore[arg-type]
            return Fingerprint(joined.size, mtime, head, tail)
    st = os.stat(long_path(archive))
    with long_path(archive).open("rb") as fh:
        head, tail = _hash_ends(fh, st.st_size)
    return Fingerprint(st.st_size, st.st_mtime_ns, head, tail)


class ResumeJournal:
    """In-memory view of the journal with incremental, atomic persistence."""

    def __init__(
        self,
        path: Path,
        fingerprint: Fingerprint,
        completed: dict[int, tuple[str, int]] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        flush_every: int = FLUSH_EVERY_ENTRIES,
        flush_seconds: float = FLUSH_EVERY_SECONDS,
    ) -> None:
        self.path = path
        self.fingerprint = fingerprint
        self.completed: dict[int, tuple[str, int]] = dict(completed or {})
        self._clock = clock
        self._flush_every = flush_every
        self._flush_seconds = flush_seconds
        self._unsaved = 0
        self._last_flush = clock()

    @classmethod
    def start(
        cls,
        dest: Path,
        archive: Path,
        *,
        multipart: bool = False,
        resume: bool = False,
        clock: Callable[[], float] = time.monotonic,
        flush_every: int = FLUSH_EVERY_ENTRIES,
        flush_seconds: float = FLUSH_EVERY_SECONDS,
    ) -> tuple[ResumeJournal, list[Issue]]:
        """Open the journal for an extraction into ``dest``.

        With ``resume`` an existing journal for the same archive is loaded; a journal for a
        changed archive (or an unreadable one) is discarded with an info issue. Without
        ``resume`` an existing journal is ignored and will be replaced.
        """
        try:
            fingerprint = fingerprint_archive(archive, multipart=multipart)
        except (OSError, CorruptArchive):
            fingerprint = Fingerprint(0, 0, "", "")
        name = find_parts(archive).first.name if multipart else archive.name
        path = journal_path(dest, name)
        journal = cls(
            path,
            fingerprint,
            clock=clock,
            flush_every=flush_every,
            flush_seconds=flush_seconds,
        )
        issues: list[Issue] = []
        if resume and os.path.lexists(long_path(path)):
            loaded, reason = _load(path, fingerprint)
            if loaded is None:
                issues.append(Issue("info", "resume-discarded", None, f"{reason}; starting over"))
            else:
                journal.completed = loaded
        return journal, issues

    def is_done(self, index: int, name: str, size: int, target: Path) -> bool:
        """True when entry ``index`` was finished earlier and its file is still intact."""
        recorded = self.completed.get(index)
        if recorded is None or recorded[0] != name or recorded[1] != size:
            return False
        try:
            st = os.lstat(long_path(target))
        except OSError:
            return False
        return st.st_size == size and not _is_special(st.st_mode)

    def mark_done(self, index: int, name: str, size: int) -> None:
        self.completed[index] = (name, size)
        self._unsaved += 1
        if (
            self._unsaved >= self._flush_every
            or self._clock() - self._last_flush >= self._flush_seconds
        ):
            self.flush()

    def flush(self) -> None:
        """Write the journal now (atomically). Does nothing when no entry is finished."""
        if self._unsaved == 0 or not self.completed:
            return
        document = {
            "version": JOURNAL_VERSION,
            "archive": self.fingerprint.to_dict(),
            "entries": [
                {"index": index, "name": name, "size": size}
                for index, (name, size) in sorted(self.completed.items())
            ],
        }
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            long_path(temp).write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            os.replace(long_path(temp), long_path(self.path))
        except OSError:
            remove_quietly(temp)  # a journal that cannot be written only costs the resume
            return
        self._unsaved = 0
        self._last_flush = self._clock()

    def remove(self) -> None:
        """Delete the journal after a complete extraction."""
        remove_quietly(self.path)
        remove_quietly(self.path.with_name(self.path.name + ".tmp"))


def _is_special(mode: int) -> bool:
    return stat.S_ISLNK(mode) or stat.S_ISDIR(mode)


def _load(path: Path, fingerprint: Fingerprint) -> tuple[dict[int, tuple[str, int]] | None, str]:
    try:
        data = json.loads(long_path(path).read_text(encoding="utf-8"))
        if data["version"] != JOURNAL_VERSION:
            return None, "the journal was written by another version"
        stored = Fingerprint.from_dict(data["archive"])
        entries = {int(e["index"]): (str(e["name"]), int(e["size"])) for e in data["entries"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None, "the resume journal is unreadable"
    if stored != fingerprint:
        return None, "the archive changed since the interrupted extraction"
    return entries, ""
