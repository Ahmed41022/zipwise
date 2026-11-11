"""Every writable format: create -> test -> extract -> compare, plus detection of each result."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers.roundtrip import assert_roundtrip
from helpers.trees import SHAPES, Link, make_tree, random_bytes, snapshot
from zipwise.backends.base import ArchiveReader
from zipwise.backends.sevenz import SevenZipReader
from zipwise.backends.single import SingleFileReader
from zipwise.backends.tar import TarReader
from zipwise.backends.zip import ZipReader
from zipwise.detect import detect
from zipwise.model import CreateOptions, ExtractOptions, FormatId
from zipwise.ops import create_archive, extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive
from zipwise.registry import capability, get_reader, get_writer_class

MULTI = [
    FormatId.ZIP,
    FormatId.SEVENZ,
    FormatId.TAR,
    FormatId.TAR_GZ,
    FormatId.TAR_BZ2,
    FormatId.TAR_XZ,
    FormatId.TAR_ZST,
]
SINGLE = [FormatId.GZ, FormatId.BZ2, FormatId.XZ, FormatId.ZST]
SHAPE_NAMES = ["simple", "nested", "unicode", "empty_dirs", "mixed"]
READER_FOR: dict[FormatId, type[ArchiveReader]] = {
    FormatId.ZIP: ZipReader,
    FormatId.SEVENZ: SevenZipReader,
    FormatId.TAR: TarReader,
    FormatId.TAR_GZ: TarReader,
    FormatId.TAR_BZ2: TarReader,
    FormatId.TAR_XZ: TarReader,
    FormatId.TAR_ZST: TarReader,
    FormatId.GZ: SingleFileReader,
    FormatId.BZ2: SingleFileReader,
    FormatId.XZ: SingleFileReader,
    FormatId.ZST: SingleFileReader,
}


@pytest.mark.parametrize("shape", SHAPE_NAMES)
@pytest.mark.parametrize("fmt", MULTI, ids=lambda f: f.value)
def test_roundtrip_every_format_and_tree_shape(tmp_path: Path, fmt: FormatId, shape: str) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES[shape])
    assert_roundtrip(tmp_path, tree, CreateOptions(format=fmt, level="fast"))


@pytest.mark.parametrize("level", ["fast", "normal", 3])
@pytest.mark.parametrize("fmt", MULTI, ids=lambda f: f.value)
def test_roundtrip_levels(tmp_path: Path, fmt: FormatId, level: str | int) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["nested"])
    assert_roundtrip(tmp_path, tree, CreateOptions(format=fmt, level=level))


@pytest.mark.needs_symlink
@pytest.mark.parametrize("fmt", MULTI, ids=lambda f: f.value)
def test_roundtrip_symlinks_are_kept_as_links(tmp_path: Path, fmt: FormatId) -> None:
    spec = {"real.txt": b"data", "sub/inner.txt": b"in", "link.txt": Link("real.txt")}
    tree = make_tree(tmp_path / "src" / "tree", spec)
    assert_roundtrip(tmp_path, tree, CreateOptions(format=fmt, level="fast"))
    link = tmp_path / "extracted" / "tree" / "link.txt"
    assert link.is_symlink()
    assert os.readlink(link).replace("\\", "/") == "real.txt"


@pytest.mark.parametrize("fmt", MULTI, ids=lambda f: f.value)
def test_identical_bytes_for_a_larger_random_file(tmp_path: Path, fmt: FormatId) -> None:
    payload = random_bytes(11, 400_000)
    tree = make_tree(tmp_path / "src" / "tree", {"big.bin": payload, "z.txt": b"\0" * 100_000})
    archive = tmp_path / "big.archive"
    create_archive([tree], archive, CreateOptions(format=fmt, level="fast"))
    extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "tree" / "big.bin").read_bytes() == payload
    assert (tmp_path / "out" / "tree" / "z.txt").read_bytes() == b"\0" * 100_000


@pytest.mark.parametrize("fmt", MULTI, ids=lambda f: f.value)
def test_multiple_sources_and_root_dir(tmp_path: Path, fmt: FormatId) -> None:
    src = tmp_path / "src"
    make_tree(src, {"a/x.txt": b"x", "b/y.txt": b"y", "c.txt": b"c"})
    archive = tmp_path / "multi.archive"
    create_archive([src / "a", src / "b", src / "c.txt"], archive, CreateOptions(format=fmt))
    extract_archive(archive, tmp_path / "out")
    assert sorted(snapshot(tmp_path / "out")) == ["a", "a/x.txt", "b", "b/y.txt", "c.txt"]


@pytest.mark.parametrize("fmt", MULTI, ids=lambda f: f.value)
def test_listing_reports_types_and_sizes(tmp_path: Path, fmt: FormatId) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["empty_dirs"])
    archive = tmp_path / "l.archive"
    create_archive([tree], archive, CreateOptions(format=fmt))
    by_name = {e.name: e for e in list_archive(archive)}
    assert by_name["tree/empty"].type.value == "dir"
    assert by_name["tree/parent/file.txt"].size == 1
    assert by_name["tree/blank.txt"].size == 0


@pytest.mark.parametrize("fmt", SINGLE, ids=lambda f: f.value)
@pytest.mark.parametrize("kind", ["text", "random", "empty"])
def test_single_file_roundtrip(tmp_path: Path, fmt: FormatId, kind: str) -> None:
    payload = {"text": b"line of text\n" * 5000, "random": random_bytes(5, 70_000), "empty": b""}[
        kind
    ]
    src = tmp_path / "src"
    src.mkdir()
    (src / "report.txt").write_bytes(payload)
    os.utime(src / "report.txt", (1_700_000_000, 1_700_000_000))
    archive = tmp_path / f"report.txt.{fmt.value}"
    create_archive([src / "report.txt"], archive, CreateOptions(format=fmt, level="fast"))
    assert verify_archive(archive).ok
    extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "report.txt").read_bytes() == payload


@pytest.mark.parametrize("fmt", MULTI + SINGLE, ids=lambda f: f.value)
def test_detection_picks_the_matching_backend(tmp_path: Path, fmt: FormatId) -> None:
    src = tmp_path / "src"
    make_tree(src, {"one.txt": b"1" * 20_000 + random_bytes(3, 9000)})
    archive = tmp_path / f"a.{fmt.value}"
    create_archive([src / "one.txt"], archive, CreateOptions(format=fmt, level="fast"))
    assert detect(archive).format is fmt
    with get_reader(archive) as reader:
        assert isinstance(reader, READER_FOR[fmt])
        assert reader.format is fmt


@pytest.mark.parametrize("fmt", [f for f in MULTI if f is not FormatId.ZIP], ids=lambda f: f.value)
def test_detection_of_a_tree_with_a_large_first_block(tmp_path: Path, fmt: FormatId) -> None:
    """bz2 and zstd only produce output after a whole block: detection must not be fooled."""
    tree = make_tree(tmp_path / "src" / "t", {"a.bin": random_bytes(21, 150_000)})
    archive = tmp_path / "big.archive"
    create_archive([tree], archive, CreateOptions(format=fmt, level="fast"))
    assert detect(archive).format is fmt


@pytest.mark.parametrize("fmt", MULTI + SINGLE, ids=lambda f: f.value)
def test_every_writable_format_has_a_writer_class(fmt: FormatId) -> None:
    assert get_writer_class(fmt).__module__ == capability(fmt).module


@pytest.mark.parametrize("fmt", MULTI, ids=lambda f: f.value)
def test_extracting_into_an_existing_folder_with_overwrite(tmp_path: Path, fmt: FormatId) -> None:
    from zipwise.model import ConflictAction

    tree = make_tree(tmp_path / "src" / "tree", SHAPES["simple"])
    archive = tmp_path / "o.archive"
    create_archive([tree], archive, CreateOptions(format=fmt))
    dest = tmp_path / "out"
    extract_archive(archive, dest)
    (dest / "tree" / "a.txt").write_bytes(b"changed")
    report = extract_archive(archive, dest, ExtractOptions(on_conflict=ConflictAction.OVERWRITE))
    assert report.ok
    assert (dest / "tree" / "a.txt").read_bytes() == b"hello\n"


@pytest.mark.parametrize("fmt", MULTI, ids=lambda f: f.value)
def test_cancel_during_create_leaves_nothing_behind(tmp_path: Path, fmt: FormatId) -> None:
    from helpers.jobs import stepping_context
    from zipwise.errors import Cancelled

    tree = make_tree(tmp_path / "src" / "t", SHAPES["many_small"])
    ctx, _, _ = stepping_context(cancel_on="t/f1/file006.txt")
    archive = tmp_path / "c.archive"
    with pytest.raises(Cancelled):
        create_archive([tree], archive, CreateOptions(format=fmt, level="fast"), ctx)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src"]


@pytest.mark.parametrize("fmt", MULTI, ids=lambda f: f.value)
def test_cancel_during_extract_leaves_no_partial_files(tmp_path: Path, fmt: FormatId) -> None:
    from helpers.jobs import stepping_context
    from zipwise.errors import Cancelled

    tree = make_tree(tmp_path / "src" / "t", SHAPES["many_small"])
    archive = tmp_path / "c.archive"
    create_archive([tree], archive, CreateOptions(format=fmt, level="fast"))
    ctx, _, _ = stepping_context(cancel_on="t/f1/file006.txt")
    with pytest.raises(Cancelled):
        extract_archive(archive, tmp_path / "out", ExtractOptions(), ctx)
    assert not list((tmp_path / "out").rglob("*.zwpart"))


@pytest.mark.parametrize("fmt", MULTI + SINGLE, ids=lambda f: f.value)
def test_progress_covers_every_byte(tmp_path: Path, fmt: FormatId) -> None:
    from helpers.jobs import stepping_context

    src = tmp_path / "src"
    make_tree(src, {"a.txt": b"a" * 30_000, "b.txt": random_bytes(2, 20_000)})
    sources = [src / "a.txt"] if fmt in SINGLE else [src]
    total = 30_000 if fmt in SINGLE else 50_000
    ctx, _, events = stepping_context()
    archive = tmp_path / "p.archive"
    create_archive(sources, archive, CreateOptions(format=fmt, level="fast"), ctx)
    compress = [e for e in events if e.phase == "compress"]
    assert max(e.bytes_done for e in compress) == total
    events.clear()
    extract_ctx, _, extract_events = stepping_context()
    extract_archive(archive, tmp_path / "out", ExtractOptions(), extract_ctx)
    assert max(e.bytes_done for e in extract_events if e.phase == "extract") == total
