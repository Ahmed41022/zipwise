from __future__ import annotations

import os
import stat
import tarfile
from pathlib import Path

import pytest

from helpers.builders import (
    CODECS,
    RawTarMember,
    compress_bytes,
    tar_bytes,
    tar_dir,
    tar_file,
    tar_hardlink,
    tar_special,
    tar_symlink,
    write_raw_tar,
)
from helpers.jobs import stepping_context
from helpers.trees import SHAPES, Link, make_tree, random_bytes, snapshot
from zipwise.backends.tar import TarReader, TarWriter
from zipwise.errors import Cancelled, CorruptArchive, UsageError
from zipwise.model import CreateOptions, EntryType, ExtractOptions, FormatId
from zipwise.ops import archive_info, create_archive, extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive

TAR_FORMATS = [
    FormatId.TAR,
    FormatId.TAR_GZ,
    FormatId.TAR_BZ2,
    FormatId.TAR_XZ,
    FormatId.TAR_ZST,
]
CODEC_OF = dict(zip(TAR_FORMATS, CODECS, strict=True))


def sample_members() -> list[RawTarMember]:
    return [
        tar_dir("d"),
        tar_file("d/a.txt", b"alpha"),
        tar_file("b.bin", random_bytes(1, 3000)),
        tar_symlink("ln", "d/a.txt"),
        tar_hardlink("hard", "d/a.txt"),
        tar_special("fifo", tarfile.FIFOTYPE),
        tar_special("chr", tarfile.CHRTYPE),
    ]


# --- reading ----------------------------------------------------------------------------------


@pytest.mark.parametrize("codec", CODECS)
def test_entries_have_types_sizes_and_links(tmp_path: Path, codec: str) -> None:
    path = write_raw_tar(tmp_path / "a.tar", sample_members(), codec)
    entries = {e.name: e for e in list_archive(path)}
    assert entries["d"].type is EntryType.DIR
    assert entries["d/a.txt"].type is EntryType.FILE
    assert entries["d/a.txt"].size == 5
    assert entries["b.bin"].size == 3000
    assert entries["ln"].type is EntryType.SYMLINK
    assert entries["ln"].link_target == "d/a.txt"
    assert entries["hard"].type is EntryType.HARDLINK
    assert entries["hard"].link_target == "d/a.txt"
    assert entries["fifo"].type is EntryType.OTHER
    assert entries["chr"].type is EntryType.OTHER
    assert all(e.compressed_size is None for e in entries.values())
    assert [e.index for e in entries.values()] == list(range(len(entries)))


@pytest.mark.parametrize("codec", CODECS)
def test_reader_reports_the_format_from_the_content(tmp_path: Path, codec: str) -> None:
    path = write_raw_tar(tmp_path / "noext", [tar_file("a")], codec)
    expected = TAR_FORMATS[CODECS.index(codec)]
    with TarReader(path) as reader:
        assert reader.format is expected
    assert archive_info(path).format is expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_modes_and_mtimes_are_listed(tmp_path: Path) -> None:
    members = [RawTarMember("x.sh", b"#!/bin/sh", mode=0o755, mtime=1_600_000_000)]
    entries = list_archive(write_raw_tar(tmp_path / "m.tar", members))
    assert entries[0].mode == 0o755
    assert int(entries[0].mtime.timestamp()) == 1_600_000_000  # type: ignore[union-attr]


@pytest.mark.parametrize("codec", CODECS)
def test_extract_through_the_sink_handles_links_and_skips_special_files(
    tmp_path: Path, codec: str
) -> None:
    path = write_raw_tar(tmp_path / "a.tar", sample_members(), codec)
    report = extract_archive(path, tmp_path / "o")
    out = tmp_path / "o"
    assert (out / "d" / "a.txt").read_bytes() == b"alpha"
    assert (out / "b.bin").read_bytes() == random_bytes(1, 3000)
    assert (out / "hard").read_bytes() == b"alpha"
    assert not (out / "fifo").exists()
    assert not (out / "chr").exists()
    assert [i.code for i in report.issues].count("special-file") == 2
    assert report.entries_skipped == 2


def test_symlink_is_recreated_when_allowed(tmp_path: Path) -> None:
    from zipwise.fsutil import can_create_symlinks

    tmp_path.joinpath("probe").mkdir()
    if not can_create_symlinks(tmp_path / "probe"):
        pytest.skip("the OS user may not create symlinks")
    path = write_raw_tar(tmp_path / "a.tar", sample_members())
    extract_archive(path, tmp_path / "o")
    assert (tmp_path / "o" / "ln").is_symlink()
    assert (tmp_path / "o" / "ln").read_bytes() == b"alpha"


def test_extracting_a_later_entry_only(tmp_path: Path) -> None:
    path = write_raw_tar(tmp_path / "a.tar.gz", sample_members(), "gz")
    report = extract_archive(path, tmp_path / "o", ExtractOptions(only=["b.bin"]))
    assert report.ok
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == ["b.bin"]


def test_entries_are_cached_and_extract_can_run_twice(tmp_path: Path) -> None:
    path = write_raw_tar(tmp_path / "a.tar.xz", sample_members(), "xz")
    with TarReader(path) as reader:
        assert reader.entries() is reader.entries()
    extract_archive(path, tmp_path / "o1")
    extract_archive(path, tmp_path / "o2")
    assert snapshot(tmp_path / "o1").keys() == snapshot(tmp_path / "o2").keys()


def test_concatenated_zstd_frames_are_read_as_one_stream(tmp_path: Path) -> None:
    raw = tar_bytes([tar_file("a.txt", b"x" * 5000), tar_file("b.txt", b"y" * 5000)])
    cut = len(raw) // 2
    path = tmp_path / "multi.tar.zst"
    path.write_bytes(compress_bytes(raw[:cut], "zst") + compress_bytes(raw[cut:], "zst"))
    extract_archive(path, tmp_path / "o")
    assert (tmp_path / "o" / "b.txt").read_bytes() == b"y" * 5000


def test_names_in_another_encoding_use_the_override(tmp_path: Path) -> None:
    buf = tmp_path / "latin.tar"
    import io

    raw = io.BytesIO()
    with tarfile.TarFile(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT, encoding="latin-1") as t:
        info = tarfile.TarInfo("café.txt")
        info.size = 2
        t.addfile(info, io.BytesIO(b"ok"))
    buf.write_bytes(raw.getvalue())
    assert [e.name for e in list_archive(buf, name_encoding="latin-1")] == ["café.txt"]
    assert "�" in list_archive(buf)[0].name


def test_unknown_name_encoding_is_a_usage_error(tmp_path: Path) -> None:
    path = write_raw_tar(tmp_path / "a.tar", [tar_file("a")])
    with pytest.raises(UsageError):
        TarReader(path, name_encoding="not-a-codec")


def test_info_counts_files(tmp_path: Path) -> None:
    path = write_raw_tar(tmp_path / "a.tar.gz", sample_members(), "gz")
    info = archive_info(path)
    assert info.entries_count == 7
    assert info.total_size == 5 + 3000
    assert info.compressed_size == path.stat().st_size
    assert info.encrypted is False


# --- writing ----------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", TAR_FORMATS, ids=lambda f: f.value)
def test_long_and_unicode_names_use_pax_headers(tmp_path: Path, fmt: FormatId) -> None:
    long_name = "d" * 120 + "/" + "f" * 150 + ".txt"
    spec = {long_name: b"long", "日本語/ملف.txt": b"uni"}
    tree = make_tree(tmp_path / "src" / "t", spec)
    archive = tmp_path / "p.archive"
    create_archive([tree], archive, CreateOptions(format=fmt, level="fast"))
    names = {e.name for e in list_archive(archive)}
    assert "t/" + long_name in names
    assert "t/日本語/ملف.txt" in names
    extract_archive(archive, tmp_path / "o")
    assert (tmp_path / "o" / "t" / long_name).read_bytes() == b"long"


def test_archive_is_standard_pax_readable_by_tarfile(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", {"ünï/ä.txt": b"u", "e/": None})
    archive = tmp_path / "p.tar.gz"
    create_archive([tree], archive, CreateOptions(format=FormatId.TAR_GZ))
    with tarfile.open(archive, "r:gz") as tf:
        names = tf.getnames()
        assert "t/ünï/ä.txt" in names
        member = tf.getmember("t/ünï/ä.txt")
        assert member.pax_headers.get("path") == "t/ünï/ä.txt"
        assert tf.getmember("t/e").isdir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_modes_roundtrip(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", {"run.sh": b"#!/bin/sh\n", "ro.txt": b"x"})
    os.chmod(tree / "run.sh", 0o755)
    os.chmod(tree / "ro.txt", 0o440)
    archive = tmp_path / "m.tar"
    create_archive([tree], archive, CreateOptions(format=FormatId.TAR))
    extract_archive(archive, tmp_path / "o")
    assert stat.S_IMODE((tmp_path / "o" / "t" / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "o" / "t" / "ro.txt").stat().st_mode) == 0o440


@pytest.mark.needs_symlink
def test_symlinks_are_stored_as_links(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", {"real.txt": b"data", "ln": Link("real.txt")})
    archive = tmp_path / "s.tar"
    create_archive([tree], archive, CreateOptions(format=FormatId.TAR))
    with tarfile.open(archive) as tf:
        assert tf.getmember("t/ln").issym()
        assert tf.getmember("t/ln").linkname == "real.txt"


def test_writer_accepts_a_stream_and_leaves_it_open(tmp_path: Path) -> None:
    src = tmp_path / "f.txt"
    src.write_bytes(b"hello")
    with (tmp_path / "s.tar.gz").open("wb") as fh:
        with TarWriter(fh, CreateOptions(format=FormatId.TAR_GZ)) as writer:
            writer.add_file(src, "f.txt")
        assert not fh.closed
    extract_archive(tmp_path / "s.tar.gz", tmp_path / "o")
    assert (tmp_path / "o" / "f.txt").read_bytes() == b"hello"


def test_writer_refuses_a_non_tar_format(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        TarWriter(tmp_path / "x", CreateOptions(format=FormatId.ZIP))


def test_cancelled_creation_leaves_nothing_behind(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["many_small"])
    ctx, _, _ = stepping_context(cancel_on="t/f1/file006.txt")
    archive = tmp_path / "c.tar.gz"
    with pytest.raises(Cancelled):
        create_archive([tree], archive, CreateOptions(format=FormatId.TAR_GZ), ctx)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src"]


@pytest.mark.parametrize("fmt", TAR_FORMATS, ids=lambda f: f.value)
def test_compressed_output_is_smaller_for_repetitive_data(tmp_path: Path, fmt: FormatId) -> None:
    tree = make_tree(tmp_path / "src" / "t", {"r.txt": b"abc" * 50_000})
    archive = tmp_path / "r.archive"
    create_archive([tree], archive, CreateOptions(format=fmt, level="normal"))
    if fmt is FormatId.TAR:
        assert archive.stat().st_size > 150_000
    else:
        assert archive.stat().st_size < 20_000


# --- damaged streams --------------------------------------------------------------------------


def big_members() -> list[RawTarMember]:
    return [tar_file("a.bin", random_bytes(2, 60_000)), tar_file("b.bin", random_bytes(3, 60_000))]


@pytest.mark.parametrize("codec", CODECS)
def test_truncated_in_the_middle_is_corrupt(tmp_path: Path, codec: str) -> None:
    path = write_raw_tar(tmp_path / "t.tar", big_members(), codec)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    with pytest.raises(CorruptArchive):
        TarReader(path).entries()


@pytest.mark.parametrize("codec", CODECS[1:])
def test_truncated_at_the_end_is_corrupt(tmp_path: Path, codec: str) -> None:
    path = write_raw_tar(tmp_path / "t.tar", big_members(), codec)
    data = path.read_bytes()
    path.write_bytes(data[:-6])
    with pytest.raises(CorruptArchive):
        TarReader(path).entries()


@pytest.mark.parametrize("codec", CODECS[1:])
def test_flipped_bytes_are_corrupt(tmp_path: Path, codec: str) -> None:
    path = write_raw_tar(tmp_path / "t.tar", big_members(), codec)
    data = bytearray(path.read_bytes())
    for offset in range(len(data) // 3, len(data) // 3 + 40):
        data[offset] ^= 0x5A
    path.write_bytes(bytes(data))
    with pytest.raises(CorruptArchive):
        TarReader(path).entries()


@pytest.mark.parametrize("codec", CODECS)
def test_extract_of_a_truncated_archive_fails_cleanly(tmp_path: Path, codec: str) -> None:
    path = write_raw_tar(tmp_path / "t.tar", big_members(), codec)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) * 3 // 4])
    with pytest.raises(CorruptArchive):
        extract_archive(path, tmp_path / "o")
    assert not list((tmp_path / "o").rglob("*.zwpart"))


def test_truncated_plain_tar_inside_a_member_is_reported_by_test(tmp_path: Path) -> None:
    path = write_raw_tar(tmp_path / "t.tar", big_members())
    data = path.read_bytes()
    path.write_bytes(data[:100_000])
    with pytest.raises(CorruptArchive):
        verify_archive(path)


def test_test_reports_an_error_issue_for_a_damaged_member_stream(tmp_path: Path) -> None:
    path = write_raw_tar(tmp_path / "t.tar", big_members(), "gz")
    reader = TarReader(path)
    entries_before = reader.entries()
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    issues = reader.test(stepping_context()[0])
    reader.close()
    assert len(entries_before) == 2
    assert issues
    assert issues[0].severity == "error"
    assert issues[0].code == "corrupt-archive"
