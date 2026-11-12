"""Regression tests found by running real 7-Zip and RAR tools against Zipwise: methods Zipwise
cannot read are named in the error, and encrypted RAR file names ask for the password."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
import rarfile
from py7zr.exceptions import UnsupportedCompressionMethodError

from helpers.builders import RawEntry, write_raw_7z, write_raw_zip, write_stored_rar4
from zipwise.backends import rar as rar_backend
from zipwise.backends.rar import RarReader
from zipwise.backends.sevenz import SevenZipReader
from zipwise.errors import (
    CorruptArchive,
    ExitCode,
    PasswordRequired,
    UnsupportedFormat,
    WrongPassword,
)
from zipwise.jobs import JobContext
from zipwise.model import EntryType
from zipwise.ops import extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive

# --- ZIP: methods the standard library cannot decode -------------------------------------------


@pytest.fixture
def deflate64_zip(tmp_path: Path) -> Path:
    return write_raw_zip(
        tmp_path / "d64.zip",
        [
            RawEntry("dir/", b"", method=0, external_attr=(0o040755 << 16) | 0x10),
            RawEntry("dir/a.bin", b"payload", method=9, compressed=b"\x00\x01\x02"),
            RawEntry("plain.txt", b"fine", method=0),
        ],
    )


def test_zip_deflate64_is_listed(deflate64_zip: Path) -> None:
    entries = list_archive(deflate64_zip)
    assert [e.name for e in entries] == ["dir", "dir/a.bin", "plain.txt"]


def test_zip_deflate64_is_named_when_extracting(deflate64_zip: Path, tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFormat) as caught:
        extract_archive(deflate64_zip, tmp_path / "out")
    assert caught.value.exit_code == ExitCode.UNSUPPORTED
    message = "'dir/a.bin' uses Deflate64 compression, which Zipwise cannot read yet"
    assert message in str(caught.value)


def test_zip_deflate64_is_named_by_test(deflate64_zip: Path) -> None:
    report = verify_archive(deflate64_zip)
    assert not report.ok
    assert [(i.code, i.entry) for i in report.issues] == [("unsupported-format", "dir/a.bin")]
    assert "Deflate64 compression" in report.issues[0].message


def test_zip_unknown_method_number_is_shown(tmp_path: Path) -> None:
    archive = write_raw_zip(tmp_path / "m.zip", [RawEntry("a", b"x", method=77, compressed=b"x")])
    issue = verify_archive(archive).issues[0]
    assert "uses method 77 compression" in issue.message


# --- 7z: methods py7zr cannot decode -----------------------------------------------------------


class _NullSink:
    def open(self, entry: Any) -> None:
        return None

    def close(self, entry: Any, stream: Any, ok: bool) -> None:
        return None


@pytest.mark.parametrize(
    ("method_id", "name"),
    [(bytes.fromhex("0303011B"), "BCJ2"), (b"\n", "ARM64"), (b"\x7f\x01", "method 0x7F01")],
)
def test_7z_unsupported_method_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method_id: bytes, name: str
) -> None:
    archive = write_raw_7z(tmp_path / "a.7z", [("file", "a.txt", b"data")])

    def refuse(self: Any, indexes: Any, make: Any) -> None:
        raise UnsupportedCompressionMethodError(method_id, "unsupported")

    monkeypatch.setattr(SevenZipReader, "_run", refuse)
    with SevenZipReader(archive) as reader:
        with pytest.raises(UnsupportedFormat) as caught:
            reader.extract([reader.entries()[0]], _NullSink(), JobContext())  # type: ignore[arg-type]
        assert f"uses {name} compression, which Zipwise cannot read yet" in str(caught.value)
        assert caught.value.exit_code == ExitCode.UNSUPPORTED
        issues = reader.test(JobContext())
    assert [(i.code, i.entry) for i in issues] == [("unsupported-format", None)]
    assert name in issues[0].message


# --- RAR: what the extraction tool says -------------------------------------------------------


@pytest.fixture
def rar_reader(tmp_path: Path) -> RarReader:
    return RarReader(write_stored_rar4(tmp_path / "s.rar", {"a.txt": b"hello"}))


@pytest.mark.parametrize(
    "message", ["Failed the read enough data: req=5 got=40", "Corrupt file - CRC check failed: a"]
)
def test_rar_tool_that_gives_up_is_not_called_damage(
    rar_reader: RarReader, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    monkeypatch.setattr(rar_backend, "_current_tool", lambda: "7z")
    entry = rar_reader.entries()[0]
    error = rar_reader._translate(rarfile.BadRarFile(message), entry)
    assert isinstance(error, UnsupportedFormat)
    assert error.exit_code == ExitCode.UNSUPPORTED
    assert "7z could not extract 'a.txt'" in error.message
    assert "unrar" in (error.hint or "")


def test_rar_tool_that_gives_up_with_a_password_may_be_a_wrong_password(
    rar_reader: RarReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rar_backend, "_current_tool", lambda: "7z")
    rar_reader.password = "pw"
    entry = dataclasses.replace(rar_reader.entries()[0], encrypted=True)
    error = rar_reader._translate(rarfile.BadRarFile("Failed the read enough data: x"), entry)
    assert isinstance(error, WrongPassword)
    assert "7z cannot read this archive" in error.message


def test_unrar_short_read_still_means_damage(
    rar_reader: RarReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rar_backend, "_current_tool", lambda: "unrar")
    entry = rar_reader.entries()[0]
    error = rar_reader._translate(rarfile.BadRarFile("Failed the read enough data: x"), entry)
    assert isinstance(error, CorruptArchive)


def test_current_tool_reads_rarfiles_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    class Setup:
        setup = {"open_cmd": ("SEVENZIP_TOOL", "e")}

    monkeypatch.setattr(rarfile, "CURRENT_SETUP", Setup(), raising=False)
    monkeypatch.setattr(rarfile, "SEVENZIP_TOOL", "/usr/bin/7z")
    assert rar_backend._current_tool() == "7z"
    monkeypatch.setattr(rarfile, "CURRENT_SETUP", None, raising=False)
    assert rar_backend._current_tool() is None


# --- RAR: encrypted file names -----------------------------------------------------------------


class _Info:
    filename = "secret.txt"
    file_size = 3
    compress_size = 3
    mtime = None
    date_time = (2024, 1, 1, 0, 0, 0)
    host_os = 3
    mode = 0o644
    CRC = 0
    file_redir = None

    def is_dir(self) -> bool:
        return False

    def is_symlink(self) -> bool:
        return False

    def needs_password(self) -> bool:
        return True


class _HeaderEncryptedRar:
    """Stands in for rarfile.RarFile on an archive with encrypted headers: no entries are
    visible until the right password is set."""

    def __init__(self) -> None:
        self.unlocked = False

    def needs_password(self) -> bool:
        return True

    def infolist(self) -> list[_Info]:
        return [_Info()] if self.unlocked else []

    def setpassword(self, pwd: str) -> None:
        if pwd != "right":
            raise rarfile.RarWrongPassword()
        self.unlocked = True

    def close(self) -> None:
        return None


@pytest.fixture
def header_encrypted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(rarfile, "RarFile", lambda path, errors="strict": _HeaderEncryptedRar())
    path = tmp_path / "enc.rar"
    path.write_bytes(b"Rar!\x1a\x07\x01\x00")
    return path


def test_rar_with_encrypted_names_asks_for_the_password(header_encrypted: Path) -> None:
    with pytest.raises(PasswordRequired) as caught:
        RarReader(header_encrypted)
    assert caught.value.exit_code == ExitCode.PASSWORD


def test_rar_with_encrypted_names_lists_with_the_password(header_encrypted: Path) -> None:
    with RarReader(header_encrypted, password="right") as reader:
        entries = reader.entries()
    assert [(e.name, e.type, e.encrypted) for e in entries] == [
        ("secret.txt", EntryType.FILE, True)
    ]


def test_rar_with_encrypted_names_rejects_a_wrong_password(header_encrypted: Path) -> None:
    with pytest.raises(WrongPassword):
        RarReader(header_encrypted, password="wrong")


def test_rar_without_the_crypto_package_is_reported(
    header_encrypted: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_crypto(self: _HeaderEncryptedRar, pwd: str) -> None:
        raise rarfile.NoCrypto("no crypto")

    monkeypatch.setattr(_HeaderEncryptedRar, "setpassword", no_crypto)
    with pytest.raises(rar_backend.BackendUnavailable) as caught:
        RarReader(header_encrypted, password="right")
    assert "cryptography" in (caught.value.hint or "")


# --- exit codes through the command line --------------------------------------------------------


def test_cli_list_works_and_extract_exits_3_for_deflate64(
    deflate64_zip: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from zipwise.cli.main import main

    assert main(["list", str(deflate64_zip)]) == 0
    capsys.readouterr()
    assert (
        main(["extract", str(deflate64_zip), "-o", str(tmp_path / "out")]) == ExitCode.UNSUPPORTED
    )
    err = capsys.readouterr().err
    assert "uses Deflate64 compression, which Zipwise cannot read yet" in err
    code = main(["test", str(deflate64_zip)])
    captured = capsys.readouterr()
    assert code == ExitCode.UNSUPPORTED
    assert "Deflate64" in captured.err + captured.out
