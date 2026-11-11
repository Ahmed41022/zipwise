"""A damaged entry: skipped and reported for ZIP, an abort that keeps finished files for streams."""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from helpers.builders import (
    compress_bytes,
    file_entry,
    tar_bytes,
    tar_file,
    write_raw_zip,
)
from helpers.names import extracted_files
from helpers.trees import random_bytes
from zipwise.errors import CorruptArchive, ExitCode, WrongPassword
from zipwise.model import CORRUPT_ENTRY, CreateOptions, ExtractOptions, FormatId, Issue
from zipwise.model import OperationReport as Report
from zipwise.ops import create_archive, extract_archive
from zipwise.ops import test_archive as verify_archive

GOOD_A = b"first file " * 300
MIDDLE = random_bytes(7, 3000) + b"middle " * 500
GOOD_C = b"third file " * 300


def damaged_middle_zip(path: Path, *, stored: bool = False) -> Path:
    """Three entries; bytes inside the middle entry's (compressed) data are flipped."""
    if stored:
        payload = bytearray(MIDDLE)
    else:
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        payload = bytearray(comp.compress(MIDDLE) + comp.flush())
    for offset in (len(payload) // 2, len(payload) // 2 + 1, len(payload) // 3):
        payload[offset] ^= 0x5A
    middle = file_entry(
        "dir/middle.bin",
        MIDDLE,
        method=0 if stored else 8,
        compressed=bytes(payload),
        declared_crc=zlib.crc32(MIDDLE),
    )
    return write_raw_zip(path, [file_entry("a.txt", GOOD_A), middle, file_entry("c.txt", GOOD_C)])


@pytest.mark.parametrize("stored", [False, True], ids=["deflated", "stored"])
def test_zip_extraction_skips_the_damaged_entry_and_keeps_going(
    tmp_path: Path, stored: bool
) -> None:
    archive = damaged_middle_zip(tmp_path / "d.zip", stored=stored)
    dest = tmp_path / "out"
    report = extract_archive(archive, dest)
    assert extracted_files(dest) == ["a.txt", "c.txt"]
    assert (dest / "a.txt").read_bytes() == GOOD_A
    assert (dest / "c.txt").read_bytes() == GOOD_C
    assert not list(dest.rglob("*.zwpart"))
    errors = [i for i in report.issues if i.severity == "error"]
    assert [(i.code, i.entry) for i in errors] == [(CORRUPT_ENTRY, "dir/middle.bin")]
    assert report.ok is False
    assert report.exit_code == ExitCode.CORRUPT == 4
    assert (report.entries_skipped, report.bytes_done) == (1, len(GOOD_A) + len(GOOD_C))
    assert report.to_dict()["issues"][0]["code"] == "corrupt-entry"


def test_zip_test_reports_exactly_the_damaged_entry(tmp_path: Path) -> None:
    report = verify_archive(damaged_middle_zip(tmp_path / "d.zip"))
    assert [i.entry for i in report.issues] == ["dir/middle.bin"]
    assert report.issues[0].severity == "error"
    assert report.ok is False and report.exit_code == 4


def test_zip_wrong_password_still_aborts(tmp_path: Path) -> None:
    src = tmp_path / "s.txt"
    src.write_bytes(b"secret" * 100)
    archive = tmp_path / "p.zip"
    create_archive([src], archive, CreateOptions(password="right"))
    with pytest.raises(WrongPassword) as info:
        extract_archive(archive, tmp_path / "out", ExtractOptions(password="wrong"))
    assert info.value.exit_code == 6


def test_truncated_tar_gz_keeps_finished_files_and_removes_the_partial_one(
    tmp_path: Path,
) -> None:
    data = {f"f{i}.bin": random_bytes(i, 30_000) for i in range(1, 5)}
    whole = compress_bytes(tar_bytes([tar_file(n, d) for n, d in data.items()]), "gz")
    archive = tmp_path / "cut.tar.gz"
    archive.write_bytes(whole[: int(len(whole) * 0.6)])
    dest = tmp_path / "out"
    with pytest.raises(CorruptArchive) as info:
        extract_archive(archive, dest)
    assert info.value.exit_code == 4
    done = extracted_files(dest)
    assert done == sorted(done) and 0 < len(done) < len(data)
    for name in done:
        assert (dest / name).read_bytes() == data[name]  # complete, not truncated
    assert not list(dest.rglob("*.zwpart"))
    assert list(dest.glob(".*zipwise-resume.json"))  # journal kept for --resume


def test_truncated_single_gz_leaves_no_partial_file(tmp_path: Path) -> None:
    whole = compress_bytes(random_bytes(9, 50_000), "gz")
    archive = tmp_path / "x.bin.gz"
    archive.write_bytes(whole[: len(whole) // 2])
    with pytest.raises(CorruptArchive):
        extract_archive(archive, tmp_path / "out")
    assert extracted_files(tmp_path / "out") == []
    assert not list((tmp_path / "out").rglob("*.zwpart"))


@pytest.mark.parametrize(
    ("issues", "code"),
    [
        ([], 0),
        ([Issue("info", "renamed-existing", "a", "m")], 0),
        ([Issue("warning", "skipped-existing", "a", "m")], 1),
        ([Issue("error", "path-traversal", "a", "m")], 1),
        ([Issue("error", CORRUPT_ENTRY, "a", "m")], 4),
        ([Issue("error", "corrupt-archive", "a", "m")], 4),
        ([Issue("error", "path-traversal", "a", "m"), Issue("error", CORRUPT_ENTRY, "b", "m")], 4),
        ([Issue("warning", CORRUPT_ENTRY, "a", "m")], 1),
    ],
)
def test_exit_code_of_a_report(issues: list[Issue], code: int) -> None:
    assert int(Report.build(issues).exit_code) == code


# --- 7z: wrong password versus damage ----------------------------------------------------


def encrypted_7z(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.bin").write_bytes(random_bytes(1, 20_000))
    (src / "b.bin").write_bytes(random_bytes(2, 20_000))
    archive = tmp_path / "e.7z"
    options = CreateOptions(format=FormatId.SEVENZ, password="right", level="fast")
    create_archive([src / "a.bin", src / "b.bin"], archive, options)
    return archive


def damage_second_file(archive: Path) -> None:
    raw = bytearray(archive.read_bytes())
    raw[32 + 30_000] ^= 0xFF  # packed data starts after the 32-byte signature header
    archive.write_bytes(bytes(raw))


def test_7z_wrong_password_is_reported_as_such(tmp_path: Path) -> None:
    archive = encrypted_7z(tmp_path)
    with pytest.raises(WrongPassword) as info:
        extract_archive(archive, tmp_path / "out", ExtractOptions(password="wrong"))
    assert info.value.hint and "damaged" in info.value.hint


def test_7z_damage_after_a_verified_entry_is_corruption_not_a_password_problem(
    tmp_path: Path,
) -> None:
    archive = encrypted_7z(tmp_path)
    damage_second_file(archive)
    dest = tmp_path / "out"
    with pytest.raises(CorruptArchive):
        extract_archive(archive, dest, ExtractOptions(password="right"))
    assert extracted_files(dest) == ["a.bin"]
    report = verify_archive(archive, password="right")
    assert [(i.code, i.entry) for i in report.issues] == [("corrupt-archive", "b.bin")]
    assert report.exit_code == 4
