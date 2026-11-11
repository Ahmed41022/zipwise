"""Legacy file names in ZIP archives: listing, info, extraction, overrides and fallbacks."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from helpers.builders import RawEntry, file_entry, unicode_path_extra, write_raw_zip
from helpers.names import ARABIC, CORPUS, extracted_files, legacy_zip
from zipwise.errors import UsageError
from zipwise.model import ExtractOptions
from zipwise.ops import archive_info, extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive
from zipwise.safety import SafetyPolicy

NO_NAMES = ExtractOptions(policy=SafetyPolicy(sanitize_windows_names="never"))
ENCODINGS = list(CORPUS)


def expected_files(names: list[str]) -> list[str]:
    return sorted(names)


# --- whole-archive detection ----------------------------------------------------------------


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_list_gives_back_the_original_names(tmp_path: Path, encoding: str) -> None:
    names = CORPUS[encoding]
    archive = legacy_zip(tmp_path / "a.zip", names, encoding)
    assert [e.name for e in list_archive(archive)] == names


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_info_reports_the_detected_encoding(tmp_path: Path, encoding: str) -> None:
    archive = legacy_zip(tmp_path / "a.zip", CORPUS[encoding], encoding)
    info = archive_info(archive)
    assert info.name_encoding is not None
    assert [n.encode(encoding).decode(info.name_encoding) for n in CORPUS[encoding]] == (
        CORPUS[encoding]
    )
    assert info.encoding_confidence is not None and info.encoding_confidence > 0.5
    assert info.issues == ()


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_extract_writes_the_original_names_to_disk(tmp_path: Path, encoding: str) -> None:
    names = CORPUS[encoding]
    archive = legacy_zip(tmp_path / "a.zip", names, encoding)
    dest = tmp_path / "out"
    report = extract_archive(archive, dest, NO_NAMES)
    assert report.ok and report.entries_skipped == 0
    assert extracted_files(dest) == expected_files(names)


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_extracted_content_matches_the_entry(tmp_path: Path, encoding: str) -> None:
    names = CORPUS[encoding]
    archive = legacy_zip(tmp_path / "a.zip", names, encoding)
    dest = tmp_path / "out"
    extract_archive(archive, dest, NO_NAMES)
    for i, name in enumerate(names):
        assert (dest / name).read_bytes() == f"content of {i}".encode()


def test_arabic_cp1256_zip_extracts_with_arabic_names(tmp_path: Path) -> None:
    archive = legacy_zip(tmp_path / "ar.zip", ARABIC, "cp1256")
    dest = tmp_path / "out"
    extract_archive(archive, dest)
    assert (dest / "صور العائلة" / "الرحلة.jpg").is_file()
    assert (dest / "تقرير المبيعات.docx").is_file()
    assert verify_archive(archive).ok


@pytest.mark.parametrize("encoding", ["cp1256", "cp720", "cp866", "cp1251", "cp932", "gbk"])
def test_test_operation_works_on_legacy_archives(tmp_path: Path, encoding: str) -> None:
    archive = legacy_zip(tmp_path / "a.zip", CORPUS[encoding], encoding)
    report = verify_archive(archive)
    assert report.ok and report.entries_done == len(CORPUS[encoding])
    assert not report.issues


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_raw_names_are_kept_on_the_entries(tmp_path: Path, encoding: str) -> None:
    names = CORPUS[encoding]
    archive = legacy_zip(tmp_path / "a.zip", names, encoding)
    assert [e.raw_name for e in list_archive(archive)] == [n.encode(encoding) for n in names]


# --- UTF-8 ----------------------------------------------------------------------------------


def test_unflagged_utf8_names_are_detected(tmp_path: Path) -> None:
    archive = legacy_zip(tmp_path / "u.zip", ARABIC, "utf-8")
    assert [e.name for e in list_archive(archive)] == ARABIC
    info = archive_info(archive)
    assert info.name_encoding == "utf-8"
    assert info.encoding_confidence is not None and info.encoding_confidence >= 0.9


def test_flagged_utf8_names_are_not_redecoded(tmp_path: Path) -> None:
    archive = write_raw_zip(tmp_path / "f.zip", [file_entry(n) for n in ARABIC])
    assert [e.name for e in list_archive(archive)] == ARABIC
    info = archive_info(archive)
    assert info.name_encoding == "utf-8" and info.encoding_confidence == 1.0


def test_flagged_entries_in_a_mixed_archive_are_untouched(tmp_path: Path) -> None:
    flagged = ["café crème.txt", "docs/Müller – Übersicht.docx"]
    legacy = ARABIC[:3]
    entries = [file_entry(n) for n in flagged] + [file_entry(n.encode("cp1256")) for n in legacy]
    archive = write_raw_zip(tmp_path / "m.zip", entries)
    assert [e.name for e in list_archive(archive)] == [*flagged, *legacy]
    assert archive_info(archive).name_encoding == "cp1256"  # chosen from the unflagged names only


def test_forced_encoding_never_touches_flagged_entries(tmp_path: Path) -> None:
    entries = [file_entry("ملف.txt"), file_entry("Отчёт.txt".encode("cp1251"))]
    archive = write_raw_zip(tmp_path / "m.zip", entries)
    names = [e.name for e in list_archive(archive, name_encoding="cp1251")]
    assert names == ["ملف.txt", "Отчёт.txt"]


# --- ASCII ----------------------------------------------------------------------------------


def test_pure_ascii_archive_is_ascii(tmp_path: Path) -> None:
    archive = write_raw_zip(
        tmp_path / "a.zip",
        [file_entry(b"readme.txt", flags=0), file_entry(b"src/main.py", flags=0)],
    )
    info = archive_info(archive)
    assert info.name_encoding == "ascii" and info.encoding_confidence == 1.0
    assert [e.name for e in list_archive(archive)] == ["readme.txt", "src/main.py"]


# --- Unicode Path extra field ---------------------------------------------------------------


def test_unicode_path_extra_field_is_honoured(tmp_path: Path) -> None:
    raw = "ملف.txt".encode("cp1256")
    extra = unicode_path_extra(raw, "ملف.txt")
    archive = write_raw_zip(tmp_path / "x.zip", [RawEntry(raw, b"x", flags=0, extra=extra)])
    assert [e.name for e in list_archive(archive, name_encoding="cp437")] == ["ملف.txt"]


def test_unicode_path_with_wrong_crc_is_ignored(tmp_path: Path) -> None:
    raw = "ملف.txt".encode("cp1256")
    payload = b"\x01" + struct.pack("<I", zlib.crc32(b"other")) + "نص.txt".encode()
    extra = struct.pack("<HH", 0x7075, len(payload)) + payload
    archive = write_raw_zip(tmp_path / "x.zip", [RawEntry(raw, b"x", flags=0, extra=extra)])
    assert [e.name for e in list_archive(archive)] == ["ملف.txt"]


def test_entries_with_unicode_path_are_left_out_of_detection(tmp_path: Path) -> None:
    raw = "Отчёт.txt".encode("cp866")
    extra = unicode_path_extra(raw, "Отчёт.txt")
    entries = [
        RawEntry(raw, b"x", flags=0, extra=extra),
        file_entry("ملاحظات الاجتماع.txt".encode("cp1256")),
        file_entry("تقرير المبيعات.docx".encode("cp1256")),
    ]
    archive = write_raw_zip(tmp_path / "x.zip", entries)
    assert [e.name for e in list_archive(archive)] == [
        "Отчёт.txt",
        "ملاحظات الاجتماع.txt",
        "تقرير المبيعات.docx",
    ]
    assert archive_info(archive).name_encoding == "cp1256"


def test_unicode_path_wins_over_the_override(tmp_path: Path) -> None:
    raw = b"caf\x82.txt"
    archive = write_raw_zip(
        tmp_path / "x.zip",
        [RawEntry(raw, b"x", flags=0, extra=unicode_path_extra(raw, "café.txt"))],
    )
    assert list_archive(archive, name_encoding="cp1256")[0].name == "café.txt"


# --- override -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "same_as_auto"),
    [("cp1256", True), ("cp720", False), ("cp437", False), ("cp1251", False)],
)
def test_override_wins_over_detection(tmp_path: Path, override: str, same_as_auto: bool) -> None:
    archive = legacy_zip(tmp_path / "a.zip", ARABIC, "cp1256")
    forced = [e.name for e in list_archive(archive, name_encoding=override)]
    assert (forced == ARABIC) is same_as_auto
    assert archive_info(archive, name_encoding=override).name_encoding == override


def test_override_reports_full_confidence(tmp_path: Path) -> None:
    archive = legacy_zip(tmp_path / "a.zip", ARABIC, "cp1256")
    assert archive_info(archive, name_encoding="cp720").encoding_confidence == 1.0


def test_override_makes_the_wrong_guess_right(tmp_path: Path) -> None:
    # one short ambiguous name: the detector may read it either way, the user decides
    archive = legacy_zip(tmp_path / "a.zip", ["дом.txt"], "cp1251")
    assert list_archive(archive, name_encoding="cp1251")[0].name == "дом.txt"
    assert list_archive(archive, name_encoding="cp866")[0].name != "дом.txt"


def test_override_applies_to_extraction(tmp_path: Path) -> None:
    archive = legacy_zip(tmp_path / "a.zip", ARABIC, "cp720")
    dest = tmp_path / "out"
    extract_archive(archive, dest, ExtractOptions(name_encoding="cp720", policy=NO_NAMES.policy))
    assert extracted_files(dest) == expected_files(ARABIC)


def test_unknown_override_is_a_usage_error(tmp_path: Path) -> None:
    archive = legacy_zip(tmp_path / "a.zip", ARABIC, "cp1256")
    with pytest.raises(UsageError, match="Unknown name encoding"):
        list_archive(archive, name_encoding="no-such-codec")


# --- names that do not fit ------------------------------------------------------------------


def test_forced_utf8_on_legacy_names_falls_back_with_a_warning(tmp_path: Path) -> None:
    archive = legacy_zip(tmp_path / "a.zip", ARABIC, "cp1256")
    names = [e.name for e in list_archive(archive, name_encoding="utf-8")]
    assert names == [n.encode("cp1256").decode("cp437") for n in ARABIC]
    info = archive_info(archive, name_encoding="utf-8")
    assert [i.code for i in info.issues] == ["name-encoding-fallback"] * len(ARABIC)
    assert all(i.severity == "warning" for i in info.issues)


def test_one_bad_name_in_a_utf8_archive_falls_back_to_cp437(tmp_path: Path) -> None:
    good = [n.encode("utf-8") for n in ARABIC]
    bad = "ملف".encode("cp1256") + b"\xff.txt"
    archive = write_raw_zip(
        tmp_path / "mixed.zip", [RawEntry(r, b"x", flags=0) for r in [*good, bad]]
    )
    names = [e.name for e in list_archive(archive)]
    assert names[: len(ARABIC)] == ARABIC
    assert names[-1] == bad.decode("cp437")
    info = archive_info(archive)
    assert info.name_encoding == "utf-8"
    assert [(i.code, i.entry) for i in info.issues] == [("name-encoding-fallback", names[-1])]


def test_fallback_warning_reaches_the_extract_report(tmp_path: Path) -> None:
    archive = legacy_zip(tmp_path / "a.zip", ARABIC[:2], "cp1256")
    report = extract_archive(
        archive, tmp_path / "out", ExtractOptions(name_encoding="utf-8", policy=NO_NAMES.policy)
    )
    assert [i.code for i in report.issues if i.code == "name-encoding-fallback"] == [
        "name-encoding-fallback"
    ] * 2
    assert report.exit_code == 1  # warnings give exit code 1


def test_fallback_warning_reaches_the_test_report(tmp_path: Path) -> None:
    good = [n.encode("utf-8") for n in ARABIC]
    archive = write_raw_zip(
        tmp_path / "mixed.zip", [RawEntry(r, b"x", flags=0) for r in [*good, b"bad\xff.txt"]]
    )
    report = verify_archive(archive)
    assert report.ok  # a warning is not an error
    assert [i.code for i in report.issues] == ["name-encoding-fallback"]


def test_clean_archives_have_no_issues(tmp_path: Path) -> None:
    archive = write_raw_zip(tmp_path / "c.zip", [file_entry("a.txt"), file_entry("ب.txt")])
    assert archive_info(archive).issues == ()
    assert extract_archive(archive, tmp_path / "out").issues == ()


def test_info_to_dict_carries_the_issues(tmp_path: Path) -> None:
    archive = legacy_zip(tmp_path / "a.zip", ARABIC[:1], "cp1256")
    data = archive_info(archive, name_encoding="ascii").to_dict()
    assert data["name_encoding"] == "ascii"
    assert [i["code"] for i in data["issues"]] == ["name-encoding-fallback"]  # type: ignore[index, union-attr]


# --- names with path separators and odd shapes ----------------------------------------------


def test_directories_with_legacy_names(tmp_path: Path) -> None:
    raw_dir = "المجلد الأول/".encode("cp1256")
    entries = [
        RawEntry(raw_dir, b"", method=0, flags=0, external_attr=(0o040755 << 16) | 0x10),
        file_entry("المجلد الأول/ملف.txt".encode("cp1256")),
    ]
    archive = write_raw_zip(tmp_path / "d.zip", entries)
    names = [e.name for e in list_archive(archive)]
    assert names == ["المجلد الأول", "المجلد الأول/ملف.txt"]
    dest = tmp_path / "out"
    extract_archive(archive, dest)
    assert (dest / "المجلد الأول" / "ملف.txt").is_file()


def test_backslash_separators_in_legacy_names_are_still_contained(tmp_path: Path) -> None:
    raw = "المجلد\\..\\..\\خارج.txt".encode("cp1256")
    archive = write_raw_zip(tmp_path / "e.zip", [file_entry(raw)])
    report = extract_archive(archive, tmp_path / "out")
    assert not report.ok
    assert not (tmp_path / "خارج.txt").exists()
