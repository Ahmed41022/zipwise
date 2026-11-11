"""Fixtures for the command-line tests: in-process runner, small trees and archives."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from cli_support import FakeTTY, Run, RunFn

from helpers.trees import make_tree
from zipwise.cli.main import main
from zipwise.model import CreateOptions, FormatId
from zipwise.ops import create_archive


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Settings, debug flag and working directory never leak in from the real machine."""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("APPDATA", str(config))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("ZIPWISE_DEBUG", raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> RunFn:
    def _run(*args: object) -> Run:
        capsys.readouterr()
        code = main([str(a) for a in args])
        captured = capsys.readouterr()
        return Run(code, captured.out, captured.err)

    return _run


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Make stdin a terminal that answers ``input()`` with the given lines.

    stderr stays captured by pytest, so the "both are terminals" check is switched on too.
    """

    def install(*answers: str) -> None:
        monkeypatch.setattr(sys, "stdin", FakeTTY())
        monkeypatch.setattr("zipwise.cli.main.interactive_terminal", lambda stdin, stderr: True)
        replies = iter(answers)

        def fake_input(prompt: str = "") -> str:
            try:
                return next(replies)
            except StopIteration:
                raise EOFError from None

        monkeypatch.setattr("builtins.input", fake_input)

    return install


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """``data/`` with a few small files and a sub-folder."""
    return make_tree(
        tmp_path / "data",
        {
            "a.txt": b"alpha\n" * 20,
            "sub/b.txt": b"beta\n" * 30,
            "sub/c.bin": bytes(range(256)),
        },
    )


@pytest.fixture
def archive(tmp_path: Path, data: Path) -> Path:
    """A ZIP with one top-level folder (``data``)."""
    out = tmp_path / "one.zip"
    create_archive([data], out, CreateOptions(format=FormatId.ZIP))
    return out


@pytest.fixture
def wide_archive(tmp_path: Path, data: Path) -> Path:
    """A ZIP with two top-level items."""
    extra = tmp_path / "readme.txt"
    extra.write_text("read me")
    out = tmp_path / "wide.zip"
    create_archive([data, extra], out, CreateOptions(format=FormatId.ZIP))
    return out
