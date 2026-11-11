from __future__ import annotations

import io
import os
import stat
from pathlib import Path
from typing import BinaryIO

import pytest

from helpers.builders import write_raw_7z
from helpers.jobs import stepping_context
from helpers.roundtrip import assert_roundtrip
from helpers.trees import SHAPES, Link, make_tree, random_bytes, snapshot
from zipwise.backends.sevenz import SevenZipReader, SevenZipWriter
from zipwise.errors import (
    PASSWORD_REQUIRED_HINT,
    BombDetected,
    Cancelled,
    CorruptArchive,
    PasswordRequired,
    UsageError,
    WrongPassword,
)
from zipwise.jobs import JobContext
from zipwise.model import (
    ArchiveEntry,
    ConflictAction,
    CreateOptions,
    EntryType,
    ExtractOptions,
    FormatId,
)
from zipwise.ops import archive_info, create_archive, extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive

OPTS = CreateOptions(format=FormatId.SEVENZ, level="fast")
PASSWORD = "pässwörd Ù…"


def make_7z(tmp_path: Path, shape: str = "nested", **kw: object) -> Path:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES[shape])
    out = tmp_path / "a.7z"
    options = CreateOptions(format=FormatId.SEVENZ, level="fast", **kw)  # type: ignore[arg-type]
    create_archive([tree], out, options)
    return out


@pytest.fixture(scope="module")
def encrypted_7z(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Built once: deriving the AES key makes every password test slow."""
    return make_7z(tmp_path_factory.mktemp("enc"), password=PASSWORD)


@pytest.fixture(scope="module")
def header_encrypted_7z(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_7z(tmp_path_factory.mktemp("hdr"), password=PASSWORD, encrypt_names=True)


class RecordingSink:
    """Collects what a backend pushes, without touching the file system."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.opened: list[ArchiveEntry] = []
        self.closed: list[tuple[str, bool]] = []
        self.data: dict[str, bytearray] = {}
        self._streams: dict[int, _Stream] = {}
        self.fail_after = fail_after

    def open(self, entry: ArchiveEntry) -> BinaryIO | None:
        self.opened.append(entry)
        if entry.type is not EntryType.FILE:
            return None
        stream = _Stream(self.data.setdefault(entry.name, bytearray()), self.fail_after)
        self._streams[entry.index] = stream
        return stream  # type: ignore[return-value]

    def close(self, entry: ArchiveEntry, stream: BinaryIO, ok: bool) -> None:
        self.closed.append((entry.name, ok))


class _Stream:
    def __init__(self, sink: bytearray, fail_after: int | None) -> None:
        self._sink = sink
        self._fail_after = fail_after

    def write(self, data: bytes) -> int:
        self._sink += data
        if self._fail_after is not None and len(self._sink) > self._fail_after:
            raise BombDetected("test overrun")
        return len(data)


# --- listing and metadata ---------------------------------------------------------------------


def test_listing_has_types_sizes_and_mtimes(tmp_path: Path) -> None:
    archive = make_7z(tmp_path, "empty_dirs")
    entries = {e.name: e for e in list_archive(archive)}
    assert entries["tree/empty"].type is EntryType.DIR
    assert entries["tree/parent/child"].type is EntryType.DIR
    assert entries["tree/parent/file.txt"].size == 1
    assert entries["tree/blank.txt"].size == 0
    assert all(e.mtime is not None for e in entries.values())
    src_mtime = (tmp_path / "src" / "tree" / "parent" / "file.txt").stat().st_mtime
    assert abs(entries["tree/parent/file.txt"].mtime.timestamp() - src_mtime) < 1  # type: ignore[union-attr]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_modes_are_stored(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", {"run.sh": b"#!/bin/sh\n", "plain.txt": b"x"})
    os.chmod(tree / "run.sh", 0o755)
    os.chmod(tree / "plain.txt", 0o640)
    out = tmp_path / "m.7z"
    create_archive([tree], out, OPTS)
    modes = {e.name: e.mode for e in list_archive(out)}
    assert modes["t/run.sh"] == 0o755
    assert modes["t/plain.txt"] == 0o640
    extract_archive(out, tmp_path / "o")
    assert stat.S_IMODE((tmp_path / "o" / "t" / "run.sh").stat().st_mode) == 0o755


def test_info_summarises_the_archive(tmp_path: Path) -> None:
    archive = make_7z(tmp_path)
    info = archive_info(archive)
    assert info.format is FormatId.SEVENZ
    assert info.entries_count == len(list_archive(archive))
    assert info.total_size == sum(e.size for e in list_archive(archive))
    assert 0 < info.compressed_size <= archive.stat().st_size
    assert info.encrypted is False


def test_solid_archive_keeps_a_compressed_size_for_the_bomb_ratio(tmp_path: Path) -> None:
    archive = make_7z(tmp_path)
    assert any(e.compressed_size for e in list_archive(archive))


@pytest.mark.needs_symlink
def test_symlink_entries_carry_their_target(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", {"real.txt": b"data", "ln": Link("real.txt")})
    out = tmp_path / "s.7z"
    create_archive([tree], out, OPTS)
    entries = {e.name: e for e in list_archive(out)}
    assert entries["t/ln"].type is EntryType.SYMLINK
    assert entries["t/ln"].link_target == "real.txt"


def test_store_flag_is_ignored_in_a_solid_archive(tmp_path: Path) -> None:
    src = tmp_path / "f.bin"
    src.write_bytes(random_bytes(1, 5000))
    dest = tmp_path / "s.7z"
    with SevenZipWriter(dest, OPTS) as writer:
        writer.add_file(src, "f.bin", store=True)
    extract_archive(dest, tmp_path / "o")
    assert (tmp_path / "o" / "f.bin").read_bytes() == src.read_bytes()


@pytest.mark.parametrize("level", [0, 1, 5, "fast", "normal"])
def test_level_presets_roundtrip(tmp_path: Path, level: str | int) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["nested"])
    assert_roundtrip(tmp_path, tree, CreateOptions(format=FormatId.SEVENZ, level=level))


def test_invalid_level_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        SevenZipWriter(tmp_path / "x.7z", CreateOptions(format=FormatId.SEVENZ, level=11))


def test_writer_accepts_a_stream_and_leaves_it_open(tmp_path: Path) -> None:
    src = tmp_path / "f.txt"
    src.write_bytes(b"hello")
    with (tmp_path / "s.7z").open("wb") as fh:
        with SevenZipWriter(fh, OPTS) as writer:
            writer.add_file(src, "f.txt")
        assert not fh.closed
    extract_archive(tmp_path / "s.7z", tmp_path / "o")
    assert (tmp_path / "o" / "f.txt").read_bytes() == b"hello"


# --- passwords --------------------------------------------------------------------------------


@pytest.mark.parametrize("names", [False, True], ids=["data-only", "header-too"])
def test_password_roundtrip(tmp_path: Path, names: bool) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["mixed"])
    options = CreateOptions(
        format=FormatId.SEVENZ, level="fast", password=PASSWORD, encrypt_names=names
    )
    assert_roundtrip(tmp_path, tree, options)


def test_encrypted_entries_are_flagged_and_listable_without_a_password(
    encrypted_7z: Path,
) -> None:
    archive = encrypted_7z
    entries = list_archive(archive)
    assert any(e.encrypted for e in entries if e.type is EntryType.FILE)
    assert archive_info(archive).encrypted is True


@pytest.mark.parametrize("operation", ["extract", "test"])
def test_missing_password_is_reported(tmp_path: Path, encrypted_7z: Path, operation: str) -> None:
    archive = encrypted_7z
    with pytest.raises(PasswordRequired):
        if operation == "extract":
            extract_archive(archive, tmp_path / "o")
        else:
            verify_archive(archive)
    assert not (tmp_path / "o").exists() or not any((tmp_path / "o").iterdir())


@pytest.mark.parametrize("operation", ["extract", "test"])
def test_wrong_password_is_reported(tmp_path: Path, encrypted_7z: Path, operation: str) -> None:
    archive = encrypted_7z
    with pytest.raises(WrongPassword):
        if operation == "extract":
            extract_archive(archive, tmp_path / "o", ExtractOptions(password="nope"))
        else:
            verify_archive(archive, password="nope")


def test_header_encrypted_archive_without_password(
    tmp_path: Path, header_encrypted_7z: Path
) -> None:
    archive = header_encrypted_7z
    with pytest.raises(PasswordRequired):
        list_archive(archive)
    with pytest.raises(PasswordRequired):
        extract_archive(archive, tmp_path / "o")


def test_password_required_hint_is_front_end_neutral(
    encrypted_7z: Path, header_encrypted_7z: Path
) -> None:
    caught: list[PasswordRequired] = []
    for call in (lambda: list_archive(header_encrypted_7z), lambda: verify_archive(encrypted_7z)):
        with pytest.raises(PasswordRequired) as info:
            call()
        caught.append(info.value)
    for exc in caught:
        assert exc.hint == PASSWORD_REQUIRED_HINT
        assert "--" not in (exc.hint or "") and "dialog" not in (exc.hint or "")


def test_header_encrypted_archive_lists_with_its_password(header_encrypted_7z: Path) -> None:
    assert list_archive(header_encrypted_7z, password=PASSWORD)
    assert archive_info(header_encrypted_7z, password=PASSWORD).encrypted is True


def test_header_encrypted_archive_with_wrong_password(header_encrypted_7z: Path) -> None:
    archive = header_encrypted_7z
    with pytest.raises(WrongPassword):
        list_archive(archive, password="wrong")


def test_password_callback_supplies_a_missing_password(tmp_path: Path, encrypted_7z: Path) -> None:
    archive = encrypted_7z
    ctx = JobContext(ask_password=lambda: PASSWORD)
    report = extract_archive(archive, tmp_path / "o", ExtractOptions(), ctx)
    assert report.ok
    assert (tmp_path / "o" / "tree" / "top.txt").read_bytes() == b"top"


def test_names_encryption_without_a_password_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        SevenZipWriter(tmp_path / "x.7z", CreateOptions(format=FormatId.SEVENZ, encrypt_names=True))


def test_reader_password_setter_reopens_the_archive(encrypted_7z: Path) -> None:
    archive = encrypted_7z
    with SevenZipReader(archive) as reader:
        reader.password = PASSWORD
        reader.check_password()
        sink = RecordingSink()
        files = [e for e in reader.entries() if e.type is EntryType.FILE]
        reader.extract(files, sink, JobContext())
        assert bytes(sink.data["tree/top.txt"]) == b"top"


# --- streaming through the sink ---------------------------------------------------------------


def test_reader_streams_into_the_sink(tmp_path: Path) -> None:
    archive = make_7z(tmp_path, "mixed")
    sink = RecordingSink()
    with SevenZipReader(archive) as reader:
        entries = reader.entries()
        reader.extract(entries, sink, JobContext())
    assert [e.name for e in sink.opened].count("tree/text.txt") == 1
    assert all(ok for _, ok in sink.closed)
    for name, data in sink.data.items():
        original = tmp_path / "src" / Path(name)
        assert bytes(data) == original.read_bytes(), name


def test_every_file_goes_through_the_writer_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zipwise.backends import sevenz

    created: list[int] = []
    real = sevenz._IndexFactory.create

    def spy(self: sevenz._IndexFactory, filename: str) -> object:
        created.append(int(filename))
        return real(self, filename)

    archive = make_7z(tmp_path)
    monkeypatch.setattr(sevenz._IndexFactory, "create", spy)
    extract_archive(archive, tmp_path / "o")
    files = [e.index for e in list_archive(archive) if e.type is EntryType.FILE]
    assert sorted(created) == sorted(files)


def test_sink_failure_closes_the_open_stream_as_failed(tmp_path: Path) -> None:
    archive = make_7z(tmp_path, "mixed")
    sink = RecordingSink(fail_after=10)
    with SevenZipReader(archive) as reader:
        files = [e for e in reader.entries() if e.type is EntryType.FILE and e.size > 100]
        with pytest.raises(BombDetected):
            reader.extract(files, sink, JobContext())
    assert sink.closed
    assert sink.closed[-1][1] is False


def test_cancel_mid_extraction_leaves_no_partial_files(tmp_path: Path) -> None:
    archive = make_7z(tmp_path, "many_small")
    first = next(e.name for e in list_archive(archive) if e.type is EntryType.FILE)
    ctx, _, _ = stepping_context(cancel_on=first)
    dest = tmp_path / "o"
    with pytest.raises(Cancelled):
        extract_archive(archive, dest, ExtractOptions(), ctx)
    assert not list(dest.rglob("*.zwpart"))


def test_extracting_a_subset_from_a_solid_archive(tmp_path: Path) -> None:
    archive = make_7z(tmp_path)
    report = extract_archive(archive, tmp_path / "o", ExtractOptions(only=["tree/d1/d2"]))
    assert report.ok
    assert sorted(snapshot(tmp_path / "o")) == [
        "tree",
        "tree/d1",
        "tree/d1/d2",
        "tree/d1/d2/d3",
        "tree/d1/d2/d3/three.bin",
        "tree/d1/d2/two.txt",
    ]
    assert not (tmp_path / "o" / "tree" / "top.txt").exists()


def test_strip_components(tmp_path: Path) -> None:
    archive = make_7z(tmp_path)
    extract_archive(archive, tmp_path / "o", ExtractOptions(strip_components=1))
    assert (tmp_path / "o" / "top.txt").read_bytes() == b"top"


def test_duplicate_names_get_distinct_outputs(tmp_path: Path) -> None:
    archive = write_raw_7z(
        tmp_path / "d.7z", [("file", "same.txt", b"first"), ("file", "same.txt", b"second")]
    )
    report = extract_archive(
        archive, tmp_path / "o", ExtractOptions(on_conflict=ConflictAction.RENAME)
    )
    assert report.ok
    contents = sorted(p.read_bytes() for p in (tmp_path / "o").iterdir())
    assert contents == [b"first", b"second"]


# --- damaged archives -------------------------------------------------------------------------


def test_truncated_archive_is_corrupt(tmp_path: Path) -> None:
    archive = make_7z(tmp_path)
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])
    with pytest.raises(CorruptArchive):
        list_archive(archive)


def test_garbage_after_the_signature_is_corrupt(tmp_path: Path) -> None:
    bad = tmp_path / "bad.7z"
    bad.write_bytes(b"7z\xbc\xaf\x27\x1c" + os.urandom(200))
    with pytest.raises(CorruptArchive):
        list_archive(bad)


def test_flipped_payload_byte_fails_the_test(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.bin").write_bytes(random_bytes(4, 20_000))
    archive = tmp_path / "f.7z"
    create_archive([src / "f.bin"], archive, OPTS)
    data = bytearray(archive.read_bytes())
    data[32 + 5000] ^= 0xFF
    archive.write_bytes(bytes(data))
    try:
        report = verify_archive(archive)
    except CorruptArchive:
        return
    assert not report.ok
    assert report.issues[0].entry == "f.bin"


def test_corrupt_data_fails_extraction_without_leaving_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.bin").write_bytes(random_bytes(4, 20_000))
    archive = tmp_path / "f.7z"
    create_archive([src / "f.bin"], archive, OPTS)
    data = bytearray(archive.read_bytes())
    data[32 + 5000] ^= 0xFF
    archive.write_bytes(bytes(data))
    with pytest.raises(CorruptArchive):
        extract_archive(archive, tmp_path / "o")
    assert not list((tmp_path / "o").rglob("*"))


def test_missing_file_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        SevenZipReader(tmp_path / "nope.7z")


def test_in_memory_stream_is_not_used_for_large_entries(tmp_path: Path) -> None:
    """The sink gets many writes, so nothing is gathered into one buffer."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(random_bytes(9, 3_000_000))
    archive = tmp_path / "big.7z"
    create_archive([src / "big.bin"], archive, OPTS)
    writes: list[int] = []

    class Counting(RecordingSink):
        def open(self, entry: ArchiveEntry) -> BinaryIO | None:
            stream = super().open(entry)
            if stream is None:
                return None
            inner = stream

            class Wrapper(io.RawIOBase):
                def write(self, data: bytes) -> int:  # type: ignore[override]
                    writes.append(len(data))
                    return inner.write(data)

            return Wrapper()  # type: ignore[return-value]

    sink = Counting()
    with SevenZipReader(archive) as reader:
        reader.extract(reader.entries(), sink, JobContext())
    assert bytes(sink.data["big.bin"]) == (src / "big.bin").read_bytes()
    assert sum(writes) == 3_000_000
