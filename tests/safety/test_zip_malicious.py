"""Hostile ZIP archives built byte by byte; nothing may escape the destination."""

from __future__ import annotations

import os
import zlib
from pathlib import Path

import pytest

from helpers.builders import (
    S_IFCHR,
    RawEntry,
    dir_entry,
    file_entry,
    special_entry,
    symlink_entry,
    write_raw_zip,
)
from helpers.jobs import stepping_context
from zipwise.backends.base import ArchiveReader
from zipwise.backends.zip import ZipReader
from zipwise.errors import BombDetected, CorruptArchive, SafetyViolation
from zipwise.fsutil import is_case_insensitive
from zipwise.jobs import JobContext
from zipwise.model import (
    ArchiveEntry,
    ArchiveInfo,
    ConflictAction,
    EntryType,
    ExtractOptions,
    FormatId,
    Issue,
    OperationReport,
)
from zipwise.ops import extract_archive
from zipwise.ops.extract import extract_reader
from zipwise.safety import SafetyPolicy


def run(
    tmp_path: Path,
    entries: list[RawEntry],
    *,
    policy: SafetyPolicy | None = None,
    conflict: ConflictAction = ConflictAction.OVERWRITE,
) -> OperationReport:
    archive = write_raw_zip(tmp_path / "evil.zip", entries)
    options = ExtractOptions(policy=policy or SafetyPolicy(), on_conflict=conflict)
    return extract_archive(archive, tmp_path / "dest", options)


def assert_nothing_outside(tmp_path: Path) -> None:
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "dest") == ["evil.zip"]


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


@pytest.mark.parametrize(("name", "code"), HOSTILE)
def test_hostile_names_are_blocked_and_benign_entries_still_extract(
    tmp_path: Path, name: str, code: str
) -> None:
    report = run(tmp_path, [file_entry(name, b"pwned"), file_entry("good.txt", b"fine")])
    assert code in codes(report)
    assert not report.ok
    assert int(report.exit_code) == 1
    assert report.entries_skipped == 1
    assert (tmp_path / "dest" / "good.txt").read_bytes() == b"fine"
    assert_nothing_outside(tmp_path)
    assert not Path("/zipwise-test-abs.txt").exists()


@pytest.mark.parametrize("name", ["../evil", "..\\evil", "/abs-dir", "C:\\abs-dir"])
def test_hostile_directory_entries_are_blocked(tmp_path: Path, name: str) -> None:
    report = run(tmp_path, [dir_entry(name), file_entry("ok.txt")])
    assert not report.ok
    assert_nothing_outside(tmp_path)


def test_nul_byte_in_name_is_blocked(tmp_path: Path) -> None:
    report = run(tmp_path, [file_entry("safe.txt\0../../evil.txt", b"x"), file_entry("ok.txt")])
    assert "invalid-name" in codes(report)
    assert sorted(p.name for p in (tmp_path / "dest").iterdir()) == ["ok.txt"]
    assert_nothing_outside(tmp_path)


@pytest.mark.parametrize("name", ["", ".", "./."])
def test_empty_and_dot_file_names_are_blocked(tmp_path: Path, name: str) -> None:
    report = run(tmp_path, [file_entry(name, b"x"), file_entry("ok.txt")])
    assert "empty-name" in codes(report)
    assert [p.name for p in (tmp_path / "dest").iterdir()] == ["ok.txt"]


def test_dot_directory_entry_is_ignored(tmp_path: Path) -> None:
    report = run(tmp_path, [dir_entry("."), file_entry("ok.txt")])
    assert report.ok and not report.issues


def test_symlink_to_outside_and_file_through_it_are_both_blocked(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    report = run(
        tmp_path,
        [symlink_entry("link", "../outside"), file_entry("link/pwned.txt", b"x")],
    )
    assert sorted(codes(report)) == ["symlink-escape", "symlink-traversal"]
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "dest" / "link").exists()


@pytest.mark.parametrize("target", ["/etc", "C:\\Windows", "..", "a/../../.."])
def test_symlinks_with_dangerous_targets_are_not_created(tmp_path: Path, target: str) -> None:
    report = run(tmp_path, [symlink_entry("l", target)])
    assert codes(report) == ["symlink-escape"]
    assert not os.path.lexists(tmp_path / "dest" / "l")


@pytest.mark.needs_symlink
def test_safe_symlink_is_created_after_the_files(tmp_path: Path) -> None:
    report = run(tmp_path, [symlink_entry("link", "real.txt"), file_entry("real.txt", b"data")])
    assert report.ok
    link = tmp_path / "dest" / "link"
    assert link.is_symlink()
    assert link.read_bytes() == b"data"


@pytest.mark.needs_symlink
def test_symlinks_can_be_refused_by_policy(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        [symlink_entry("link", "real.txt"), file_entry("real.txt")],
        policy=SafetyPolicy(allow_symlinks=False),
    )
    assert "symlink-disallowed" in codes(report)
    assert not os.path.lexists(tmp_path / "dest" / "link")


@pytest.mark.needs_symlink
def test_existing_symlink_in_destination_is_never_written_through(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    os.symlink(outside, dest / "link", target_is_directory=True)
    report = run(tmp_path, [file_entry("link/evil.txt", b"x"), file_entry("ok.txt")])
    assert "symlink-traversal" in codes(report)
    assert list(outside.iterdir()) == []


@pytest.mark.needs_symlink
def test_overwriting_a_symlink_replaces_the_link_not_its_target(tmp_path: Path) -> None:
    outside = tmp_path / "victim.txt"
    outside.write_bytes(b"precious")
    dest = tmp_path / "dest"
    dest.mkdir()
    os.symlink(outside, dest / "f.txt")
    report = run(tmp_path, [file_entry("f.txt", b"new")], conflict=ConflictAction.OVERWRITE)
    assert report.ok
    assert outside.read_bytes() == b"precious"
    assert not (dest / "f.txt").is_symlink()
    assert (dest / "f.txt").read_bytes() == b"new"


@pytest.mark.parametrize("kind", [S_IFCHR, 0o010000, 0o140000], ids=["chr", "fifo", "socket"])
def test_special_files_are_skipped_with_a_warning(tmp_path: Path, kind: int) -> None:
    report = run(tmp_path, [special_entry("dev", kind), file_entry("ok.txt")])
    assert codes(report) == ["special-file"]
    assert report.issues[0].severity == "warning"
    assert report.ok  # warnings do not fail the job
    assert int(report.exit_code) == 1
    assert [p.name for p in (tmp_path / "dest").iterdir()] == ["ok.txt"]


def test_file_in_the_way_of_a_directory_is_reported(tmp_path: Path) -> None:
    report = run(tmp_path, [file_entry("a", b"file"), file_entry("a/b.txt", b"x")])
    assert "parent-is-file" in codes(report)
    assert (tmp_path / "dest" / "a").read_bytes() == b"file"


def test_backslash_separated_names_become_folders(tmp_path: Path) -> None:
    report = run(tmp_path, [file_entry("dir\\sub\\file.txt", b"x")])
    assert report.ok
    assert (tmp_path / "dest" / "dir" / "sub" / "file.txt").read_bytes() == b"x"


def test_allow_absolute_policy_relocates_inside_destination(tmp_path: Path) -> None:
    report = run(
        tmp_path,
        [file_entry("/abs/file.txt", b"x")],
        policy=SafetyPolicy(allow_absolute=True),
    )
    assert report.ok
    assert (tmp_path / "dest" / "abs" / "file.txt").exists()


# --- bombs and lying headers ----------------------------------------------------------------


def test_high_ratio_bomb_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    bomb = file_entry("zeros.bin", b"\0" * 5_000_000, level=9)
    assert len(bomb.compressed or b"") == 0  # compressed lazily by the builder
    policy = SafetyPolicy(ratio_min_bytes=1000)
    with pytest.raises(BombDetected) as info:
        run(tmp_path, [file_entry("ok.txt"), bomb], policy=policy)
    assert info.value.code == "ratio"
    assert info.value.exit_code == 5
    assert not (tmp_path / "dest").exists() or list((tmp_path / "dest").rglob("*")) == []


def test_bomb_archive_is_small_on_disk(tmp_path: Path) -> None:
    archive = write_raw_zip(tmp_path / "b.zip", [file_entry("z", b"\0" * 5_000_000)])
    assert archive.stat().st_size < 10_000


def test_bomb_limits_are_raisable(tmp_path: Path) -> None:
    policy = SafetyPolicy(ratio_min_bytes=1000, max_ratio=100_000)
    report = run(tmp_path, [file_entry("z.bin", b"\0" * 2_000_000)], policy=policy)
    assert report.ok
    assert (tmp_path / "dest" / "z.bin").stat().st_size == 2_000_000


def test_total_size_limit_aborts_the_whole_job(tmp_path: Path) -> None:
    with pytest.raises(BombDetected):
        run(
            tmp_path,
            [file_entry("a", b"x" * 600), file_entry("b", b"y" * 600)],
            policy=SafetyPolicy(max_total_bytes=1000),
        )


def test_too_many_entries_are_refused(tmp_path: Path) -> None:
    entries = [file_entry(f"e{i}", b"", method=0) for i in range(2000)]
    with pytest.raises(BombDetected) as info:
        run(tmp_path, entries, policy=SafetyPolicy(max_entries=1000))
    assert info.value.code == "too-many-entries"


def test_header_declaring_less_than_the_real_size_cannot_overrun(tmp_path: Path) -> None:
    data = b"A" * 100_000
    entry = file_entry(
        "lie.bin", data, declared_size=1000, declared_crc=zlib.crc32(data[:1000]), level=9
    )
    try:
        report = run(tmp_path, [entry])
    except (CorruptArchive, BombDetected):
        report = None
    written = tmp_path / "dest" / "lie.bin"
    if report is not None:
        assert written.stat().st_size <= 1000
    assert not list((tmp_path / "dest").rglob("*.zwpart"))


def test_header_with_wrong_crc_is_corrupt_and_leaves_no_partial_file(tmp_path: Path) -> None:
    data = b"B" * 50_000
    entry = file_entry("lie.bin", data, declared_size=1000, level=9)
    with pytest.raises(CorruptArchive):
        run(tmp_path, [file_entry("first.txt", b"ok"), entry])
    leftovers = [
        p.name
        for p in (tmp_path / "dest").rglob("*")
        if p.is_file() and not p.name.endswith(".zipwise-resume.json")
    ]
    assert leftovers == ["first.txt"]


class LyingReader(ArchiveReader):
    """A backend whose data outgrows the declared sizes, like a hostile TAR or 7z."""

    format = FormatId.TAR

    def __init__(self, declared: int, produced: int) -> None:
        super().__init__(Path("fake"))
        self.declared = declared
        self.produced = produced

    def entries(self) -> list[ArchiveEntry]:
        return [ArchiveEntry("liar.bin", size=self.declared, index=0)]

    def extract(self, entries, sink, ctx) -> None:  # type: ignore[no-untyped-def]
        out = sink.open(entries[0])
        assert out is not None
        ok = False
        try:
            remaining = self.produced
            while remaining:
                chunk = min(remaining, 4096)
                out.write(b"\1" * chunk)
                remaining -= chunk
            ok = True
        finally:
            sink.close(entries[0], out, ok)

    def test(self, ctx: JobContext) -> list[Issue]:
        return []

    def info(self) -> ArchiveInfo:
        raise NotImplementedError

    def close(self) -> None:
        pass


def test_byte_budget_stops_a_lying_backend_and_removes_the_partial(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    with pytest.raises(BombDetected) as info:
        extract_reader(LyingReader(declared=1000, produced=1_000_000), dest)
    assert info.value.code == "size-overrun"
    assert list(dest.rglob("*")) == []  # neither the file nor its .zwpart remain


def test_honest_backend_passes_the_budget(tmp_path: Path) -> None:
    report = extract_reader(LyingReader(declared=5000, produced=5000), tmp_path / "dest")
    assert report.ok
    assert (tmp_path / "dest" / "liar.bin").stat().st_size == 5000


def test_total_limit_applies_to_backends_that_underdeclare(tmp_path: Path) -> None:
    options = ExtractOptions(policy=SafetyPolicy(max_total_bytes=2000, max_entry_bytes=None))
    with pytest.raises(BombDetected):
        extract_reader(LyingReader(declared=-1, produced=5000), tmp_path / "dest", options)


# --- naming and conflicts -------------------------------------------------------------------

WINDOWS_NAMES = [
    ("CON", "_CON"),
    ("aux.txt", "_aux.txt"),
    ("a.", "a"),
    ("a?b.txt", "a_b.txt"),
    ("x:y", "x_y"),
    ("dir/NUL.log", "dir/_NUL.log"),
    ("bad<name>.txt", "bad_name_.txt"),
    ("trail ", "trail"),
]


@pytest.mark.parametrize(("name", "expected"), WINDOWS_NAMES)
def test_windows_invalid_names_are_renamed_when_policy_is_always(
    tmp_path: Path, name: str, expected: str
) -> None:
    report = run(
        tmp_path, [file_entry(name, b"x")], policy=SafetyPolicy(sanitize_windows_names="always")
    )
    assert report.ok
    assert "renamed-invalid-name" in codes(report)
    assert (tmp_path / "dest" / Path(*expected.split("/"))).read_bytes() == b"x"


def test_clean_names_produce_no_rename_issue(tmp_path: Path) -> None:
    report = run(
        tmp_path, [file_entry("fine.txt")], policy=SafetyPolicy(sanitize_windows_names="always")
    )
    assert not report.issues


def test_case_clash_second_entry_is_renamed_on_insensitive_target(tmp_path: Path) -> None:
    archive = write_raw_zip(
        tmp_path / "c.zip", [file_entry("Readme.txt", b"one"), file_entry("README.TXT", b"two")]
    )
    dest = tmp_path / "dest"
    with ZipReader(archive) as reader:
        report = extract_reader(reader, dest, ExtractOptions(), case_insensitive=True)
    assert "renamed-case-clash" in codes(report)
    assert (dest / "Readme.txt").read_bytes() == b"one"
    assert (dest / "README (2).TXT").read_bytes() == b"two"


def test_case_variants_follow_the_real_file_system_when_not_forced(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    report = run(
        tmp_path,
        [file_entry("Readme.txt", b"one"), file_entry("README.TXT", b"two")],
        conflict=ConflictAction.SKIP,
    )
    names = sorted(p.name for p in dest.iterdir())
    if is_case_insensitive(dest):
        assert names == ["README (2).TXT", "Readme.txt"]
    else:
        assert names == ["README.TXT", "Readme.txt"]
    assert report.ok


def test_case_clash_merges_folders_that_differ_by_case(tmp_path: Path) -> None:
    archive = write_raw_zip(
        tmp_path / "d.zip", [file_entry("Docs/a.txt", b"1"), file_entry("docs/b.txt", b"2")]
    )
    dest = tmp_path / "dest"
    with ZipReader(archive) as reader:
        extract_reader(reader, dest, ExtractOptions(), case_insensitive=True)
    assert sorted(p.name for p in dest.iterdir()) == ["Docs"]
    assert sorted(p.name for p in (dest / "Docs").iterdir()) == ["a.txt", "b.txt"]


DUPLICATES = [
    (ConflictAction.OVERWRITE, ["dup.txt"], b"second"),
    (ConflictAction.SKIP, ["dup.txt"], b"first"),
    (ConflictAction.RENAME, ["dup (2).txt", "dup.txt"], b"first"),
]


@pytest.mark.parametrize(("action", "names", "winner"), DUPLICATES, ids=lambda v: str(v)[:20])
def test_duplicate_names_follow_the_conflict_policy(
    tmp_path: Path, action: ConflictAction, names: list[str], winner: bytes
) -> None:
    report = run(
        tmp_path,
        [file_entry("dup.txt", b"first"), file_entry("dup.txt", b"second")],
        conflict=action,
    )
    assert sorted(p.name for p in (tmp_path / "dest").iterdir()) == names
    assert (tmp_path / "dest" / "dup.txt").read_bytes() == winner
    assert report.entries_done == (2 if action is not ConflictAction.SKIP else 1)


# --- cleanup --------------------------------------------------------------------------------


def test_cancel_mid_extraction_leaves_no_partial_files(tmp_path: Path) -> None:
    archive = write_raw_zip(
        tmp_path / "c.zip",
        [file_entry("first.txt", b"1" * 5000), file_entry("second.txt", b"2" * 5000)],
    )
    ctx, _, _ = stepping_context("second.txt")
    from zipwise.errors import Cancelled

    with pytest.raises(Cancelled):
        extract_archive(archive, tmp_path / "dest", ExtractOptions(), ctx)
    files = sorted(
        p.name
        for p in (tmp_path / "dest").rglob("*")
        if p.is_file() and not p.name.endswith(".zipwise-resume.json")
    )
    assert files == ["first.txt"]  # the journal for --resume is the only other file


def test_safety_violation_is_a_zipwise_error_with_exit_code_5() -> None:
    assert SafetyViolation("x").exit_code == 5


def test_entry_types_in_hostile_archive_are_classified(tmp_path: Path) -> None:
    archive = write_raw_zip(
        tmp_path / "t.zip",
        [symlink_entry("l", "x"), dir_entry("d"), special_entry("s"), file_entry("f")],
    )
    with ZipReader(archive) as reader:
        types = {e.name: e.type for e in reader.entries()}
    assert types == {
        "l": EntryType.SYMLINK,
        "d": EntryType.DIR,
        "s": EntryType.OTHER,
        "f": EntryType.FILE,
    }
