"""Part discovery, recipient notes and the MultiPartFile object."""

from __future__ import annotations

import io
import random
import zipfile
from pathlib import Path

import pytest

from helpers.trees import random_bytes
from zipwise.errors import CorruptArchive, IOFailure, UsageError
from zipwise.split import (
    NOTES_NAME,
    MultiPartFile,
    find_parts,
    is_part_path,
    join_parts,
    manifest_name,
    part_digits,
    part_name,
    recipient_notes,
    split_file,
)


def make_parts(folder: Path, size: int, part: int, name: str = "data.bin") -> list[Path]:
    src = folder / name
    src.write_bytes(random_bytes(7, size))
    return split_file(src, part, manifest=False)


# --- naming ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "digits"), [(1, 3), (999, 3), (1000, 4), (9999, 4), (10000, 5), (123456, 6)]
)
def test_part_digits(count: int, digits: int) -> None:
    assert part_digits(count) == digits


def test_part_name() -> None:
    assert part_name("a.zip", 7, 3) == "a.zip.007"
    assert part_name("a.zip", 7, 4) == "a.zip.0007"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.zip.001", True),
        ("a.7z.123", True),
        ("a.zip.0001", True),
        ("a.zip.999", True),
        ("a.zip", False),
        ("a.zip.01", False),
        ("a.001.zip", False),
        ("a.zip.abc", False),
        ("001", False),
    ],
)
def test_is_part_path(name: str, expected: bool) -> None:
    assert is_part_path(Path(name)) is expected


# --- find_parts -----------------------------------------------------------------------------


def test_find_parts_from_any_part(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 450, 100)
    for part in parts:
        found = find_parts(part)
        assert list(found.parts) == parts
        assert found.first == parts[0]
        assert found.base == "data.bin"
        assert found.digits == 3
        assert found.total_size == 450


def test_missing_middle_part_is_reported_by_name(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 450, 100)
    parts[2].unlink()
    with pytest.raises(CorruptArchive) as info:
        find_parts(parts[0])
    assert "data.bin.003" in info.value.message and info.value.code == "part-missing"
    assert ".001" in info.value.message and ".004" in info.value.message  # what was found


def test_missing_first_part(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 450, 100)
    parts[0].unlink()
    with pytest.raises(CorruptArchive, match=r"data\.bin\.001"):
        find_parts(parts[1])


def test_gap_after_the_given_part(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 900, 100)
    parts[4].unlink()
    parts[5].unlink()
    with pytest.raises(CorruptArchive, match=r"data\.bin\.005"):
        find_parts(parts[1])


def test_numbering_must_start_at_one(tmp_path: Path) -> None:
    (tmp_path / "x.bin.002").write_bytes(b"a")
    (tmp_path / "x.bin.003").write_bytes(b"b")
    with pytest.raises(CorruptArchive, match=r"x\.bin\.001"):
        find_parts(tmp_path / "x.bin.002")


def test_stray_files_with_other_widths_are_reported_not_joined(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 300, 100)
    stray = tmp_path / "data.bin.0004"
    stray.write_bytes(b"zz")
    found = find_parts(parts[0])
    assert list(found.parts) == parts
    assert found.stray == (stray,)


def test_unrelated_files_are_ignored(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 300, 100)
    (tmp_path / "other.bin.001").write_bytes(b"x")
    (tmp_path / "data.bin.bak").write_bytes(b"x")
    (tmp_path / "data.bin.001.old").write_bytes(b"x")
    assert list(find_parts(parts[0]).parts) == parts


def test_a_part_that_is_not_numbered_is_a_usage_error(tmp_path: Path) -> None:
    (tmp_path / "plain.zip").write_bytes(b"x")
    with pytest.raises(UsageError, match="not a numbered part"):
        find_parts(tmp_path / "plain.zip")


def test_a_missing_file_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="not found"):
        find_parts(tmp_path / "nothing.zip.001")


def test_manifest_lists_a_part_that_is_gone(tmp_path: Path) -> None:
    src = tmp_path / "data.bin"
    src.write_bytes(random_bytes(3, 450))
    parts = split_file(src, 100)
    parts[-1].unlink()  # the last one cannot be noticed from numbering alone
    with pytest.raises(CorruptArchive, match=r"data\.bin\.005"):
        find_parts(parts[0])


def test_manifest_rejects_a_stray_extra_part(tmp_path: Path) -> None:
    src = tmp_path / "data.bin"
    src.write_bytes(random_bytes(3, 300))
    parts = split_file(src, 100)
    (tmp_path / "data.bin.004").write_bytes(b"extra")
    with pytest.raises(CorruptArchive, match=r"data\.bin\.004"):
        find_parts(parts[0])


def test_seven_zip_style_overflow_numbering_is_understood(tmp_path: Path) -> None:
    for i in (*range(1, 1000), 1000, 1001):
        (tmp_path / f"v.7z.{i:03d}").write_bytes(b"x")
    found = find_parts(tmp_path / "v.7z.001")
    assert len(found.parts) == 1001
    assert found.parts[-1].name == "v.7z.1001"


# --- recipient notes ------------------------------------------------------------------------


def read_notes(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_notes_contain_english_then_arabic(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 300, 100, "report.zip")
    notes = recipient_notes(parts, "report.zip")
    text = read_notes(notes)
    assert notes.name == NOTES_NAME and notes.parent == tmp_path
    assert text.index("HOW TO OPEN") < text.index("طريقة فتح هذه الملفات")


def test_notes_name_the_right_files(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 300, 100, "report.zip")
    text = read_notes(recipient_notes(parts, "report.zip"))
    assert "report.zip.001" in text and "report.zip.003" in text
    assert 'zipwise join "report.zip.001"' in text
    assert "3 part(s)" in text and "3 جزء" in text
    assert text.count("«report.zip»") >= 2  # the Arabic text quotes the result file


@pytest.mark.parametrize("needle", ["7-Zip", "WinRAR", "zipwise join", "copy /b", "cat "])
def test_notes_explain_every_way_to_open(tmp_path: Path, needle: str) -> None:
    parts = make_parts(tmp_path, 300, 100, "report.zip")
    assert needle in read_notes(recipient_notes(parts, "report.zip"))


def test_notes_wildcards_do_not_match_the_manifest(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 300, 100, "report.zip")
    text = read_notes(recipient_notes(parts, "report.zip"))
    assert 'copy /b "report.zip.???" "report.zip"' in text
    assert 'cat "report.zip.[0-9][0-9][0-9]" > "report.zip"' in text
    assert manifest_name("report.zip").endswith(".parts.json")  # has more than three characters


def test_notes_use_wider_wildcards_for_four_digit_sets(tmp_path: Path) -> None:
    parts = [tmp_path / f"x.bin.{i:04d}" for i in range(1, 1101)]  # only the names matter
    text = read_notes(recipient_notes(parts, "x.bin", directory=tmp_path))
    assert '"x.bin.????"' in text


def test_notes_for_a_single_part(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 10, 100, "tiny.zip")
    text = read_notes(recipient_notes(parts, "tiny.zip"))
    assert "1 part(s)" in text and "..." not in text.split("Option 1")[0]


def test_notes_without_parts_are_an_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        recipient_notes([], "x")


def test_notes_name_defaults_to_the_part_base(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 300, 100, "auto.iso")
    assert "«auto.iso»" in read_notes(recipient_notes(parts))


def test_notes_are_valid_utf8_with_unix_newlines(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 300, 100, "r.zip")
    raw = recipient_notes(parts, "r.zip").read_bytes()
    raw.decode("utf-8")
    assert b"\r\n" not in raw


# --- MultiPartFile --------------------------------------------------------------------------


def test_reads_the_same_bytes_as_the_joined_file(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 1000, 128)
    joined = join_parts(parts[0], tmp_path / "joined").read_bytes()
    with MultiPartFile(parts) as fh:
        assert fh.read() == joined
        assert fh.tell() == len(joined)
        assert fh.read(10) == b""


@pytest.mark.parametrize("seed", range(5))
def test_random_seeks_and_reads_match_the_joined_bytes(tmp_path: Path, seed: int) -> None:
    parts = make_parts(tmp_path, 5000, 300)
    expected = join_parts(parts[0], tmp_path / "joined").read_bytes()
    rng = random.Random(seed)
    with MultiPartFile(parts) as fh:
        assert fh.size == len(expected)
        for _ in range(200):
            offset = rng.randrange(0, len(expected) + 20)
            length = rng.choice([0, 1, 2, 7, 299, 300, 301, 600, 1234, 9999])
            assert fh.seek(offset) == offset
            assert fh.read(length) == expected[offset : offset + length]
            assert fh.tell() == min(offset + length, max(offset, len(expected)))


def test_seek_whence_variants(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 1000, 300)
    data = join_parts(parts[0], tmp_path / "joined").read_bytes()
    with MultiPartFile(parts) as fh:
        fh.seek(-10, io.SEEK_END)
        assert fh.read() == data[-10:]
        fh.seek(100)
        fh.seek(50, io.SEEK_CUR)
        assert fh.read(5) == data[150:155]
        fh.seek(0, io.SEEK_END)
        assert fh.tell() == 1000


def test_seek_errors(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 100, 30)
    with MultiPartFile(parts) as fh:
        with pytest.raises(ValueError):
            fh.seek(-1)
        with pytest.raises(ValueError):
            fh.seek(0, 9)


def test_read_across_boundaries_returns_the_full_request(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 100, 10)
    data = join_parts(parts[0], tmp_path / "j").read_bytes()
    with MultiPartFile(parts) as fh:
        fh.seek(5)
        assert fh.read(60) == data[5:65]  # six part boundaries in one call


def test_readinto(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 100, 10)
    data = join_parts(parts[0], tmp_path / "j").read_bytes()
    buf = bytearray(35)
    with MultiPartFile(parts) as fh:
        fh.seek(8)
        assert fh.readinto(buf) == 35
        assert bytes(buf) == data[8:43]
        fh.seek(90)
        assert fh.readinto(buf) == 10


def test_capabilities_and_state(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 100, 30)
    fh = MultiPartFile(parts)
    assert fh.readable() and fh.seekable() and not fh.writable()
    assert fh.name == str(parts[0])
    assert not fh.closed
    fh.close()
    assert fh.closed
    fh.close()  # closing twice is fine
    with pytest.raises(ValueError):
        fh.read(1)
    with pytest.raises(ValueError):
        fh.tell()


def test_context_manager_closes(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 100, 30)
    with MultiPartFile(parts) as fh:
        fh.read(40)
    assert fh.closed


def test_only_one_part_is_open_at_a_time(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 100, 10)
    with MultiPartFile(parts) as fh:
        fh.read()
        assert fh._fh is not None and fh._current == len(parts) - 1
    assert fh._fh is None


def test_empty_part_list_is_rejected() -> None:
    with pytest.raises(UsageError):
        MultiPartFile([])


def test_missing_part_file_is_an_io_failure(tmp_path: Path) -> None:
    with pytest.raises(IOFailure):
        MultiPartFile([tmp_path / "gone.001"])


def test_zero_length_parts_are_skipped(tmp_path: Path) -> None:
    a, b, c = tmp_path / "x.001", tmp_path / "x.002", tmp_path / "x.003"
    a.write_bytes(b"abc")
    b.write_bytes(b"")
    c.write_bytes(b"def")
    with MultiPartFile([a, b, c]) as fh:
        assert fh.read() == b"abcdef"


def test_a_part_that_shrank_is_reported(tmp_path: Path) -> None:
    parts = make_parts(tmp_path, 100, 50)
    with MultiPartFile(parts) as fh:
        parts[0].write_bytes(b"short")
        with pytest.raises(IOFailure, match="shorter"):
            fh.read(100)


def test_zipfile_can_read_straight_from_parts(tmp_path: Path) -> None:
    whole = tmp_path / "w.zip"
    with zipfile.ZipFile(whole, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(5):
            zf.writestr(f"f{i}.txt", random_bytes(i, 3000))
    parts = split_file(whole, 777, manifest=False)
    with MultiPartFile(parts) as fh, zipfile.ZipFile(fh) as zf:  # type: ignore[arg-type]
        assert zf.testzip() is None
        assert zf.read("f3.txt") == random_bytes(3, 3000)
