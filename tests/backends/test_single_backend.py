from __future__ import annotations

import bz2
import gzip
import lzma
import os
import struct
import zlib
from pathlib import Path

import pytest

from helpers.builders import compress_bytes
from helpers.trees import make_tree, random_bytes
from zipwise.backends.single import (
    SingleFileReader,
    SingleFileWriter,
    output_name,
    read_gzip_header,
)
from zipwise.errors import BombDetected, CorruptArchive, UsageError
from zipwise.jobs import JobContext
from zipwise.model import CreateOptions, EntryType, ExtractOptions, FormatId
from zipwise.ops import archive_info, create_archive, extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive
from zipwise.safety import SafetyPolicy

CODECS = ["gz", "bz2", "xz", "zst"]
FORMAT_OF = {"gz": FormatId.GZ, "bz2": FormatId.BZ2, "xz": FormatId.XZ, "zst": FormatId.ZST}
PAYLOAD = b"some repeated text\n" * 2000


def write_single(path: Path, codec: str, data: bytes = PAYLOAD) -> Path:
    path.write_bytes(compress_bytes(data, codec))
    return path


# --- output naming ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("archive", "codec", "expected"),
    [
        ("report.txt.gz", "gz", "report.txt"),
        ("report.txt.bz2", "bz2", "report.txt"),
        ("report.txt.xz", "xz", "report.txt"),
        ("report.txt.zst", "zst", "report.txt"),
        ("a.b.c.gz", "gz", "a.b.c"),
        ("DATA.GZ", "gz", "DATA"),
        ("notes.bz", "bz2", "notes"),
        ("notes.zstd", "zst", "notes"),
        ("noext", "gz", "noext.out"),
        ("wrong.xz", "gz", "wrong.xz.out"),
        ("report.txt.gz.001", "gz", "report.txt"),
        (".gz", "gz", ".gz.out"),
    ],
)
def test_output_name_from_the_archive_name(archive: str, codec: str, expected: str) -> None:
    assert output_name(archive, codec) == expected


@pytest.mark.parametrize("codec", CODECS)
def test_listing_uses_the_archive_name_without_the_suffix(tmp_path: Path, codec: str) -> None:
    path = write_single(tmp_path / f"report.txt.{codec}", codec)
    entries = list_archive(path)
    assert [e.name for e in entries] == ["report.txt"]
    assert entries[0].type is EntryType.FILE


def test_gzip_header_name_wins_over_the_archive_name(tmp_path: Path) -> None:
    path = tmp_path / "renamed.gz"
    with (
        open(path, "wb") as raw,
        gzip.GzipFile(filename="original.dat", fileobj=raw, mode="wb", mtime=1_600_000_000) as g,
    ):
        g.write(PAYLOAD)
    entry = list_archive(path)[0]
    assert entry.name == "original.dat"
    assert int(entry.mtime.timestamp()) == 1_600_000_000  # type: ignore[union-attr]
    extract_archive(path, tmp_path / "o")
    assert (tmp_path / "o" / "original.dat").read_bytes() == PAYLOAD


@pytest.mark.parametrize(
    ("header_name", "expected"),
    [("dir/sub/inner.txt", "inner.txt"), ("..\\..\\evil.txt", "evil.txt"), ("..", "x"), ("/", "x")],
)
def test_gzip_header_name_is_reduced_to_a_file_name(
    tmp_path: Path, header_name: str, expected: str
) -> None:
    path = tmp_path / "x.gz"
    with open(path, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb") as g:
        g.write(b"data")
    data = bytearray(path.read_bytes())
    data[3] |= 0x08
    named = bytes(data[:10]) + header_name.encode() + b"\0" + bytes(data[10:])
    path.write_bytes(named)
    assert list_archive(path)[0].name == expected


def test_gzip_header_name_in_utf8_and_latin1(tmp_path: Path) -> None:
    def build(name: bytes) -> bytes:
        comp = zlib.compressobj(6, zlib.DEFLATED, -15)
        body = comp.compress(b"x") + comp.flush()
        head = b"\x1f\x8b\x08\x08" + struct.pack("<I", 0) + b"\x00\xff" + name + b"\0"
        return head + body + struct.pack("<II", zlib.crc32(b"x"), 1)

    utf = tmp_path / "u.gz"
    utf.write_bytes(build("résumé.txt".encode()))
    assert list_archive(utf)[0].name == "résumé.txt"
    lat = tmp_path / "l.gz"
    lat.write_bytes(build("résumé.txt".encode("latin-1")))
    assert list_archive(lat)[0].name == "résumé.txt"


def test_read_gzip_header_handles_extra_field_and_short_input() -> None:
    import io

    extra = b"\x1f\x8b\x08\x0c" + struct.pack("<I", 7) + b"\x00\xff" + struct.pack("<H", 3)
    stream = io.BytesIO(extra + b"abc" + b"named.bin\0" + b"rest")
    assert read_gzip_header(stream) == ("named.bin", 7)
    assert read_gzip_header(io.BytesIO(b"\x1f\x8b")) == (None, 0)
    assert read_gzip_header(io.BytesIO(b"PK\x03\x04" + b"\0" * 20)) == (None, 0)


# --- unknown size and the byte budget ---------------------------------------------------------


@pytest.mark.parametrize("codec", CODECS)
def test_size_is_unknown(tmp_path: Path, codec: str) -> None:
    path = write_single(tmp_path / f"x.{codec}", codec)
    entry = list_archive(path)[0]
    assert entry.size == -1
    assert entry.compressed_size == path.stat().st_size


def test_info_reports_the_gzip_trailer_size_and_unknown_for_others(tmp_path: Path) -> None:
    gz = write_single(tmp_path / "x.gz", "gz")
    assert archive_info(gz).total_size == len(PAYLOAD)
    assert archive_info(gz).format is FormatId.GZ
    xz = write_single(tmp_path / "x.xz", "xz")
    assert archive_info(xz).total_size == -1
    assert archive_info(xz).entries_count == 1


@pytest.mark.parametrize("codec", CODECS)
def test_output_larger_than_the_total_limit_triggers_the_byte_budget(
    tmp_path: Path, codec: str
) -> None:
    path = write_single(tmp_path / f"zeros.{codec}", codec, b"\0" * 3_000_000)
    policy = SafetyPolicy(max_total_bytes=500_000)
    dest = tmp_path / "o"
    with pytest.raises(BombDetected) as caught:
        extract_archive(path, dest, ExtractOptions(policy=policy))
    assert caught.value.code == "size-limit"
    assert list(dest.iterdir()) == []  # the partial output file was removed


def test_per_entry_limit_stops_an_unknown_size_stream(tmp_path: Path) -> None:
    path = write_single(tmp_path / "zeros.gz", "gz", b"\0" * 3_000_000)
    policy = SafetyPolicy(max_entry_bytes=100_000)
    with pytest.raises(BombDetected):
        extract_archive(path, tmp_path / "o", ExtractOptions(policy=policy))
    assert list((tmp_path / "o").iterdir()) == []


def test_without_a_limit_the_free_disk_space_bounds_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_single(tmp_path / "zeros.gz", "gz", b"\0" * 3_000_000)
    monkeypatch.setattr("zipwise.ops.extract.free_space", lambda _p: 1_000_000)
    with pytest.raises(BombDetected) as caught:
        extract_archive(path, tmp_path / "o")
    assert caught.value.code == "size-limit"
    assert list((tmp_path / "o").iterdir()) == []


def test_within_the_limit_the_stream_extracts_fully(tmp_path: Path) -> None:
    path = write_single(tmp_path / "ok.xz", "xz", b"\0" * 300_000)
    report = extract_archive(
        path, tmp_path / "o", ExtractOptions(policy=SafetyPolicy(max_total_bytes=400_000))
    )
    assert report.ok
    assert (tmp_path / "o" / "ok").stat().st_size == 300_000


# --- damaged streams --------------------------------------------------------------------------


@pytest.mark.parametrize("codec", CODECS)
def test_truncated_stream_is_corrupt(tmp_path: Path, codec: str) -> None:
    data = random_bytes(7, 40_000)
    path = write_single(tmp_path / f"t.{codec}", codec, data)
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) - 9])
    with pytest.raises(CorruptArchive):
        extract_archive(path, tmp_path / "o")
    assert list((tmp_path / "o").iterdir()) == []
    report = verify_archive(path)
    assert not report.ok
    assert report.issues[0].code == "corrupt-archive"


@pytest.mark.parametrize("codec", CODECS)
def test_flipped_bytes_are_corrupt(tmp_path: Path, codec: str) -> None:
    path = write_single(tmp_path / f"t.{codec}", codec, random_bytes(8, 40_000))
    raw = bytearray(path.read_bytes())
    for offset in range(len(raw) // 2, len(raw) // 2 + 30):
        raw[offset] ^= 0xA5
    path.write_bytes(bytes(raw))
    with pytest.raises(CorruptArchive):
        extract_archive(path, tmp_path / "o")


def test_trailing_garbage_in_gzip_is_corrupt(tmp_path: Path) -> None:
    path = write_single(tmp_path / "g.gz", "gz")
    path.write_bytes(path.read_bytes() + b"not a gzip member at all")
    with pytest.raises(CorruptArchive):
        extract_archive(path, tmp_path / "o")


def test_multi_member_gzip_and_bz2_are_read_to_the_end(tmp_path: Path) -> None:
    gz = tmp_path / "m.gz"
    gz.write_bytes(gzip.compress(b"one ") + gzip.compress(b"two"))
    extract_archive(gz, tmp_path / "o1")
    assert (tmp_path / "o1" / "m").read_bytes() == b"one two"
    bz = tmp_path / "m.bz2"
    bz.write_bytes(bz2.compress(b"one ") + bz2.compress(b"two"))
    extract_archive(bz, tmp_path / "o2")
    assert (tmp_path / "o2" / "m").read_bytes() == b"one two"


def test_a_plain_file_is_rejected_by_the_reader(tmp_path: Path) -> None:
    path = tmp_path / "plain.gz"
    path.write_bytes(b"just text")
    with pytest.raises(CorruptArchive):
        SingleFileReader(path)


# --- creating ---------------------------------------------------------------------------------


@pytest.mark.parametrize("codec", CODECS)
def test_created_file_is_a_standard_stream(tmp_path: Path, codec: str) -> None:
    src = tmp_path / "src.txt"
    src.write_bytes(PAYLOAD)
    archive = tmp_path / f"src.txt.{codec}"
    create_archive([src], archive, CreateOptions(format=FORMAT_OF[codec], level="fast"))
    raw = archive.read_bytes()
    if codec == "gz":
        assert gzip.decompress(raw) == PAYLOAD
    elif codec == "bz2":
        assert bz2.decompress(raw) == PAYLOAD
    elif codec == "xz":
        assert lzma.decompress(raw) == PAYLOAD
    else:
        import zstandard

        assert zstandard.ZstdDecompressor().decompressobj().decompress(raw) == PAYLOAD


def test_created_gzip_stores_name_and_mtime(tmp_path: Path) -> None:
    src = tmp_path / "résumé.txt"
    src.write_bytes(b"data")
    os.utime(src, (1_650_000_000, 1_650_000_000))
    archive = tmp_path / "whatever.gz"
    create_archive([src], archive, CreateOptions(format=FormatId.GZ))
    with archive.open("rb") as fh:
        assert read_gzip_header(fh) == ("résumé.txt", 1_650_000_000)
    extract_archive(archive, tmp_path / "o")
    assert (tmp_path / "o" / "résumé.txt").read_bytes() == b"data"
    assert abs((tmp_path / "o" / "résumé.txt").stat().st_mtime - 1_650_000_000) < 2


@pytest.mark.parametrize("fmt", list(FORMAT_OF.values()), ids=lambda f: f.value)
def test_two_sources_are_a_usage_error_and_leave_nothing(tmp_path: Path, fmt: FormatId) -> None:
    make_tree(tmp_path / "src", {"a.txt": b"a", "b.txt": b"b"})
    archive = tmp_path / f"x.{fmt.value}"
    with pytest.raises(UsageError):
        sources = [tmp_path / "src" / "a.txt", tmp_path / "src" / "b.txt"]
        create_archive(sources, archive, CreateOptions(format=fmt))
    assert not archive.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_a_directory_source_is_a_usage_error(tmp_path: Path) -> None:
    make_tree(tmp_path / "src", {"only.txt": b"a"})
    with pytest.raises(UsageError):
        create_archive([tmp_path / "src"], tmp_path / "x.gz", CreateOptions(format=FormatId.GZ))


def test_writer_methods_other_than_add_file(tmp_path: Path) -> None:
    writer = SingleFileWriter(tmp_path / "x.gz", CreateOptions(format=FormatId.GZ))
    with pytest.raises(UsageError):
        writer.add_dir("d", 0.0, None)
    with pytest.raises(UsageError):
        writer.add_symlink("l", "t", 0.0)
    with pytest.raises(UsageError):
        writer.close()  # nothing was added


def test_second_add_file_is_refused(tmp_path: Path) -> None:
    src = tmp_path / "a.txt"
    src.write_bytes(b"a")
    writer = SingleFileWriter(tmp_path / "x.xz", CreateOptions(format=FormatId.XZ))
    writer.add_file(src, "a.txt")
    with pytest.raises(UsageError):
        writer.add_file(src, "a.txt")
    writer.abort()


def test_writer_refuses_a_multi_file_format(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        SingleFileWriter(tmp_path / "x", CreateOptions(format=FormatId.TAR_GZ))


def test_writer_streams_into_an_open_file(tmp_path: Path) -> None:
    src = tmp_path / "a.txt"
    src.write_bytes(PAYLOAD)
    with (tmp_path / "s.zst").open("wb") as fh:
        with SingleFileWriter(fh, CreateOptions(format=FormatId.ZST), JobContext()) as writer:
            writer.add_file(src, "a.txt")
        assert not fh.closed
    extract_archive(tmp_path / "s.zst", tmp_path / "o")
    assert (tmp_path / "o" / "s").read_bytes() == PAYLOAD
