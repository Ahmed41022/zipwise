"""7z, TAR and single-file readers read numbered parts directly, without a joined temp copy."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from helpers.names import extracted_files
from helpers.trees import SHAPES, make_tree, random_bytes, snapshot
from zipwise import registry
from zipwise.jobs import JobContext
from zipwise.model import CreateOptions, ExtractOptions, FormatId
from zipwise.ops import archive_info, create_archive, extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive
from zipwise.ops._input import open_input_reader
from zipwise.split import split_file

HAS_ZSTD = importlib.util.find_spec("zstandard") is not None
PART_SIZE = 700

CASES = [
    pytest.param(FormatId.SEVENZ, "x.7z", id="7z"),
    pytest.param(FormatId.TAR_GZ, "x.tar.gz", id="tar.gz"),
    pytest.param(
        FormatId.TAR_ZST,
        "x.tar.zst",
        id="tar.zst",
        marks=pytest.mark.skipif(not HAS_ZSTD, reason="zstandard is not installed"),
    ),
    pytest.param(FormatId.GZ, "x.txt.gz", id="txt.gz"),
]


@pytest.fixture(autouse=True)
def _no_joined_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("materialise_parts must not be used for this format")

    monkeypatch.setattr("zipwise.ops._input.materialise_parts", refuse)


def make_pair(tmp_path: Path, fmt: FormatId, name: str) -> tuple[Path, Path]:
    """The whole archive and the first part of the same archive split into small parts."""
    if fmt is FormatId.GZ:
        source = tmp_path / "src" / "x.txt"
        source.parent.mkdir()
        source.write_bytes(random_bytes(3, 2500) + b"tail" * 300)
    else:
        source = make_tree(tmp_path / "src" / "tree", SHAPES["mixed"])
    whole = tmp_path / "whole" / name
    create_archive([source], whole, CreateOptions(format=fmt, level="fast"))
    split_dir = tmp_path / "split"
    split_dir.mkdir()
    shutil.copy(whole, split_dir / name)
    parts = split_file(split_dir / name, PART_SIZE, keep_source=False)
    assert len(parts) >= 3
    return whole, parts[0]


@pytest.mark.parametrize(("fmt", "name"), CASES)
def test_reader_takes_the_parts_as_source(tmp_path: Path, fmt: FormatId, name: str) -> None:
    assert registry.reader_accepts_source(fmt)
    _, first = make_pair(tmp_path, fmt, name)
    with open_input_reader(first) as reader:
        assert reader.source is not None
        assert reader.path == first and reader.multipart
        reader.entries()
        reader.info()
        assert not reader.source.closed  # two passes, and nothing closed the shared source
        source = reader.source
    assert source.closed  # released together with the reader


@pytest.mark.parametrize(("fmt", "name"), CASES)
def test_list_and_info_match_the_whole_archive(tmp_path: Path, fmt: FormatId, name: str) -> None:
    whole, first = make_pair(tmp_path, fmt, name)
    assert list_archive(first) == list_archive(whole)
    split_info, whole_info = archive_info(first), archive_info(whole)
    assert split_info.multipart and not whole_info.multipart
    for field in ("format", "entries_count", "total_size", "compressed_size", "encrypted"):
        assert getattr(split_info, field) == getattr(whole_info, field), field


@pytest.mark.parametrize(("fmt", "name"), CASES)
def test_test_matches_the_whole_archive(tmp_path: Path, fmt: FormatId, name: str) -> None:
    whole, first = make_pair(tmp_path, fmt, name)
    split_report, whole_report = verify_archive(first), verify_archive(whole)
    assert split_report.ok and whole_report.ok
    assert split_report.bytes_done == whole_report.bytes_done > 0


@pytest.mark.parametrize(("fmt", "name"), CASES)
def test_extract_matches_the_whole_archive(tmp_path: Path, fmt: FormatId, name: str) -> None:
    whole, first = make_pair(tmp_path, fmt, name)
    assert extract_archive(whole, tmp_path / "a").ok
    assert extract_archive(first, tmp_path / "b").ok
    assert snapshot(tmp_path / "b") == snapshot(tmp_path / "a")


def test_partial_selection_from_tar_parts(tmp_path: Path) -> None:
    whole, first = make_pair(tmp_path, FormatId.TAR_GZ, "x.tar.gz")
    options = ExtractOptions(only=["tree/sub"])
    assert extract_archive(first, tmp_path / "b", options).ok
    assert extract_archive(whole, tmp_path / "a", options).ok
    files = extracted_files(tmp_path / "a")
    assert files and extracted_files(tmp_path / "b") == files
    for name in files:
        assert (tmp_path / "b" / name).read_bytes() == (tmp_path / "a" / name).read_bytes()


def test_tar_zst_parts_are_detected_as_tar(tmp_path: Path) -> None:
    pytest.importorskip("zstandard")
    _, first = make_pair(tmp_path, FormatId.TAR_ZST, "x.tar.zst")
    assert archive_info(first).format is FormatId.TAR_ZST


def test_encrypted_7z_parts_reopen_the_shared_source_for_the_password(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["simple"])
    whole = tmp_path / "whole" / "s.7z"
    create_archive([tree], whole, CreateOptions(format=FormatId.SEVENZ, password="pw"))
    (tmp_path / "split").mkdir()
    parts = split_file(whole, 200, keep_source=True, out_dir=tmp_path / "split")
    with open_input_reader(parts[0]) as reader:
        names = [e.name for e in reader.entries()]
        reader.password = "pw"  # py7zr re-opens the archive: the view starts at 0 again
        assert [e.name for e in reader.entries()] == names
        ctx = JobContext()
        assert reader.test(ctx) == []
        assert ctx.entries_done == len(names)
    report = extract_archive(parts[0], tmp_path / "d", ExtractOptions(password="pw"))
    assert report.ok and (tmp_path / "d" / "tree" / "a.txt").read_bytes() == b"hello\n"


def test_rar_keeps_the_temporary_joined_copy() -> None:
    pytest.importorskip("rarfile")
    # the external tool needs a real file, so the reader declares no ``source`` parameter
    assert registry.reader_accepts_source(FormatId.RAR) is False
