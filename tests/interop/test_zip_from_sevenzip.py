"""ZIP, tar and single-file archives written by the real 7-Zip, read by Zipwise."""

from __future__ import annotations

from pathlib import Path

import pytest

from zipwise.errors import ExitCode, PasswordRequired, UnsupportedFormat, WrongPassword
from zipwise.model import ExtractOptions
from zipwise.ops import extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive

from .checks import check_archive, expected_names
from .tools import ASCII_PASSWORD, TREE_NAME, needs_7zip, sevenzip

pytestmark = needs_7zip


def make_zip(tree: Path, work: Path, *args: str, name: str = "x.zip") -> Path:
    archive = work / name
    sevenzip("a", "-tzip", *args, str(archive), TREE_NAME, cwd=tree.parent)
    return archive


@pytest.mark.parametrize(
    "method",
    [
        pytest.param("Deflate", id="deflate"),
        pytest.param("BZip2", id="bzip2"),
        pytest.param("LZMA", id="lzma"),
        pytest.param("Copy", id="store"),
    ],
)
def test_zip_methods(tree: Path, work: Path, method: str) -> None:
    check_archive(make_zip(tree, work, f"-mm={method}"), tree, work)


def test_zip_deflate64_fails_clearly(tree: Path, work: Path) -> None:
    archive = make_zip(tree, work, "-mm=Deflate64")
    assert {e.name for e in list_archive(archive)} == expected_names(tree)
    issues = verify_archive(archive).issues
    assert issues
    assert {i.code for i in issues} == {"unsupported-format"}
    assert all(
        "uses Deflate64 compression, which Zipwise cannot read yet" in i.message for i in issues
    )
    with pytest.raises(UnsupportedFormat) as caught:
        extract_archive(archive, work / "out")
    assert caught.value.exit_code == ExitCode.UNSUPPORTED
    assert "Deflate64" in caught.value.message


@pytest.mark.parametrize("scheme", ["AES256", "ZipCrypto"])
def test_zip_encrypted(tree: Path, work: Path, scheme: str) -> None:
    archive = make_zip(tree, work, f"-mem={scheme}", f"-p{ASCII_PASSWORD}")
    check_archive(archive, tree, work, password=ASCII_PASSWORD)
    with pytest.raises(PasswordRequired):
        extract_archive(archive, work / "nopw")
    with pytest.raises(WrongPassword):
        extract_archive(archive, work / "badpw", ExtractOptions(password="wrong"))


def test_zip_unicode_names_without_a_hint(tree: Path, work: Path) -> None:
    names = {e.name for e in list_archive(make_zip(tree, work))}
    assert f"{TREE_NAME}/unicode/ملف عربي.txt" in names
    assert f"{TREE_NAME}/unicode/文件/数据.txt" in names


def test_zip_multi_volume(tree: Path, work: Path) -> None:
    make_zip(tree, work, "-v100k")
    assert (work / "x.zip.003").exists()
    check_archive(work / "x.zip.001", tree, work)


@pytest.mark.parametrize(
    ("codec", "suffix"), [("gzip", "gz"), ("xz", "xz"), ("bzip2", "bz2")], ids=["gz", "xz", "bz2"]
)
def test_tar_variants(tree: Path, work: Path, codec: str, suffix: str) -> None:
    tar = work / "x.tar"
    sevenzip("a", "-ttar", str(tar), TREE_NAME, cwd=tree.parent)
    check_archive(tar, tree, work)
    compressed = work / f"x.tar.{suffix}"
    sevenzip("a", f"-t{codec}", str(compressed), str(tar), cwd=work)
    (work / "extracted").rename(work / "extracted-plain")
    check_archive(compressed, tree, work)


@pytest.mark.parametrize(
    ("codec", "suffix"), [("gzip", "gz"), ("xz", "xz"), ("bzip2", "bz2")], ids=["gz", "xz", "bz2"]
)
def test_single_file(single_file: Path, work: Path, codec: str, suffix: str) -> None:
    archive = work / f"{single_file.name}.{suffix}"
    sevenzip("a", f"-t{codec}", str(archive), single_file.name, cwd=single_file.parent)
    entries = list_archive(archive)
    assert [e.name for e in entries] == [single_file.name]
    assert verify_archive(archive).ok
    assert extract_archive(archive, work / "out").ok
    assert (work / "out" / single_file.name).read_bytes() == single_file.read_bytes()
