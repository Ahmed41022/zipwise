"""Archives written by Zipwise, checked and unpacked by the real 7-Zip."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers.roundtrip import compare_snapshots
from helpers.trees import snapshot
from zipwise.model import CreateOptions, FormatId
from zipwise.ops import create_archive

from .tools import ASCII_PASSWORD, PASSWORD, TREE_NAME, needs_7zip, sevenzip

pytestmark = needs_7zip

F = FormatId

ARCHIVES = [
    pytest.param(F.ZIP, {}, None, id="zip"),
    pytest.param(F.ZIP, {"smart_store": False}, None, id="zip-no-smart-store"),
    pytest.param(F.ZIP, {"level": 0}, None, id="zip-level-0"),
    pytest.param(F.ZIP, {"password": ASCII_PASSWORD}, ASCII_PASSWORD, id="zip-aes256"),
    pytest.param(
        F.ZIP,
        {"password": PASSWORD},
        PASSWORD,
        id="zip-aes256-unicode-password",
        marks=pytest.mark.skipif(
            os.name == "nt",
            reason="7-Zip on Windows converts a non-ASCII ZIP password with a legacy code page, "
            "not UTF-8 as WinZip AES does",
        ),
    ),
    pytest.param(F.SEVENZ, {}, None, id="7z"),
    pytest.param(F.SEVENZ, {"level": "best"}, None, id="7z-best"),
    pytest.param(F.SEVENZ, {"password": PASSWORD}, PASSWORD, id="7z-password"),
    pytest.param(
        F.SEVENZ, {"password": PASSWORD, "encrypt_names": True}, PASSWORD, id="7z-encrypted-names"
    ),
    pytest.param(F.TAR, {}, None, id="tar"),
]

TAR_CODECS = [
    pytest.param(F.TAR_GZ, "gz", id="tar.gz"),
    pytest.param(F.TAR_BZ2, "bz2", id="tar.bz2"),
    pytest.param(F.TAR_XZ, "xz", id="tar.xz"),
]


def seven_test(archive: Path, work: Path, password: str | None = None) -> str:
    pw = [f"-p{password}"] if password else []
    return sevenzip("t", *pw, str(archive), cwd=work)


def seven_extract(archive: Path, out: Path, work: Path, password: str | None = None) -> None:
    pw = [f"-p{password}"] if password else []
    sevenzip("x", *pw, f"-o{out}", str(archive), cwd=work)


def assert_same_tree(tree: Path, extracted: Path) -> None:
    compare_snapshots(snapshot(tree), snapshot(extracted / TREE_NAME), modes=False)


@pytest.mark.parametrize(("fmt", "extra", "password"), ARCHIVES)
def test_7zip_reads_zipwise_archive(
    tree: Path, work: Path, fmt: FormatId, extra: dict[str, object], password: str | None
) -> None:
    archive = work / f"out.{fmt.value}"
    create_archive([tree], archive, CreateOptions(format=fmt, **extra))  # type: ignore[arg-type]
    seven_test(archive, work, password)
    seven_extract(archive, work / "out", work, password)
    assert_same_tree(tree, work / "out")


@pytest.mark.parametrize(("fmt", "suffix"), TAR_CODECS)
def test_7zip_reads_zipwise_compressed_tar(
    tree: Path, work: Path, fmt: FormatId, suffix: str
) -> None:
    archive = work / f"out.tar.{suffix}"
    create_archive([tree], archive, CreateOptions(format=fmt))
    seven_test(archive, work)
    seven_extract(archive, work / "step1", work)
    seven_extract(work / "step1" / "out.tar", work / "step2", work)
    assert_same_tree(tree, work / "step2")


@pytest.mark.parametrize(
    ("fmt", "suffix"), [(F.GZ, "gz"), (F.XZ, "xz"), (F.BZ2, "bz2")], ids=["gz", "xz", "bz2"]
)
def test_7zip_reads_zipwise_single_file(
    single_file: Path, work: Path, fmt: FormatId, suffix: str
) -> None:
    archive = work / f"{single_file.name}.{suffix}"
    create_archive([single_file], archive, CreateOptions(format=fmt))
    seven_test(archive, work)
    seven_extract(archive, work / "out", work)
    assert (work / "out" / single_file.name).read_bytes() == single_file.read_bytes()


@pytest.mark.parametrize(
    ("fmt", "password"),
    [(F.ZIP, None), (F.ZIP, ASCII_PASSWORD), (F.SEVENZ, None), (F.SEVENZ, PASSWORD), (F.TAR, None)],
    ids=["zip", "zip-aes", "7z", "7z-password", "tar"],
)
def test_7zip_reads_zipwise_split_set(
    tree: Path, work: Path, fmt: FormatId, password: str | None
) -> None:
    archive = work / f"set.{fmt.value}"
    report = create_archive(
        [tree], archive, CreateOptions(format=fmt, password=password, split_size=100_000)
    )
    first = work / f"set.{fmt.value}.001"
    assert first in report.output_paths
    assert (work / f"set.{fmt.value}.003").exists()
    seven_test(first, work, password)
    seven_extract(first, work / "out", work, password)
    assert_same_tree(tree, work / "out")
