"""Collects what the GUI would have shown to the user."""

from __future__ import annotations


class Recorder:
    def __init__(self) -> None:
        self.errors: list[BaseException] = []
        self.messages: list[tuple[str, str]] = []
