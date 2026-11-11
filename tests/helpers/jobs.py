"""Job-context helpers for tests."""

from __future__ import annotations

from collections.abc import Callable

from zipwise.jobs import CancelToken, JobContext, ProgressEvent


def stepping_context(
    cancel_on: str | None = None,
    observe: Callable[[ProgressEvent], None] | None = None,
) -> tuple[JobContext, CancelToken, list[ProgressEvent]]:
    """A context whose progress events are never throttled.

    It cancels as soon as an event names the entry ``cancel_on``.
    """
    token = CancelToken()
    ticks = iter(range(10**7))
    events: list[ProgressEvent] = []

    def on_progress(event: ProgressEvent) -> None:
        events.append(event)
        if observe is not None:
            observe(event)
        if cancel_on is not None and event.entry_name == cancel_on:
            token.cancel()

    ctx = JobContext(cancel=token, on_progress=on_progress, clock=lambda: float(next(ticks)))
    return ctx, token, events
