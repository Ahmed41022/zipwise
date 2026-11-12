"""Archives written by the real 7-Zip, read by Zipwise."""

from __future__ import annotations

from pathlib import Path

import pytest

from zipwise.errors import ExitCode, PasswordRequired, UnsupportedFormat, WrongPassword
from zipwise.model import ExtractOptions
from zipwise.ops import extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive

from .checks import check_archive, expected_names
from .tools import PASSWORD, TREE_NAME, needs_7zip, sevenzip

pytestmark = needs_7zip

SEVENZ_METHODS = [
    pytest.param([], id="lzma2-solid-default"),
    pytest.param(["-ms=off"], id="non-solid"),
    pytest.param(["-m0=LZMA"], id="lzma"),
    pytest.param(["-m0=PPMd"], id="ppmd"),
    pytest.param(["-m0=BZip2"], id="bzip2"),
    pytest.param(["-m0=Deflate"], id="deflate"),
    pytest.param(["-m0=Copy"], id="copy"),
    pytest.param(["-m0=Delta:4", "-m1=LZMA2"], id="delta-lzma2"),
    pytest.param(["-mmt=on"], id="multithreaded"),
    pytest.param(["-mmt=on", "-ms=200k", "-mx=1"], id="multithreaded-several-folders"),
]


def make_7z(tree: Path, work: Path, *args: str, name: str = "x.7z") -> Path:
    archive = work / name
    sevenzip("a", "-t7z", *args, str(archive), TREE_NAME, cwd=tree.parent)
    return archive


@pytest.mark.parametrize("args", SEVENZ_METHODS)
def test_7z_methods(tree: Path, work: Path, args: list[str]) -> None:
    check_archive(make_7z(tree, work, *args), tree, work)


@pytest.mark.parametrize("filter_args", [["-mf=BCJ"], ["-mf=Delta:4"], ["-mf=PPC"]])
def test_7z_filters(tree: Path, work: Path, filter_args: list[str]) -> None:
    check_archive(make_7z(tree, work, *filter_args), tree, work)


@pytest.mark.parametrize(
    ("args", "method"),
    [
        pytest.param(["-mf=BCJ2", "-m0=LZMA2"], "BCJ2", id="bcj2"),
        pytest.param(["-mf=ARM64"], "ARM64", id="arm64"),
    ],
)
def test_7z_method_py7zr_lacks_fails_clearly(
    tree: Path, work: Path, args: list[str], method: str
) -> None:
    archive = make_7z(tree, work, *args)
    assert method in sevenzip("l", "-slt", str(archive), cwd=work)
    assert {e.name for e in list_archive(archive)} == expected_names(tree)  # listing still works
    issues = verify_archive(archive).issues
    assert [i.code for i in issues] == ["unsupported-format"]
    assert f"uses {method} compression, which Zipwise cannot read yet" in issues[0].message
    with pytest.raises(UnsupportedFormat) as caught:
        extract_archive(archive, work / "out")
    assert caught.value.exit_code == ExitCode.UNSUPPORTED
    assert f"uses {method} compression" in caught.value.message


def test_7z_data_encrypted(tree: Path, work: Path) -> None:
    archive = make_7z(tree, work, f"-p{PASSWORD}")
    assert all(e.encrypted for e in list_archive(archive) if e.size)
    check_archive(archive, tree, work, password=PASSWORD)
    with pytest.raises(PasswordRequired):
        extract_archive(archive, work / "nopw")
    with pytest.raises(WrongPassword):
        extract_archive(archive, work / "badpw", ExtractOptions(password="wrong"))


def test_7z_header_encrypted(tree: Path, work: Path) -> None:
    archive = make_7z(tree, work, f"-p{PASSWORD}", "-mhe=on")
    with pytest.raises(PasswordRequired):
        list_archive(archive)
    check_archive(archive, tree, work, password=PASSWORD)


def test_7z_header_encrypted_wrong_password(tree: Path, work: Path) -> None:
    archive = make_7z(tree, work, f"-p{PASSWORD}", "-mhe=on")
    with pytest.raises(WrongPassword):
        list_archive(archive, password="not it")


def test_7z_multi_volume_from_first_part(tree: Path, work: Path) -> None:
    make_7z(tree, work, "-v100k")
    first = work / "x.7z.001"
    assert (work / "x.7z.003").exists()
    check_archive(first, tree, work)


def test_7z_multi_volume_opened_from_a_later_part(tree: Path, work: Path) -> None:
    make_7z(tree, work, "-v100k")
    assert verify_archive(work / "x.7z.002").ok
    assert {e.name for e in list_archive(work / "x.7z.002")} >= {TREE_NAME}


def test_7z_empty_archive(tree: Path, work: Path) -> None:
    archive = make_7z(tree, work)
    sevenzip("d", str(archive), TREE_NAME, cwd=work)
    assert list_archive(archive) == []
    assert verify_archive(archive).ok
    result = extract_archive(archive, work / "out")
    assert result.ok
    assert result.entries_done == 0
