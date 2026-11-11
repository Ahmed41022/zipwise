"""Hostile 7z archives (names that py7zr's own writer refuses are written through its
internals); nothing may escape the destination."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from helpers.builders import write_raw_7z
from zipwise.backends.sevenz import SevenZipReader
from zipwise.errors import BombDetected
from zipwise.model import ArchiveEntry, ConflictAction, ExtractOptions, OperationReport
from zipwise.ops import extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive
from zipwise.ops.extract import extract_reader
from zipwise.safety import SafetyPolicy

Item = tuple[str, str, bytes | str]


def run(
    tmp_path: Path,
    items: list[Item],
    *,
    policy: SafetyPolicy | None = None,
    conflict: ConflictAction = ConflictAction.OVERWRITE,
) -> OperationReport:
    archive = write_raw_7z(tmp_path / "evil.7z", items)
    options = ExtractOptions(policy=policy or SafetyPolicy(), on_conflict=conflict)
    return extract_archive(archive, tmp_path / "dest", options)


def assert_nothing_outside(tmp_path: Path) -> None:
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "dest") == ["evil.7z"]


def codes(report: OperationReport) -> list[str]:
    return [i.code for i in report.issues]


def file(name: str, data: bytes = b"data") -> Item:
    return ("file", name, data)


def nothing_written(tmp_path: Path) -> bool:
    dest = tmp_path / "dest"
    return not dest.exists() or not any(dest.iterdir())


HOSTILE = [
    ("../evil.txt", "path-traversal"),
    ("a/../../evil.txt", "path-traversal"),
    ("..\\evil.txt", "path-traversal"),
    ("sub\\..\\..\\evil.txt", "path-traversal"),
    ("/zipwise-test-abs.txt", "absolute-path"),
    ("C:\\zipwise-test-abs.txt", "absolute-path"),
    ("c:/zipwise-test-abs.txt", "absolute-path"),
    ("\\\\srv\\share\\x.txt", "absolute-path"),
    ("//srv/share/x.txt", "absolute-path"),
]


@pytest.mark.parametrize(("name", "code"), HOSTILE)
def test_hostile_names_are_blocked_and_benign_entries_still_extract(
    tmp_path: Path, name: str, code: str
) -> None:
    report = run(tmp_path, [file(name, b"pwned"), file("good.txt", b"fine")])
    assert code in codes(report)
    assert not report.ok
    assert int(report.exit_code) == 1
    assert report.entries_skipped == 1
    assert (tmp_path / "dest" / "good.txt").read_bytes() == b"fine"
    assert_nothing_outside(tmp_path)
    assert not Path("/zipwise-test-abs.txt").exists()


@pytest.mark.parametrize("name", ["../evil", "..\\evil", "/abs-dir", "C:\\abs-dir"])
def test_hostile_directory_entries_are_blocked(tmp_path: Path, name: str) -> None:
    report = run(tmp_path, [("dir", name, b""), file("ok.txt")])
    assert not report.ok
    assert_nothing_outside(tmp_path)


@pytest.mark.parametrize("name", ["", ".", "./."])
def test_empty_and_dot_file_names_are_blocked(tmp_path: Path, name: str) -> None:
    report = run(tmp_path, [file(name, b"x"), file("ok.txt")])
    assert "empty-name" in codes(report)
    assert [p.name for p in (tmp_path / "dest").iterdir()] == ["ok.txt"]


def test_symlink_to_outside_and_file_through_it_are_both_blocked(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    report = run(tmp_path, [("symlink", "link", "../outside"), file("link/pwned.txt", b"x")])
    assert sorted(codes(report)) == ["symlink-escape", "symlink-traversal"]
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "dest" / "link").exists()


def test_file_through_an_inside_symlink_is_still_blocked(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        [("dir", "real", b""), ("symlink", "alias", "real"), file("alias/f.txt", b"x")],
    )
    assert "symlink-traversal" in codes(report)
    assert not (tmp_path / "dest" / "real" / "f.txt").exists()


@pytest.mark.parametrize("target", ["/etc", "C:\\Windows", "..", "a/../../.."])
def test_symlinks_with_dangerous_targets_are_not_created(tmp_path: Path, target: str) -> None:
    report = run(tmp_path, [("symlink", "l", target)])
    assert codes(report) == ["symlink-escape"]
    assert not os.path.lexists(tmp_path / "dest" / "l")


def test_unsafe_names_do_not_break_listing_or_testing(tmp_path: Path) -> None:
    archive = write_raw_7z(tmp_path / "evil.7z", [file("../evil.txt"), file("good.txt")])
    assert sorted(e.name for e in list_archive(archive)) == ["../evil.txt", "good.txt"]
    assert verify_archive(archive).ok


def test_high_ratio_bomb_is_refused_before_writing(tmp_path: Path) -> None:
    policy = SafetyPolicy(ratio_min_bytes=1000, max_ratio=50)
    with pytest.raises(BombDetected) as caught:
        run(tmp_path, [file("zeros.bin", b"\0" * 4_000_000)], policy=policy)
    assert caught.value.code == "ratio"
    assert nothing_written(tmp_path)


def test_declared_total_above_the_limit_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BombDetected) as caught:
        run(
            tmp_path,
            [file("big.bin", os.urandom(50_000))],
            policy=SafetyPolicy(max_total_bytes=10_000),
        )
    assert caught.value.code == "size-limit"
    assert nothing_written(tmp_path)


def test_too_many_entries_are_refused(tmp_path: Path) -> None:
    items = [file(f"f{i}.txt", b"x") for i in range(60)]
    with pytest.raises(BombDetected) as caught:
        run(tmp_path, items, policy=SafetyPolicy(max_entries=50))
    assert caught.value.code == "too-many-entries"


class LyingReader(SevenZipReader):
    """Reports a smaller size than the data really has, as a hostile header could."""

    def entries(self) -> list[ArchiveEntry]:
        return [dataclasses.replace(e, size=10) for e in super().entries()]


def test_real_output_beyond_the_declared_size_hits_the_byte_budget(tmp_path: Path) -> None:
    archive = write_raw_7z(tmp_path / "lie.7z", [file("big.bin", b"A" * 5000)])
    with LyingReader(archive) as reader, pytest.raises(BombDetected) as caught:
        extract_reader(reader, tmp_path / "dest", ExtractOptions(policy=SafetyPolicy()))
    assert caught.value.code == "size-overrun"
    assert list((tmp_path / "dest").iterdir()) == []  # the partial file was removed


@pytest.mark.parametrize("name", ["CON", "aux.txt", "a.", "a?b", "x:y", "lpt1"])
def test_windows_invalid_names_are_renamed_with_policy_always(tmp_path: Path, name: str) -> None:
    policy = SafetyPolicy(sanitize_windows_names="always")
    report = run(tmp_path, [file(name, b"x")], policy=policy)
    assert report.ok
    assert "renamed-invalid-name" in codes(report)
    assert len(list((tmp_path / "dest").iterdir())) == 1
    assert_nothing_outside(tmp_path)


def test_case_clash_is_renamed_on_a_case_insensitive_target(tmp_path: Path) -> None:
    items = [file("Readme.txt", b"one"), file("README.TXT", b"two")]
    archive = write_raw_7z(tmp_path / "evil.7z", items)
    with SevenZipReader(archive) as reader:
        report = extract_reader(reader, tmp_path / "dest", ExtractOptions(), case_insensitive=True)
    assert "renamed-case-clash" in codes(report)
    assert sorted(p.read_bytes() for p in (tmp_path / "dest").iterdir()) == [b"one", b"two"]


@pytest.mark.parametrize(
    ("conflict", "expected"),
    [
        (ConflictAction.OVERWRITE, [b"second"]),
        (ConflictAction.SKIP, [b"first"]),
        (ConflictAction.RENAME, [b"first", b"second"]),
    ],
)
def test_duplicate_names_follow_the_conflict_policy(
    tmp_path: Path, conflict: ConflictAction, expected: list[bytes]
) -> None:
    run(tmp_path, [file("dup.txt", b"first"), file("dup.txt", b"second")], conflict=conflict)
    assert sorted(p.read_bytes() for p in (tmp_path / "dest").iterdir()) == expected
