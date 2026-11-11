"""Cancellation and progress plumbing shared by every long-running operation."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import Cancelled
from .model import ConflictAction

Phase = Literal["scan", "compress", "verify", "split", "extract", "test", "join"]


class CancelToken:
    """Thread-safe cancellation flag."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled()


@dataclass(frozen=True)
class ProgressEvent:
    phase: Phase
    entry_name: str
    bytes_done: int
    bytes_total: int
    entries_done: int
    entries_total: int


ProgressCallback = Callable[[ProgressEvent], None]
ConflictCallback = Callable[[Path], tuple[ConflictAction, bool]]
PasswordCallback = Callable[[], str | None]

MIN_REPORT_INTERVAL = 0.05  # about 20 events per second


class JobContext:
    """Carries the cancel token, the progress sink and the user-interaction callbacks.

    The running counters live here so that byte-copying helpers only need to call
    :meth:`advance`.
    """

    def __init__(
        self,
        cancel: CancelToken | None = None,
        on_progress: ProgressCallback | None = None,
        resolve_conflict: ConflictCallback | None = None,
        ask_password: PasswordCallback | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cancel = cancel or CancelToken()
        self.on_progress = on_progress
        self.resolve_conflict = resolve_conflict
        self.ask_password = ask_password
        self._clock = clock
        self._last_emit = float("-inf")
        self.phase: Phase = "scan"
        self.entry_name = ""
        self.bytes_done = 0
        self.bytes_total = 0
        self.entries_done = 0
        self.entries_total = 0

    def raise_if_cancelled(self) -> None:
        self.cancel.raise_if_cancelled()

    def report(
        self,
        phase: Phase,
        entry_name: str = "",
        bytes_done: int = 0,
        bytes_total: int = 0,
        entries_done: int = 0,
        entries_total: int = 0,
        *,
        force: bool = False,
    ) -> None:
        """Emit a progress event, throttled to about 20 per second unless ``force``."""
        if self.on_progress is None:
            return
        now = self._clock()
        if not force and now - self._last_emit < MIN_REPORT_INTERVAL:
            return
        self._last_emit = now
        self.on_progress(
            ProgressEvent(phase, entry_name, bytes_done, bytes_total, entries_done, entries_total)
        )

    def begin(self, phase: Phase, *, bytes_total: int = 0, entries_total: int = 0) -> None:
        self.phase = phase
        self.entry_name = ""
        self.bytes_done = 0
        self.bytes_total = bytes_total
        self.entries_done = 0
        self.entries_total = entries_total
        self._emit(force=True)

    def set_entry(self, name: str) -> None:
        self.entry_name = name
        self._emit()

    def advance(self, nbytes: int) -> None:
        self.bytes_done += nbytes
        self._emit()

    def entry_done(self) -> None:
        self.entries_done += 1
        self._emit()

    def finish(self) -> None:
        self._emit(force=True)

    def _emit(self, *, force: bool = False) -> None:
        self.report(
            self.phase,
            self.entry_name,
            self.bytes_done,
            self.bytes_total,
            self.entries_done,
            self.entries_total,
            force=force,
        )
