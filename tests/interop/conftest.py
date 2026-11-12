from __future__ import annotations

from pathlib import Path

import pytest

from helpers.trees import make_tree

from .tools import INTEROP_SPEC, MISSING_TOOL_REASONS, SINGLE_NAME, TREE_NAME


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests whose real program (7-Zip, rar, unrar) is not installed."""
    for item in items:
        for name, (missing, reason) in MISSING_TOOL_REASONS.items():
            if missing and name in item.keywords:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One shared source tree (read-only for the tests): Unicode names, empty folders,
    nested folders, a file over 1 MiB and an executable-like file."""
    base = tmp_path_factory.mktemp("interop-src")
    return make_tree(base / TREE_NAME, INTEROP_SPEC)


@pytest.fixture(scope="session")
def single_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("interop-single") / SINGLE_NAME
    path.write_bytes(b"one file, repeated text " * 5000)
    return path


@pytest.fixture
def work(tmp_path: Path) -> Path:
    out = tmp_path / "work"
    out.mkdir()
    return out
