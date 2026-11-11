from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from zipwise.fsutil import can_create_symlinks
from zipwise.jobs import JobContext


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``needs_symlink`` tests when the OS user may not create symlinks."""
    marked = [i for i in items if "needs_symlink" in i.keywords]
    if not marked:
        return
    with tempfile.TemporaryDirectory() as tmp:
        if can_create_symlinks(Path(tmp)):
            return
    skip = pytest.mark.skip(reason="the OS user may not create symlinks")
    for item in marked:
        item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_system_locale_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name detection must not depend on the machine running the tests; tests that need a
    locale hint patch ``zipwise.encoding.system_locale_hint`` themselves."""
    monkeypatch.setattr("zipwise.encoding.system_locale_hint", lambda: None)


@pytest.fixture
def ctx() -> JobContext:
    return JobContext()
