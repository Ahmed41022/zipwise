from __future__ import annotations

import io
import zipfile
import zlib
from pathlib import Path

import pytest

from helpers.builders import (
    RawEntry,
    dir_entry,
    file_entry,
    symlink_entry,
    unicode_path_extra,
    write_raw_zip,
    zipcrypto_encrypt,
)
from helpers.trees import SHAPES, make_tree
from zipwise.backends.zip import ZipReader, ZipWriter
from zipwise.errors import (
    CorruptArchive,
    PasswordRequired,
    UnsupportedFormat,
    UsageError,
    WrongPassword,
)
from zipwise.jobs import JobContext
from zipwise.model import CreateOptions, EntryType, ExtractOptions, FormatId
from zipwise.ops import archive_info, create_archive, extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive

PASSWORD = "pässwörd"


@pytest.fixture
def aes_zip(tmp_path: Path) -> Path:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["nested"])
    out = tmp_path / "aes.zip"
    create_archive([tree], out, CreateOptions(password=PASSWORD))
    return out


def legacy_entry(name: str, data: bytes, password: bytes) -> RawEntry:
    crc = zlib.crc32(data)
    encrypted = zipcrypto_encrypt(data, password, crc)
    return RawEntry(
        name, data, method=0, flags=0x801, compressed=encrypted, declared_size=len(data)
    )


# --- passwords ------------------------------------------------------------------------------


def test_aes_archive_lists_without_a_password(aes_zip: Path) -> None:
    entries = list_archive(aes_zip)
    assert [e.name for e in entries if e.type is EntryType.FILE][0].startswith("t/")
    assert any(e.encrypted for e in entries)
    assert not any(e.encrypted for e in entries if e.type is EntryType.DIR)


def test_info_reports_encryption(aes_zip: Path) -> None:
    assert archive_info(aes_zip).encrypted is True


@pytest.mark.parametrize("operation", ["extract", "test"])
def test_missing_password_is_reported_before_any_work(
    tmp_path: Path, aes_zip: Path, operation: str
) -> None:
    dest = tmp_path / "dest"
    with pytest.raises(PasswordRequired) as info:
        if operation == "extract":
            extract_archive(aes_zip, dest)
        else:
            verify_archive(aes_zip)
    assert info.value.exit_code == 6
    assert not dest.exists()


@pytest.mark.parametrize("operation", ["extract", "test"])
@pytest.mark.parametrize("wrong", ["nope", PASSWORD + "x", PASSWORD.upper()])
def test_wrong_password_is_reported(
    tmp_path: Path, aes_zip: Path, operation: str, wrong: str
) -> None:
    dest = tmp_path / "dest"
    with pytest.raises(WrongPassword) as info:
        if operation == "extract":
            extract_archive(aes_zip, dest, ExtractOptions(password=wrong))
        else:
            verify_archive(aes_zip, password=wrong)
    assert info.value.exit_code == 6
    assert not dest.exists() or not [p for p in dest.rglob("*") if p.is_file()]


def test_password_callback_is_asked_once(tmp_path: Path, aes_zip: Path) -> None:
    asked = 0

    def ask() -> str:
        nonlocal asked
        asked += 1
        return PASSWORD

    report = extract_archive(aes_zip, tmp_path / "dest", ctx=JobContext(ask_password=ask))
    assert report.ok and asked == 1
    assert (tmp_path / "dest" / "t" / "top.txt").read_bytes() == b"top"


def test_password_callback_returning_nothing_is_password_required(
    tmp_path: Path, aes_zip: Path
) -> None:
    with pytest.raises(PasswordRequired):
        extract_archive(aes_zip, tmp_path / "d", ctx=JobContext(ask_password=lambda: None))


def test_password_callback_with_a_wrong_answer_is_wrong_password(
    tmp_path: Path, aes_zip: Path
) -> None:
    with pytest.raises(WrongPassword):
        extract_archive(aes_zip, tmp_path / "d", ctx=JobContext(ask_password=lambda: "bad"))


def test_archive_opens_with_the_pyzipper_reference_implementation(aes_zip: Path) -> None:
    import pyzipper

    with pyzipper.AESZipFile(aes_zip) as zf:
        zf.setpassword(PASSWORD.encode())
        assert zf.read("t/top.txt") == b"top"
        assert zf.testzip() is None


def test_encryption_is_winzip_aes_256(aes_zip: Path) -> None:
    import pyzipper

    with pyzipper.AESZipFile(aes_zip) as zf:
        info = zf.getinfo("t/top.txt")
        assert info.wz_aes_strength == 3  # 256-bit


def test_legacy_zipcrypto_can_be_read(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "old.zip", [legacy_entry("old.txt", b"legacy data", b"secret")])
    report = extract_archive(out, tmp_path / "dest", ExtractOptions(password="secret"))
    assert report.ok
    assert (tmp_path / "dest" / "old.txt").read_bytes() == b"legacy data"


def test_legacy_zipcrypto_wrong_and_missing_password(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "old.zip", [legacy_entry("old.txt", b"legacy data", b"secret")])
    with pytest.raises(WrongPassword):
        extract_archive(out, tmp_path / "d1", ExtractOptions(password="other"))
    with pytest.raises(PasswordRequired):
        extract_archive(out, tmp_path / "d2")


def test_archive_where_only_some_entries_are_encrypted(tmp_path: Path) -> None:
    out = write_raw_zip(
        tmp_path / "mixed.zip",
        [file_entry("plain.txt", b"open"), legacy_entry("locked.txt", b"closed", b"pw")],
    )
    dest = tmp_path / "dest"
    with pytest.raises(PasswordRequired):
        extract_archive(out, dest)
    extract_archive(out, dest, ExtractOptions(password="pw"))
    assert (dest / "plain.txt").read_bytes() == b"open"
    assert (dest / "locked.txt").read_bytes() == b"closed"


def test_only_the_open_part_of_a_mixed_archive_needs_no_password(tmp_path: Path) -> None:
    out = write_raw_zip(
        tmp_path / "mixed.zip",
        [file_entry("plain.txt", b"open"), legacy_entry("locked.txt", b"closed", b"pw")],
    )
    report = extract_archive(out, tmp_path / "dest", ExtractOptions(only=["plain.txt"]))
    assert report.ok
    assert [p.name for p in (tmp_path / "dest").iterdir()] == ["plain.txt"]


# --- damaged and unusual archives -----------------------------------------------------------


def good_zip(tmp_path: Path) -> Path:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["nested"])
    out = tmp_path / "good.zip"
    create_archive([tree], out)
    return out


@pytest.mark.parametrize("fraction", [0.1, 0.5, 0.9, 0.99])
def test_truncated_zip_is_corrupt(tmp_path: Path, fraction: float) -> None:
    data = good_zip(tmp_path).read_bytes()
    broken = tmp_path / "broken.zip"
    broken.write_bytes(data[: int(len(data) * fraction)])
    for call in (list_archive, archive_info, verify_archive):
        with pytest.raises(CorruptArchive) as info:
            call(broken)
        assert info.value.exit_code == 4
    with pytest.raises(CorruptArchive):
        extract_archive(broken, tmp_path / "dest")


def test_magic_bytes_followed_by_noise_are_corrupt(tmp_path: Path) -> None:
    bogus = tmp_path / "noise.zip"
    bogus.write_bytes(b"PK\x03\x04" + bytes(range(256)) * 4)
    with pytest.raises(CorruptArchive):
        list_archive(bogus)


def test_damaged_central_directory_is_corrupt(tmp_path: Path) -> None:
    data = bytearray(good_zip(tmp_path).read_bytes())
    start = data.rindex(b"PK\x01\x02")
    data[start : start + 4] = b"XXXX"
    broken = tmp_path / "cd.zip"
    broken.write_bytes(bytes(data))
    with pytest.raises(CorruptArchive):
        list_archive(broken)


def test_flipped_data_byte_is_detected_by_the_checksum(tmp_path: Path) -> None:
    out = tmp_path / "t.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("a.txt", b"payload-" * 50)
    raw = bytearray(out.read_bytes())
    raw[raw.index(b"payload-") + 20] ^= 0x01
    out.write_bytes(bytes(raw))
    with pytest.raises(CorruptArchive):
        extract_archive(out, tmp_path / "dest")
    assert not [p for p in (tmp_path / "dest").rglob("*") if p.is_file()]


def test_empty_zip_has_no_entries_and_extracts_cleanly(tmp_path: Path) -> None:
    out = tmp_path / "empty.zip"
    with zipfile.ZipFile(out, "w"):
        pass
    assert list_archive(out) == []
    assert verify_archive(out).ok
    assert extract_archive(out, tmp_path / "dest").ok


def test_unsupported_compression_method_is_reported(tmp_path: Path) -> None:
    out = write_raw_zip(
        tmp_path / "d64.zip", [RawEntry("x.bin", b"data", method=9, compressed=b"junk")]
    )
    with pytest.raises(UnsupportedFormat):
        extract_archive(out, tmp_path / "dest")
    report = verify_archive(out)
    assert not report.ok
    assert report.issues[0].code == "unsupported-format"


def test_missing_file_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        ZipReader(tmp_path / "nope.zip")


@pytest.mark.parametrize(
    "method",
    [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA],
    ids=["stored", "deflated", "bzip2", "lzma"],
)
def test_archives_written_by_other_tools_extract(tmp_path: Path, method: int) -> None:
    out = tmp_path / "foreign.zip"
    with zipfile.ZipFile(out, "w", method) as zf:
        zf.writestr("café/ملف.txt", b"foreign" * 100)
        zf.writestr("plain.txt", b"ascii")
    extract_archive(out, tmp_path / "dest")
    assert (tmp_path / "dest" / "café" / "ملف.txt").read_bytes() == b"foreign" * 100
    assert (tmp_path / "dest" / "plain.txt").read_bytes() == b"ascii"


def test_duplicate_names_are_listed_separately(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "d.zip", [file_entry("a", b"1"), file_entry("a", b"2")])
    assert [(e.name, e.index) for e in list_archive(out)] == [("a", 0), ("a", 1)]


def test_many_entries_list_quickly(tmp_path: Path) -> None:
    out = write_raw_zip(
        tmp_path / "n.zip", [file_entry(f"f{i}", b"", method=0) for i in range(3000)]
    )
    assert len(list_archive(out)) == 3000


# --- entry metadata -------------------------------------------------------------------------


def test_dos_created_archive_uses_the_directory_attribute(tmp_path: Path) -> None:
    out = write_raw_zip(
        tmp_path / "dos.zip",
        [
            RawEntry("folder", b"", method=0, external_attr=0x10, create_system=0),
            RawEntry("file.txt", b"x", external_attr=0x20, create_system=0),
        ],
    )
    entries = {e.name: e for e in list_archive(out)}
    assert entries["folder"].type is EntryType.DIR
    assert entries["file.txt"].type is EntryType.FILE
    assert entries["file.txt"].mode is None


def test_symlink_entry_exposes_its_target(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "s.zip", [symlink_entry("l", "some/target")])
    entry = list_archive(out)[0]
    assert entry.type is EntryType.SYMLINK
    assert entry.link_target == "some/target"


def test_entry_fields_come_from_the_headers(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "f.zip", [file_entry("a.txt", b"hello hello hello")])
    entry = list_archive(out)[0]
    assert entry.size == 17
    assert entry.crc == zlib.crc32(b"hello hello hello")
    assert entry.compressed_size is not None and entry.compressed_size > 0
    assert entry.mode == 0o644
    assert entry.mtime is not None and entry.mtime.year == 2024
    assert entry.encrypted is False


def test_directory_names_lose_their_trailing_slash(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "d.zip", [dir_entry("a/b/")])
    assert list_archive(out)[0].name == "a/b"


def test_zip_comment_is_reported(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "c.zip", [file_entry("a")], comment=b"hello comment")
    assert archive_info(out).comment == "hello comment"


# --- names and encodings --------------------------------------------------------------------


def test_flagged_names_keep_no_raw_bytes(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "f.zip", [file_entry("café.txt")])
    entry = list_archive(out)[0]
    assert entry.name == "café.txt"
    assert entry.raw_name is None


def test_unflagged_name_keeps_raw_bytes_and_decodes_as_cp437(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "u.zip", [file_entry(b"caf\x82.txt")])
    entry = list_archive(out)[0]
    assert entry.raw_name == b"caf\x82.txt"
    assert entry.name == "café.txt"


def test_unflagged_ascii_names_still_keep_raw_bytes(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "u.zip", [file_entry(b"plain.txt")])
    assert list_archive(out)[0].raw_name == b"plain.txt"


def test_unflagged_utf8_bytes_are_recognised(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "u.zip", [file_entry("café.txt".encode())])
    entry = list_archive(out)[0]
    assert entry.name == "café.txt"
    assert entry.raw_name == "café.txt".encode()


ARABIC = "ملف عربي.txt"


def test_name_encoding_override_decodes_legacy_names(tmp_path: Path) -> None:
    raw = ARABIC.encode("cp1256")
    out = write_raw_zip(tmp_path / "ar.zip", [file_entry(raw)])
    assert list_archive(out)[0].name != ARABIC  # the default guess cannot know
    assert list_archive(out, name_encoding="cp1256")[0].name == ARABIC
    extract_archive(out, tmp_path / "dest", ExtractOptions(name_encoding="cp1256"))
    assert (tmp_path / "dest" / ARABIC).exists()


def test_override_never_touches_flagged_names(tmp_path: Path) -> None:
    out = write_raw_zip(
        tmp_path / "m.zip", [file_entry("café.txt"), file_entry(ARABIC.encode("cp1256"))]
    )
    names = [e.name for e in list_archive(out, name_encoding="cp1256")]
    assert names == ["café.txt", ARABIC]


def test_unicode_path_extra_wins_over_any_decoding(tmp_path: Path) -> None:
    raw = b"caf\x82.txt"
    extra = unicode_path_extra(raw, "café-unicode.txt")
    out = write_raw_zip(tmp_path / "x.zip", [RawEntry(raw, b"x", flags=0, extra=extra)])
    assert list_archive(out, name_encoding="cp1256")[0].name == "café-unicode.txt"


def test_unicode_path_extra_with_wrong_checksum_is_ignored(tmp_path: Path) -> None:
    raw = b"caf\x82.txt"
    extra = unicode_path_extra(b"other bytes", "bogus.txt")
    out = write_raw_zip(tmp_path / "x.zip", [RawEntry(raw, b"x", flags=0, extra=extra)])
    assert list_archive(out)[0].name == "café.txt"


def test_unknown_encoding_is_a_usage_error(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "x.zip", [file_entry(b"a")])
    with pytest.raises(UsageError):
        list_archive(out, name_encoding="klingon-9")


def test_info_reports_the_guessed_encoding(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "x.zip", [file_entry(b"caf\x82.txt")])
    info = archive_info(out)
    assert info.name_encoding == "cp437"
    assert 0 < (info.encoding_confidence or 0) <= 1


# --- the writer on its own ------------------------------------------------------------------


def test_writer_accepts_a_file_object(tmp_path: Path) -> None:
    src = tmp_path / "f.txt"
    src.write_bytes(b"abc" * 100)
    buf = io.BytesIO()
    writer = ZipWriter(buf, CreateOptions())
    writer.add_dir("d", 1_700_000_000.0, 0o755)
    writer.add_file(src, "d/f.txt")
    writer.add_symlink("d/l", "f.txt", 1_700_000_000.0)
    writer.close()
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        assert zf.namelist() == ["d/", "d/f.txt", "d/l"]
        assert zf.read("d/f.txt") == b"abc" * 100
        assert zf.read("d/l") == b"f.txt"
        assert zf.testzip() is None


def test_writer_accepts_a_path(tmp_path: Path) -> None:
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    out = tmp_path / "w.zip"
    with ZipWriter(out, CreateOptions()) as writer:
        writer.add_file(src, "f.txt")
    assert zipfile.ZipFile(out).namelist() == ["f.txt"]


def test_aborted_writer_does_not_finalise_the_archive() -> None:
    buf = io.BytesIO()
    writer = ZipWriter(buf, CreateOptions())
    writer.add_dir("d", 0.0, None)
    writer.abort()
    assert not zipfile.is_zipfile(io.BytesIO(buf.getvalue()))


def test_writer_context_manager_aborts_on_error(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with pytest.raises(RuntimeError), ZipWriter(buf, CreateOptions()) as writer:
        writer.add_dir("d", 0.0, None)
        raise RuntimeError("boom")
    assert not zipfile.is_zipfile(io.BytesIO(buf.getvalue()))


def test_writer_reports_format_and_missing_source(tmp_path: Path) -> None:
    from zipwise.errors import IOFailure

    assert ZipWriter.format is FormatId.ZIP
    writer = ZipWriter(io.BytesIO(), CreateOptions())
    with pytest.raises(IOFailure):
        writer.add_file(tmp_path / "missing", "m")
    writer.abort()


def test_reader_context_manager_closes_the_file(tmp_path: Path) -> None:
    out = write_raw_zip(tmp_path / "x.zip", [file_entry("a")])
    with ZipReader(out) as reader:
        assert reader.entries()
    assert reader._zf.fp is None
