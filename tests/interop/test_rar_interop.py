"""RAR archives written by the real ``rar`` program (local runs only), read through ``unrar``
and through 7-Zip. Zipwise never creates RAR files; these tests exist because the RAR
backend otherwise only ever sees a hand-built stored archive."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers.builders import write_stored_rar4
from helpers.trees import snapshot
from zipwise.backends import rar as rar_backend
from zipwise.errors import ExitCode, PasswordRequired, UnsupportedFormat, UsageError, WrongPassword
from zipwise.model import ExtractOptions
from zipwise.ops import extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive

from .checks import check_archive, expected_names
from .tools import PASSWORD, SEVENZIP, TREE_NAME, needs_crypto, needs_rar, needs_rar4, rar

_REAL_WHICH = shutil.which


@pytest.fixture(params=["unrar", "7z"])
def rar_tool(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Make Zipwise see exactly one extraction program, so each test covers both."""
    wanted = {"unrar"} if request.param == "unrar" else {"7z", "7zz"}
    if not any(_REAL_WHICH(name) for name in wanted):
        pytest.skip(f"{request.param} not found on PATH")

    def which(name: str, *args: object, **kwargs: object) -> str | None:
        return _REAL_WHICH(name) if name in wanted else None

    monkeypatch.setattr(rar_backend, "shutil", SimpleNamespace(which=which))
    yield request.param


def make_rar(tree: Path, work: Path, *args: str, name: str = "x.rar") -> Path:
    archive = work / name
    rar("a", *args, str(archive), TREE_NAME, cwd=tree.parent)
    return archive


def sevenzip_reads(archive: Path, password: str | None) -> bool:
    """Whether the installed 7-Zip itself can decode ``archive`` (older releases cannot read
    what RAR 7 writes)."""
    assert SEVENZIP is not None
    done = subprocess.run(
        [SEVENZIP, "t", "-bd", f"-p{password or ''}", str(archive)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=120,
    )
    return done.returncode == 0


def check_rar(
    archive: Path, tree: Path, work: Path, rar_tool: str, *, password: str | None = None
) -> None:
    """With unrar the archive must round-trip. With 7-Zip it must too, unless 7-Zip itself
    cannot decode it: then Zipwise has to say so instead of calling the archive damaged."""
    if rar_tool == "7z" and not sevenzip_reads(archive, password):
        with pytest.raises((UnsupportedFormat, WrongPassword)) as caught:
            extract_archive(archive, work / "unsupported", ExtractOptions(password=password))
        assert "7z" in caught.value.message
        assert caught.value.exit_code in (ExitCode.UNSUPPORTED, ExitCode.PASSWORD)
        issues = verify_archive(archive, password=password).issues if password is None else []
        assert all(i.code == "unsupported-format" for i in issues)
        return
    check_archive(archive, tree, work, password=password)


RAR_FLAVOURS = [
    pytest.param(["-ma5"], id="rar5"),
    pytest.param(["-ma5", "-s"], id="rar5-solid"),
    pytest.param(["-ma5", "-m5"], id="rar5-best"),
    pytest.param(["-ma5", "-m0"], id="rar5-store"),
    pytest.param(["-ma4"], id="rar4", marks=needs_rar4),
    pytest.param(["-ma4", "-s"], id="rar4-solid", marks=needs_rar4),
]

VERSIONS = [pytest.param("-ma5", id="rar5"), pytest.param("-ma4", id="rar4", marks=needs_rar4)]


@needs_rar
@pytest.mark.parametrize("args", RAR_FLAVOURS)
def test_compressed_rar(tree: Path, work: Path, rar_tool: str, args: list[str]) -> None:
    check_rar(make_rar(tree, work, *args), tree, work, rar_tool)


@needs_rar
@pytest.mark.parametrize("version", VERSIONS)
def test_rar_password(tree: Path, work: Path, rar_tool: str, version: str) -> None:
    archive = make_rar(tree, work, version, f"-p{PASSWORD}")
    assert all(e.encrypted for e in list_archive(archive) if e.size)
    check_rar(archive, tree, work, rar_tool, password=PASSWORD)
    with pytest.raises(PasswordRequired):
        extract_archive(archive, work / "nopw")
    with pytest.raises(WrongPassword):
        extract_archive(archive, work / "badpw", ExtractOptions(password="wrong"))


@needs_rar
@needs_crypto
@pytest.mark.parametrize("version", VERSIONS)
def test_rar_header_encrypted(tree: Path, work: Path, rar_tool: str, version: str) -> None:
    archive = make_rar(tree, work, version, f"-hp{PASSWORD}")
    with pytest.raises(PasswordRequired):
        list_archive(archive)
    check_rar(archive, tree, work, rar_tool, password=PASSWORD)


@needs_rar
def test_rar5_multi_volume(tree: Path, work: Path, rar_tool: str) -> None:
    make_rar(tree, work, "-ma5", "-v100k", name="x.rar")
    volumes = sorted(work.glob("x.part*.rar"))
    assert len(volumes) >= 3
    check_rar(volumes[0], tree, work, rar_tool)


@needs_rar
@needs_rar4
def test_rar4_multi_volume(tree: Path, work: Path, rar_tool: str) -> None:
    make_rar(tree, work, "-ma4", "-v100k", name="x.rar")
    assert (work / "x.r00").exists()
    check_rar(work / "x.rar", tree, work, rar_tool)


@needs_rar
def test_rar_later_volume_is_refused_with_advice(tree: Path, work: Path, rar_tool: str) -> None:
    make_rar(tree, work, "-ma5", "-v100k", name="x.rar")
    volumes = sorted(work.glob("x.part*.rar"))
    with pytest.raises(UsageError) as caught:
        list_archive(volumes[1])
    assert "first volume" in (caught.value.hint or "")


@needs_rar
def test_rar_unicode_names_and_empty_dirs(tree: Path, work: Path, rar_tool: str) -> None:
    archive = make_rar(tree, work, "-m0")  # stored: every tool can extract it
    names = {e.name for e in list_archive(archive)}
    assert names == expected_names(tree)
    assert f"{TREE_NAME}/unicode/ملف عربي.txt" in names
    assert f"{TREE_NAME}/empty" in names
    assert extract_archive(archive, work / "out").ok
    assert (work / "out" / TREE_NAME / "empty").is_dir()
    assert snapshot(tree).keys() == snapshot(work / "out" / TREE_NAME).keys()


FILES: dict[str, bytes | None] = {"a.txt": b"hello", "d/": None, "d/b.txt": b"world!!"}


def test_hand_built_stored_rar4_extracts(work: Path, rar_tool: str) -> None:
    archive = write_stored_rar4(work / "stored.rar", FILES)
    assert verify_archive(archive).ok
    out = work / "out"
    assert extract_archive(archive, out).ok
    assert (out / "a.txt").read_bytes() == b"hello"
    assert (out / "d" / "b.txt").read_bytes() == b"world!!"
