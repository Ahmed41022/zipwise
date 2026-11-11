"""Hostile TAR archives built with raw ``TarInfo``; nothing may escape the destination."""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

import pytest

from helpers.builders import (
    RawTarMember,
    tar_dir,
    tar_file,
    tar_hardlink,
    tar_special,
    tar_symlink,
    write_raw_tar,
)
from zipwise.backends.tar import TarReader
from zipwise.errors import BombDetected
from zipwise.model import (
    ArchiveEntry,
    ConflictAction,
    ExtractOptions,
    OperationReport,
)
from zipwise.ops import extract_archive
from zipwise.ops.extract import extract_reader
from zipwise.safety import SafetyPolicy

CODECS = ["none", "gz", "zst"]


def run(
    tmp_path: Path,
    members: list[RawTarMember],
    *,
    codec: str = "none",
    policy: SafetyPolicy | None = None,
    conflict: ConflictAction = ConflictAction.OVERWRITE,
    fmt: int = tarfile.GNU_FORMAT,
) -> OperationReport:
    archive = write_raw_tar(tmp_path / "evil.tar", members, codec, fmt=fmt)
    options = ExtractOptions(policy=policy or SafetyPolicy(), on_conflict=conflict)
    return extract_archive(archive, tmp_path / "dest", options)


def assert_nothing_outside(tmp_path: Path) -> None:
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "dest") == ["evil.tar"]


def codes(report: OperationReport) -> list[str]:
    return [i.code for i in report.issues]


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


@pytest.mark.parametrize("codec", CODECS)
@pytest.mark.parametrize(("name", "code"), HOSTILE)
def test_hostile_names_are_blocked_and_benign_entries_still_extract(
    tmp_path: Path, name: str, code: str, codec: str
) -> None:
    report = run(tmp_path, [tar_file(name, b"pwned"), tar_file("good.txt", b"fine")], codec=codec)
    assert code in codes(report)
    assert not report.ok
    assert int(report.exit_code) == 1
    assert report.entries_skipped == 1
    assert (tmp_path / "dest" / "good.txt").read_bytes() == b"fine"
    assert_nothing_outside(tmp_path)
    assert not Path("/zipwise-test-abs.txt").exists()


@pytest.mark.parametrize("name", ["../evil", "..\\evil", "/abs-dir", "C:\\abs-dir"])
def test_hostile_directory_entries_are_blocked(tmp_path: Path, name: str) -> None:
    report = run(tmp_path, [tar_dir(name), tar_file("ok.txt")])
    assert not report.ok
    assert_nothing_outside(tmp_path)


def test_hostile_pax_long_name_is_blocked(tmp_path: Path) -> None:
    name = "a/" + "b" * 150 + "/../../../" + "c" * 150 + "/evil.txt"
    report = run(tmp_path, [tar_file(name, b"x"), tar_file("ok.txt")], fmt=tarfile.PAX_FORMAT)
    assert "path-traversal" in codes(report)
    assert_nothing_outside(tmp_path)


@pytest.mark.parametrize("name", ["", ".", "./."])
def test_empty_and_dot_file_names_are_blocked(tmp_path: Path, name: str) -> None:
    report = run(tmp_path, [tar_file(name, b"x"), tar_file("ok.txt")])
    assert "empty-name" in codes(report)
    assert [p.name for p in (tmp_path / "dest").iterdir()] == ["ok.txt"]


def test_dot_directory_entry_is_ignored(tmp_path: Path) -> None:
    report = run(tmp_path, [tar_dir("."), tar_file("ok.txt")])
    assert report.ok and not report.issues


def test_symlink_to_outside_and_file_through_it_are_both_blocked(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    report = run(tmp_path, [tar_symlink("link", "../outside"), tar_file("link/pwned.txt", b"x")])
    assert sorted(codes(report)) == ["symlink-escape", "symlink-traversal"]
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "dest" / "link").exists()


def test_file_through_an_inside_symlink_is_still_blocked(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        [tar_dir("real"), tar_symlink("alias", "real"), tar_file("alias/f.txt", b"x")],
    )
    assert "symlink-traversal" in codes(report)
    assert not (tmp_path / "dest" / "real" / "f.txt").exists()


@pytest.mark.parametrize("target", ["/etc", "C:\\Windows", "..", "a/../../..", ""])
def test_symlinks_with_dangerous_targets_are_not_created(tmp_path: Path, target: str) -> None:
    report = run(tmp_path, [tar_symlink("l", target)])
    assert codes(report) == ["symlink-escape"]
    assert not os.path.lexists(tmp_path / "dest" / "l")


@pytest.mark.parametrize("target", ["../outside/secret", "/etc/passwd", "C:\\Windows\\win.ini"])
def test_hardlink_to_outside_is_blocked(tmp_path: Path, target: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    report = run(tmp_path, [tar_hardlink("hl", target), tar_file("ok.txt")])
    assert "hardlink-escape" in codes(report)
    assert not (tmp_path / "dest" / "hl").exists()
    assert (outside / "secret").read_bytes() == b"secret"


def test_hardlink_through_a_symlink_is_blocked(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        [tar_dir("real"), tar_symlink("alias", "real"), tar_hardlink("hl", "alias/x")],
    )
    assert "hardlink-escape" in codes(report)


def test_hardlink_to_a_missing_member_is_reported(tmp_path: Path) -> None:
    report = run(tmp_path, [tar_hardlink("hl", "nothing-here")])
    assert "hardlink-missing" in codes(report)
    assert not (tmp_path / "dest" / "hl").exists()


def test_hardlink_inside_the_archive_works(tmp_path: Path) -> None:
    report = run(tmp_path, [tar_file("orig.txt", b"same"), tar_hardlink("copy.txt", "orig.txt")])
    assert report.ok
    assert (tmp_path / "dest" / "copy.txt").read_bytes() == b"same"


@pytest.mark.parametrize("kind", [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_device_and_fifo_entries_are_skipped_with_a_warning(tmp_path: Path, kind: bytes) -> None:
    report = run(tmp_path, [tar_special("dev", kind), tar_file("ok.txt", b"fine")])
    assert codes(report) == ["special-file"]
    assert report.issues[0].severity == "warning"
    assert report.entries_skipped == 1
    assert [p.name for p in (tmp_path / "dest").iterdir()] == ["ok.txt"]


def test_too_many_entries_are_refused(tmp_path: Path) -> None:
    members = [tar_file(f"f{i}.txt", b"") for i in range(300)]
    with pytest.raises(BombDetected) as caught:
        run(tmp_path, members, policy=SafetyPolicy(max_entries=200))
    assert caught.value.code == "too-many-entries"
    assert not (tmp_path / "dest").exists() or not any((tmp_path / "dest").iterdir())


def test_declared_total_above_the_limit_is_refused_before_writing(tmp_path: Path) -> None:
    members = [tar_file("big.bin", b"\0" * 400_000)]
    with pytest.raises(BombDetected) as caught:
        run(tmp_path, members, codec="gz", policy=SafetyPolicy(max_total_bytes=100_000))
    assert caught.value.code == "size-limit"
    assert not (tmp_path / "dest").exists()


def test_declared_entry_size_above_the_per_file_limit_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BombDetected) as caught:
        run(
            tmp_path,
            [tar_file("big.bin", b"\0" * 50_000)],
            policy=SafetyPolicy(max_entry_bytes=10_000),
        )
    assert caught.value.code == "entry-too-large"


class LyingTarReader(TarReader):
    """Reports a smaller size than the data really has, as a hostile header could."""

    def entries(self) -> list[ArchiveEntry]:
        import dataclasses

        return [dataclasses.replace(e, size=10) for e in super().entries()]


@pytest.mark.parametrize("codec", CODECS)
def test_real_output_beyond_the_declared_size_hits_the_byte_budget(
    tmp_path: Path, codec: str
) -> None:
    archive = write_raw_tar(tmp_path / "lie.tar", [tar_file("big.bin", b"A" * 5000)], codec)
    reader = LyingTarReader(archive)
    with reader, pytest.raises(BombDetected) as caught:
        extract_reader(reader, tmp_path / "dest", ExtractOptions(policy=SafetyPolicy()))
    assert caught.value.code == "size-overrun"
    assert list((tmp_path / "dest").iterdir()) == []  # no partial .zwpart left behind


@pytest.mark.parametrize("name", ["CON", "aux.txt", "a.", "a?b", "x:y", "NUL.tar.gz", "lpt1"])
def test_windows_invalid_names_are_renamed_with_policy_always(tmp_path: Path, name: str) -> None:
    policy = SafetyPolicy(sanitize_windows_names="always")
    report = run(tmp_path, [tar_file(name, b"x")], policy=policy)
    assert report.ok
    assert "renamed-invalid-name" in codes(report)
    assert len(list((tmp_path / "dest").iterdir())) == 1
    assert_nothing_outside(tmp_path)


def test_case_clash_is_renamed_on_a_case_insensitive_target(tmp_path: Path) -> None:
    archive = write_raw_tar(
        tmp_path / "evil.tar", [tar_file("Readme.txt", b"one"), tar_file("README.TXT", b"two")]
    )
    with TarReader(archive) as reader:
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
    report = run(
        tmp_path, [tar_file("dup.txt", b"first"), tar_file("dup.txt", b"second")], conflict=conflict
    )
    contents = sorted(p.read_bytes() for p in (tmp_path / "dest").iterdir())
    assert contents == expected
    assert report.ok or conflict is ConflictAction.SKIP


def test_file_over_an_existing_directory_is_blocked(tmp_path: Path) -> None:
    (tmp_path / "dest" / "name").mkdir(parents=True)
    report = run(tmp_path, [tar_file("name", b"x")], conflict=ConflictAction.OVERWRITE)
    assert "dir-in-the-way" in codes(report)
    assert (tmp_path / "dest" / "name").is_dir()
