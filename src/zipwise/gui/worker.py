"""Runs one archive job on a ``QThread`` and relays progress and questions to the GUI thread."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..errors import Cancelled
from ..jobs import CancelToken, JobContext
from ..model import ConflictAction

Job = Callable[[JobContext], object]
ConflictHandler = Callable[[Path], tuple[ConflictAction, bool]]
PasswordHandler = Callable[[], str | None]

_POLL_SECONDS = 0.05


class _Question:
    """A question asked from the worker thread; the worker blocks until it is answered."""

    def __init__(self, kind: str, payload: object = None) -> None:
        self.kind = kind
        self.payload = payload
        self.answer: Any = None
        self.done = threading.Event()


class _Task(QObject):
    """Lives in the worker thread."""

    progress = Signal(object)
    ask = Signal(object)
    finished = Signal(object)
    failed = Signal(object)
    cancelled = Signal()

    def __init__(self, job: Job, token: CancelToken) -> None:
        super().__init__()
        self._job = job
        self._token = token

    def _question(self, kind: str, payload: object = None) -> Any:
        question = _Question(kind, payload)
        self.ask.emit(question)
        while not question.done.wait(_POLL_SECONDS):
            self._token.raise_if_cancelled()
        self._token.raise_if_cancelled()
        return question.answer

    def _conflict(self, path: Path) -> tuple[ConflictAction, bool]:
        answer: tuple[ConflictAction, bool] = self._question("conflict", path)
        return answer

    def _password(self) -> str | None:
        answer: str | None = self._question("password")
        return answer

    @Slot()
    def run(self) -> None:
        ctx = JobContext(
            cancel=self._token,
            on_progress=self.progress.emit,
            resolve_conflict=self._conflict,
            ask_password=self._password,
        )
        try:
            result = self._job(ctx)
        except Cancelled:
            self.cancelled.emit()
        except BaseException as exc:  # noqa: BLE001 - forwarded to the GUI thread
            self.failed.emit(exc)
        else:
            if self._token.cancelled:
                self.cancelled.emit()
            else:
                self.finished.emit(result)


class JobWorker(QObject):
    """Owns the thread and the cancel token of one job.

    Exactly one of ``finished``, ``failed`` or ``cancelled`` is emitted, in the GUI thread.
    ``conflict_handler`` and ``password_handler`` are called in the GUI thread while the job
    waits; without them conflicts are skipped and no password is supplied.
    """

    progress = Signal(object)
    finished = Signal(object)
    failed = Signal(object)
    cancelled = Signal()

    def __init__(
        self,
        job: Job,
        *,
        conflict_handler: ConflictHandler | None = None,
        password_handler: PasswordHandler | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.token = CancelToken()
        self.conflict_handler = conflict_handler
        self.password_handler = password_handler
        self._thread = QThread(self)
        self._task = _Task(job, self.token)
        self._task.moveToThread(self._thread)
        self._thread.started.connect(self._task.run)
        self._task.progress.connect(self.progress)
        self._task.ask.connect(self._on_ask)
        self._task.finished.connect(self._on_finished)
        self._task.failed.connect(self._on_failed)
        self._task.cancelled.connect(self._on_cancelled)
        self._started = False

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def cancel(self) -> None:
        self.token.cancel()

    def is_running(self) -> bool:
        return self._thread.isRunning()

    def wait(self, timeout_ms: int = 10000) -> bool:
        """Block until the thread has stopped; True when it did within the timeout."""
        if not self._started:
            return True
        return self._thread.wait(timeout_ms)

    def shutdown(self, timeout_ms: int = 10000) -> bool:
        """Cancel the job and wait for the thread (used when the window closes)."""
        self.cancel()
        self._thread.quit()
        return self.wait(timeout_ms)

    # -- slots in the GUI thread ----------------------------------------------------------

    @Slot(object)
    def _on_ask(self, question: _Question) -> None:
        try:
            if question.kind == "conflict":
                handler = self.conflict_handler
                question.answer = (
                    handler(question.payload)  # type: ignore[arg-type]
                    if handler
                    else (ConflictAction.SKIP, False)
                )
            else:
                question.answer = self.password_handler() if self.password_handler else None
        finally:
            question.done.set()

    def _stop_thread(self) -> None:
        self._thread.quit()

    @Slot(object)
    def _on_finished(self, result: object) -> None:
        self._stop_thread()
        self.finished.emit(result)

    @Slot(object)
    def _on_failed(self, exc: object) -> None:
        self._stop_thread()
        self.failed.emit(exc)

    @Slot()
    def _on_cancelled(self) -> None:
        self._stop_thread()
        self.cancelled.emit()
