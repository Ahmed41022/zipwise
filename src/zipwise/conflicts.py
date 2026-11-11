"""What to do when an entry's target already exists."""

from __future__ import annotations

import os
from pathlib import Path

from .fsutil import long_path
from .jobs import JobContext
from .model import ConflictAction
from .naming import unique_name

__all__ = ["ConflictAction", "ConflictResolver", "unique_path"]


def unique_path(target: Path) -> Path:
    """``dir/a.txt`` -> ``dir/a (2).txt`` (first name that is free)."""
    free = unique_name(target.name, lambda n: os.path.lexists(long_path(target.with_name(n))))
    return target.with_name(free)


class ConflictResolver:
    """Resolves conflicts by policy; ASK goes to the caller and can be remembered."""

    def __init__(self, action: ConflictAction, ctx: JobContext) -> None:
        self.action = action
        self.ctx = ctx
        self.remembered: ConflictAction | None = None

    def resolve(self, target: Path) -> ConflictAction:
        """Return SKIP, RENAME or OVERWRITE for an existing ``target``."""
        if self.action is not ConflictAction.ASK:
            return self.action
        if self.remembered is not None:
            return self.remembered
        callback = self.ctx.resolve_conflict
        if callback is None:
            return ConflictAction.SKIP  # nobody to ask: never overwrite silently
        self.ctx.raise_if_cancelled()
        choice, remember = callback(target)
        if choice is ConflictAction.ASK:
            choice = ConflictAction.SKIP
        if remember:
            self.remembered = choice
        return choice
