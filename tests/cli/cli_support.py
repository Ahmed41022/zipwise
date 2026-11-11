"""Shared helpers for the CLI tests."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Run:
    code: int
    out: str
    err: str

    def json(self) -> dict:
        return json.loads(self.out)


RunFn = Callable[..., Run]


class FakeTTY(io.StringIO):
    """A text stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True
