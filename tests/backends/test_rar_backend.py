from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import rarfile

from helpers.builders import write_stored_rar4
from zipwise.backends import rar as rar_backend
from zipwise.backends.rar import INSTALL_HINT, RarReader, find_tool, require_tool
from zipwise.detect import detect
from zipwise.errors import (
    BackendUnavailable,
    CorruptArchive,
    ExitCode,
    PasswordRequired,
    UnsupportedFormat,
    WrongPassword,
)
from zipwise.model import ArchiveEntry, CreateOptions, EntryType, FormatId
from zipwise.ops import archive_info, create_archive, extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive
from zipwise.registry import capability, get_reader, get_writer_class

FILES: dict[str, bytes | None] = {
    "a.txt": b"hello",
    "d/": None,
    "d/b.txt": b"world!!",
}

REAL_TOOL = find_tool()
needs_unrar = pytest.mark.skipif(REAL_TOOL is None, reason="no unrar-compatible tool on PATH")


@pytest.fixture
def rar_file(tmp_path: Path) -> Path:
    return write_stored_rar4(tmp_path / "stored.rar", FILES)


@pytest.fixture
def no_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def fake_tool(directory: Path, name: str) -> None:
    directory.mkdir(exist_ok=True)
    exe = directory / (name + (".exe" if os.name == "nt" else ""))
    exe.write_bytes(b"")
    exe.chmod(0o755)


# --- listing without any tool -----------------------------------------------------------------


def test_stored_archive_is_detected_as_rar(rar_file: Path) -> None:
    assert detect(rar_file).format is FormatId.RAR
    with get_reader(rar_file) as reader:
        assert isinstance(reader, RarReader)


def test_listing_works_without_an_extraction_tool(rar_file: Path, no_tools: None) -> None:
    entries = {e.name: e for e in list_archive(rar_file)}
    assert sorted(entries) == ["a.txt", "d", "d/b.txt"]
    assert entries["d"].type is EntryType.DIR
    assert entries["a.txt"].type is EntryType.FILE
    assert entries["a.txt"].size == 5
    assert entries["d/b.txt"].size == 7
    assert entries["a.txt"].compressed_size == 5
    assert entries["a.txt"].encrypted is False
    assert entries["a.txt"].crc == 907060870
    assert entries["a.txt"].mtime is not None
    assert (entries["a.txt"].mtime.year, entries["a.txt"].mtime.month) == (2024, 3)
    assert [e.index for e in list_archive(rar_file)] == [0, 1, 2]


def test_info_works_without_an_extraction_tool(rar_file: Path, no_tools: None) -> None:
    info = archive_info(rar_file)
    assert info.format is FormatId.RAR
    assert info.entries_count == 3
    assert info.total_size == 12
    assert info.encrypted is False


# --- tool lookup ------------------------------------------------------------------------------


def test_no_tool_on_an_empty_path(no_tools: None) -> None:
    assert find_tool() is None
    with pytest.raises(BackendUnavailable) as caught:
        require_tool()
    assert caught.value.exit_code == ExitCode.UNSUPPORTED
    assert caught.value.code == "backend-unavailable"


def test_hint_names_the_install_command_for_both_systems(no_tools: None) -> None:
    with pytest.raises(BackendUnavailable) as caught:
        require_tool()
    hint = caught.value.hint or ""
    assert "winget install RARLab.WinRAR" in hint
    assert "7zip" in hint
    assert "sudo apt install unrar" in hint
    assert hint == INSTALL_HINT
    assert "unrar" in caught.value.message


@pytest.mark.parametrize("operation", ["extract", "test"])
def test_extract_and_test_need_a_tool(
    rar_file: Path, tmp_path: Path, no_tools: None, operation: str
) -> None:
    with pytest.raises(BackendUnavailable) as caught:
        if operation == "extract":
            extract_archive(rar_file, tmp_path / "out")
        else:
            verify_archive(rar_file)
    assert "winget" in (caught.value.hint or "")
    assert not (tmp_path / "out" / "a.txt").exists()


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        (["unrar"], "unrar"),
        (["unar", "unrar"], "unrar"),
        (["bsdtar", "unar"], "unar"),
        (["bsdtar"], "bsdtar"),
        (["7z", "bsdtar"], "7z"),
        (["7zz"], "7zz"),
    ],
)
def test_find_tool_prefers_unrar_then_unar_then_7z(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, present: list[str], expected: str
) -> None:
    bin_dir = tmp_path / "bin"
    for name in present:
        fake_tool(bin_dir, name)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert find_tool() == expected


def test_a_tool_that_cannot_run_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_tool(tmp_path / "bin", "unrar")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(BackendUnavailable) as caught:
        require_tool()
    assert "could not be run" in caught.value.message
    assert caught.value.hint == INSTALL_HINT


def test_registry_row_declares_the_tool_and_no_writer() -> None:
    cap = capability(FormatId.RAR)
    assert cap.writer is None
    assert cap.needs_tool == "unrar"


# --- creating is not possible -----------------------------------------------------------------


def test_creating_rar_is_unsupported(tmp_path: Path) -> None:
    src = tmp_path / "f.txt"
    src.write_bytes(b"x")
    with pytest.raises(UnsupportedFormat) as caught:
        create_archive([src], tmp_path / "x.rar", CreateOptions(format=FormatId.RAR))
    assert "RAR archives can be read but not created" in caught.value.message
    assert caught.value.exit_code == ExitCode.UNSUPPORTED
    assert not (tmp_path / "x.rar").exists()


def test_writer_class_lookup_is_unsupported() -> None:
    with pytest.raises(UnsupportedFormat, match="read but not created"):
        get_writer_class(FormatId.RAR)


# --- damaged files ----------------------------------------------------------------------------


def test_garbage_after_the_signature_is_corrupt(tmp_path: Path) -> None:
    bad = tmp_path / "bad.rar"
    bad.write_bytes(b"Rar!\x1a\x07\x00" + os.urandom(64))
    with pytest.raises(CorruptArchive):
        list_archive(bad)


def test_truncated_header_is_corrupt(rar_file: Path) -> None:
    raw = rar_file.read_bytes()
    rar_file.write_bytes(raw[:30])
    with pytest.raises(CorruptArchive):
        list_archive(rar_file)


def test_a_damaged_header_checksum_is_corrupt(rar_file: Path) -> None:
    raw = bytearray(rar_file.read_bytes())
    raw[7 + 13 + 12] ^= 0xFF  # inside the first file header
    rar_file.write_bytes(bytes(raw))
    with pytest.raises(CorruptArchive):
        list_archive(rar_file)


# --- translation of rarfile errors ------------------------------------------------------------


@pytest.fixture
def reader_and_entry(rar_file: Path) -> tuple[RarReader, ArchiveEntry]:
    reader = RarReader(rar_file)
    return reader, reader.entries()[0]


def test_translate_wrong_password(reader_and_entry: tuple[RarReader, ArchiveEntry]) -> None:
    reader, entry = reader_and_entry
    assert isinstance(reader._translate(rarfile.RarWrongPassword("x"), entry), WrongPassword)


def test_translate_password_required(reader_and_entry: tuple[RarReader, ArchiveEntry]) -> None:
    reader, entry = reader_and_entry
    assert isinstance(reader._translate(rarfile.PasswordRequired("x"), entry), PasswordRequired)


def test_translate_missing_tool(reader_and_entry: tuple[RarReader, ArchiveEntry]) -> None:
    reader, entry = reader_and_entry
    error = reader._translate(rarfile.RarCannotExec("x"), entry)
    assert isinstance(error, BackendUnavailable)
    assert error.hint == INSTALL_HINT


def test_translate_crc_error_is_corrupt(reader_and_entry: tuple[RarReader, ArchiveEntry]) -> None:
    reader, entry = reader_and_entry
    assert isinstance(reader._translate(rarfile.RarCRCError("x"), entry), CorruptArchive)
    assert isinstance(reader._translate(rarfile.BadRarFile("x"), entry), CorruptArchive)


def test_translate_bad_file_with_a_password_on_an_encrypted_entry(
    reader_and_entry: tuple[RarReader, ArchiveEntry],
) -> None:
    import dataclasses

    reader, entry = reader_and_entry
    reader.password = "guess"
    encrypted = dataclasses.replace(entry, encrypted=True)
    assert isinstance(reader._translate(rarfile.BadRarFile("crc"), encrypted), WrongPassword)


def test_translate_other_rarfile_errors_are_unsupported(
    reader_and_entry: tuple[RarReader, ArchiveEntry],
) -> None:
    reader, entry = reader_and_entry
    assert isinstance(reader._translate(rarfile.NoRarEntry("x"), entry), UnsupportedFormat)


def test_translate_passes_zipwise_errors_through(
    reader_and_entry: tuple[RarReader, ArchiveEntry],
) -> None:
    reader, entry = reader_and_entry
    original = CorruptArchive("kept")
    assert reader._translate(original, entry) is original


def test_translate_reraises_unrelated_exceptions(
    reader_and_entry: tuple[RarReader, ArchiveEntry],
) -> None:
    reader, entry = reader_and_entry
    with pytest.raises(KeyError):
        reader._translate(KeyError("bug"), entry)


def test_tool_flags_follow_what_is_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_setup(**kwargs: object) -> None:
        seen.update(kwargs)

    fake_tool(tmp_path / "bin", "unar")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(rar_backend.rarfile, "tool_setup", fake_setup)
    assert require_tool() == "unar"
    assert seen["unar"] is True
    assert seen["unrar"] is False
    assert seen["bsdtar"] is False
    assert seen["force"] is True


# --- with a real tool (skipped when none is installed) ----------------------------------------


@needs_unrar
@pytest.mark.needs_unrar
def test_extract_a_stored_archive_with_a_real_tool(rar_file: Path, tmp_path: Path) -> None:
    report = extract_archive(rar_file, tmp_path / "out")
    assert report.ok
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"hello"
    assert (tmp_path / "out" / "d" / "b.txt").read_bytes() == b"world!!"
    assert (tmp_path / "out" / "d").is_dir()


@needs_unrar
@pytest.mark.needs_unrar
def test_test_a_stored_archive_with_a_real_tool(rar_file: Path) -> None:
    assert verify_archive(rar_file).ok


@needs_unrar
@pytest.mark.needs_unrar
def test_damaged_data_is_reported_with_a_real_tool(rar_file: Path, tmp_path: Path) -> None:
    raw = bytearray(rar_file.read_bytes())
    index = raw.index(b"hello")
    raw[index] ^= 0xFF
    rar_file.write_bytes(bytes(raw))
    try:
        report = verify_archive(rar_file)
    except CorruptArchive:
        return
    assert not report.ok


@needs_unrar
@pytest.mark.needs_unrar
def test_modes_are_not_invented_for_windows_archives(rar_file: Path) -> None:
    entries = list_archive(rar_file)
    assert all(e.mode is None for e in entries)
    assert stat.S_IMODE(0o644) == 0o644
