from __future__ import annotations

import errno
import os
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

from helpers.builders import file_entry, write_raw_zip
from helpers.jobs import stepping_context
from helpers.roundtrip import POSIX
from helpers.trees import BASE_MTIME, SHAPES, make_tree, snapshot
from zipwise import safety
from zipwise.errors import (
    Cancelled,
    CorruptArchive,
    IOFailure,
    UnsupportedFormat,
    UsageError,
)
from zipwise.jobs import JobContext
from zipwise.model import ConflictAction, CreateOptions, ExtractOptions
from zipwise.ops import archive_info, create_archive, extract_archive, list_archive
from zipwise.ops import extract as extract_mod
from zipwise.ops import test_archive as verify_archive
from zipwise.safety import SafetyPolicy

NO_NAMES = SafetyPolicy(sanitize_windows_names="never")


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["nested"])
    out = tmp_path / "tree.zip"
    create_archive([tree], out)
    return out


def tree_files(root: Path) -> list[str]:
    """Extracted files; the resume journal (a dot file) is not part of the content."""
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(".zipwise-resume.json")
    )


# --- conflicts ------------------------------------------------------------------------------


@pytest.fixture
def existing(tmp_path: Path, archive: Path) -> Path:
    dest = tmp_path / "dest"
    (dest / "tree").mkdir(parents=True)
    (dest / "tree" / "top.txt").write_bytes(b"old content")
    return dest


def test_conflict_skip_keeps_the_old_file_and_warns(archive: Path, existing: Path) -> None:
    report = extract_archive(archive, existing, ExtractOptions(on_conflict=ConflictAction.SKIP))
    assert (existing / "tree" / "top.txt").read_bytes() == b"old content"
    assert [i.code for i in report.issues] == ["skipped-existing"]
    assert report.entries_skipped == 1
    assert int(report.exit_code) == 1


def test_conflict_rename_keeps_both(archive: Path, existing: Path) -> None:
    report = extract_archive(archive, existing, ExtractOptions(on_conflict=ConflictAction.RENAME))
    assert (existing / "tree" / "top.txt").read_bytes() == b"old content"
    assert (existing / "tree" / "top (2).txt").read_bytes() == b"top"
    assert [i.code for i in report.issues] == ["renamed-existing"]
    assert int(report.exit_code) == 0  # a rename is informational


def test_conflict_overwrite_replaces(archive: Path, existing: Path) -> None:
    report = extract_archive(
        archive, existing, ExtractOptions(on_conflict=ConflictAction.OVERWRITE)
    )
    assert (existing / "tree" / "top.txt").read_bytes() == b"top"
    assert report.issues == ()


def test_conflict_overwrite_clears_the_read_only_attribute(archive: Path, existing: Path) -> None:
    os.chmod(existing / "tree" / "top.txt", stat.S_IREAD)
    extract_archive(archive, existing, ExtractOptions(on_conflict=ConflictAction.OVERWRITE))
    assert (existing / "tree" / "top.txt").read_bytes() == b"top"


def test_conflict_ask_without_a_callback_skips(archive: Path, existing: Path) -> None:
    extract_archive(archive, existing, ExtractOptions(on_conflict=ConflictAction.ASK))
    assert (existing / "tree" / "top.txt").read_bytes() == b"old content"


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        (ConflictAction.OVERWRITE, b"top"),
        (ConflictAction.SKIP, b"old content"),
    ],
)
def test_conflict_ask_uses_the_callback(
    archive: Path, existing: Path, choice: ConflictAction, expected: bytes
) -> None:
    asked: list[Path] = []

    def callback(path: Path) -> tuple[ConflictAction, bool]:
        asked.append(path)
        return choice, False

    ctx = JobContext(resolve_conflict=callback)
    extract_archive(archive, existing, ExtractOptions(on_conflict=ConflictAction.ASK), ctx)
    assert [p.name for p in asked] == ["top.txt"]
    assert (existing / "tree" / "top.txt").read_bytes() == expected


def test_conflict_ask_remembers_apply_to_all(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "dest"
    extract_archive(archive, dest)  # first extraction: no conflicts
    asked = 0

    def callback(path: Path) -> tuple[ConflictAction, bool]:
        nonlocal asked
        asked += 1
        return ConflictAction.RENAME, True

    ctx = JobContext(resolve_conflict=callback)
    report = extract_archive(archive, dest, ExtractOptions(on_conflict=ConflictAction.ASK), ctx)
    assert asked == 1
    assert report.entries_done == 8  # 4 renamed files + 4 directories merged
    assert (dest / "tree" / "d1" / "d2" / "two (2).txt").exists()


def test_folder_in_the_way_of_a_file_is_reported(tmp_path: Path) -> None:
    archive = write_raw_zip(tmp_path / "a.zip", [file_entry("name", b"x")])
    dest = tmp_path / "dest"
    (dest / "name").mkdir(parents=True)
    report = extract_archive(archive, dest, ExtractOptions(on_conflict=ConflictAction.OVERWRITE))
    assert [i.code for i in report.issues] == ["dir-in-the-way"]
    assert (dest / "name").is_dir()


# --- selection ------------------------------------------------------------------------------


def test_only_extracts_a_subtree(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "dest"
    extract_archive(archive, dest, ExtractOptions(only=["tree/d1/d2"]))
    assert tree_files(dest) == ["tree/d1/d2/d3/three.bin", "tree/d1/d2/two.txt"]


def test_only_accepts_several_names_and_backslashes(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "dest"
    extract_archive(archive, dest, ExtractOptions(only=["tree\\top.txt", "tree/d1/one.txt"]))
    assert tree_files(dest) == ["tree/d1/one.txt", "tree/top.txt"]


def test_only_does_not_match_name_prefixes(tmp_path: Path, archive: Path) -> None:
    with pytest.raises(UsageError):
        extract_archive(archive, tmp_path / "d", ExtractOptions(only=["tree/to"]))


def test_only_with_no_match_is_a_usage_error(tmp_path: Path, archive: Path) -> None:
    with pytest.raises(UsageError, match="None of the requested"):
        extract_archive(archive, tmp_path / "d", ExtractOptions(only=["nothing"]))


@pytest.mark.parametrize("strip", [1, 2])
def test_strip_components(tmp_path: Path, archive: Path, strip: int) -> None:
    dest = tmp_path / "dest"
    report = extract_archive(archive, dest, ExtractOptions(strip_components=strip))
    assert report.ok
    expected = {
        1: ["d1/d2/d3/three.bin", "d1/d2/two.txt", "d1/one.txt", "top.txt"],
        2: ["d2/d3/three.bin", "d2/two.txt", "one.txt"],
    }[strip]
    assert tree_files(dest) == sorted(expected)


def test_destination_is_created_with_parents(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "x" / "y" / "z"
    extract_archive(archive, dest)
    assert (dest / "tree" / "top.txt").exists()


def test_destination_that_is_a_file_is_a_usage_error(tmp_path: Path, archive: Path) -> None:
    target = tmp_path / "file"
    target.write_text("x")
    with pytest.raises(UsageError):
        extract_archive(archive, target)


# --- metadata -------------------------------------------------------------------------------


def test_mtimes_of_files_and_directories_are_restored(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["nested"])
    out = tmp_path / "t.zip"
    create_archive([tree], out)
    dest = tmp_path / "dest"
    extract_archive(out, dest)
    before, after = snapshot(tree), snapshot(dest / "tree")
    for rel, (_, _, mtime, _) in before.items():
        assert abs(after[rel][2] - mtime) < 1.5, rel


def test_directory_mtimes_survive_extraction_of_their_children(tmp_path: Path) -> None:
    out = tmp_path / "t.zip"
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["nested"])
    os.utime(tree / "d1", (BASE_MTIME - 1000, BASE_MTIME - 1000))
    create_archive([tree], out)
    extract_archive(out, tmp_path / "dest")
    assert abs((tmp_path / "dest" / "tree" / "d1").stat().st_mtime - (BASE_MTIME - 1000)) < 1.5


def test_modes_are_restored_masked_and_can_be_disabled(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "tree", {"run.sh": b"#!/bin/sh", "d/": None})
    if POSIX:
        os.chmod(tree / "run.sh", 0o750)
        os.chmod(tree / "d", 0o700)
    out = tmp_path / "t.zip"
    create_archive([tree], out)
    on, off = tmp_path / "on", tmp_path / "off"
    extract_archive(out, on)
    extract_archive(out, off, ExtractOptions(restore_mode=False))
    assert (on / "tree" / "run.sh").read_bytes() == b"#!/bin/sh"
    assert (off / "tree" / "run.sh").read_bytes() == b"#!/bin/sh"
    if POSIX:  # Windows cannot hold POSIX permission bits
        assert stat.S_IMODE((on / "tree" / "run.sh").stat().st_mode) == 0o750
        assert stat.S_IMODE((on / "tree" / "d").stat().st_mode) == 0o700
        assert stat.S_IMODE((off / "tree" / "run.sh").stat().st_mode) != 0o750


def test_setuid_and_sticky_bits_are_dropped(tmp_path: Path) -> None:
    from helpers.builders import RawEntry

    entry = RawEntry("x", b"x", external_attr=(0o104755 << 16))
    out = write_raw_zip(tmp_path / "s.zip", [entry])
    extract_archive(out, tmp_path / "dest")
    assert (tmp_path / "dest" / "x").read_bytes() == b"x"
    if POSIX:
        assert stat.S_IMODE((tmp_path / "dest" / "x").stat().st_mode) == 0o755


def test_read_only_directory_does_not_block_its_own_contents(tmp_path: Path) -> None:
    from helpers.builders import RawEntry

    entries = [
        RawEntry("ro/", b"", method=0, external_attr=(0o040555 << 16) | 0x10),
        file_entry("ro/inner.txt", b"x"),
    ]
    out = write_raw_zip(tmp_path / "r.zip", entries)
    dest = tmp_path / "dest"
    report = extract_archive(out, dest)
    try:
        assert report.ok
        assert (dest / "ro" / "inner.txt").read_bytes() == b"x"
    finally:
        os.chmod(dest / "ro", 0o755)


# --- progress, cleanup and failure ----------------------------------------------------------


def test_progress_covers_the_whole_extraction(tmp_path: Path, archive: Path) -> None:
    ctx, _, events = stepping_context()
    report = extract_archive(archive, tmp_path / "dest", ctx=ctx)
    last = events[-1]
    assert last.phase == "extract"
    assert last.bytes_done == last.bytes_total == report.bytes_done
    assert last.entries_done == last.entries_total == report.entries_done


def test_cancel_before_start_writes_nothing(tmp_path: Path, archive: Path) -> None:
    ctx = JobContext()
    ctx.cancel.cancel()
    with pytest.raises(Cancelled):
        extract_archive(archive, tmp_path / "dest", ctx=ctx)
    assert tree_files(tmp_path / "dest") == []


def test_cancel_in_the_middle_keeps_finished_files_only(tmp_path: Path, archive: Path) -> None:
    ctx, _, _ = stepping_context(cancel_on="tree/d1/d2/two.txt")
    dest = tmp_path / "dest"
    with pytest.raises(Cancelled):
        extract_archive(archive, dest, ctx=ctx)
    assert not [p for p in dest.rglob("*") if p.name.endswith((".zwpart", ".partial"))]
    assert (dest / "tree" / "d1" / "d2" / "d3" / "three.bin").exists()
    assert not (dest / "tree" / "d1" / "d2" / "two.txt").exists()


def test_disk_full_while_writing_cleans_up_and_reports_io_failure(
    tmp_path: Path, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full_disk(self: extract_mod._OutFile, data: bytes) -> int:
        raise extract_mod.translate_oserror(OSError(errno.ENOSPC, "No space left on device"))

    monkeypatch.setattr(extract_mod._OutFile, "write", full_disk)
    dest = tmp_path / "dest"
    with pytest.raises(IOFailure) as info:
        extract_archive(archive, dest)
    assert info.value.code == "disk-full"
    assert info.value.exit_code == 7
    assert not [p for p in dest.rglob("*") if p.is_file()]


def test_declared_size_larger_than_free_space_fails_before_writing(
    tmp_path: Path, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(safety.fsutil, "free_space", lambda path: 10)
    dest = tmp_path / "dest"
    with pytest.raises(IOFailure) as info:
        extract_archive(archive, dest)
    assert info.value.code == "disk-full"
    assert not dest.exists()


def test_corrupt_entry_aborts_and_removes_its_partial_file(tmp_path: Path) -> None:
    out = write_raw_zip(
        tmp_path / "c.zip",
        [file_entry("good.txt", b"fine"), file_entry("bad.txt", b"x" * 100, declared_crc=1)],
    )
    dest = tmp_path / "dest"
    with pytest.raises(CorruptArchive):
        extract_archive(out, dest)
    assert tree_files(dest) == ["good.txt"]


def test_resume_without_a_journal_is_a_plain_extraction(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "dest"
    report = extract_archive(archive, dest, ExtractOptions(resume=True))
    assert report.ok
    assert len(tree_files(dest)) == 4
    assert not [p for p in dest.iterdir() if p.name.startswith(".")]  # no journal left


@pytest.mark.parametrize("op", ["extract", "list", "info", "test"])
def test_multipart_input_works_for_every_operation(tmp_path: Path, archive: Path, op: str) -> None:
    part = tmp_path / "tree.zip.001"
    shutil.copy(archive, part)
    calls = {
        "extract": lambda: extract_archive(part, tmp_path / "d"),
        "list": lambda: list_archive(part),
        "info": lambda: archive_info(part),
        "test": lambda: verify_archive(part),
    }
    result = calls[op]()
    if op == "info":
        assert result.multipart is True  # type: ignore[union-attr]
    elif op == "list":
        assert len(result) > 0  # type: ignore[arg-type]
    else:
        assert result.ok  # type: ignore[union-attr]


def test_long_destination_paths_work(tmp_path: Path) -> None:
    deep = "/".join(["d" * 60] * 5 + ["file.txt"])
    out = write_raw_zip(tmp_path / "deep.zip", [file_entry(deep, b"deep")])
    dest = tmp_path / "dest"
    report = extract_archive(out, dest, ExtractOptions(policy=NO_NAMES))
    assert report.ok, report.issues
    from zipwise.fsutil import long_path

    assert len(str(dest / deep)) > 300
    assert long_path(dest / deep).read_bytes() == b"deep"


# --- list / info / test ---------------------------------------------------------------------


def test_list_returns_entries_in_archive_order(archive: Path) -> None:
    names = [e.name for e in list_archive(archive)]
    assert names == [
        "tree",
        "tree/d1",
        "tree/d1/d2",
        "tree/d1/d2/d3",
        "tree/d1/d2/d3/three.bin",
        "tree/d1/d2/two.txt",
        "tree/d1/one.txt",
        "tree/top.txt",
    ]


def test_list_reports_sizes_types_and_indices(archive: Path) -> None:
    entries = {e.name: e for e in list_archive(archive)}
    assert entries["tree/top.txt"].size == 3
    assert entries["tree/top.txt"].type.value == "file"
    assert entries["tree/d1"].type.value == "dir"
    assert [e.index for e in list_archive(archive)] == list(range(8))


def test_info_summarises_the_archive(archive: Path) -> None:
    info = archive_info(archive)
    assert info.entries_count == 8
    assert info.total_size == 3 + 2000 + 3000 + 4096
    assert info.format.value == "zip"
    assert info.encrypted is False
    assert info.multipart is False
    assert info.name_encoding in ("ascii", "utf-8")


def test_test_archive_passes_for_a_good_archive(archive: Path) -> None:
    report = verify_archive(archive)
    assert report.ok and report.entries_done == 8 and report.entries_skipped == 0
    assert report.bytes_done == 3 + 2000 + 3000 + 4096


def test_test_archive_flags_a_flipped_byte(tmp_path: Path) -> None:
    out = tmp_path / "t.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("a.txt", b"A" * 200)
        zf.writestr("b.txt", b"B" * 200)
    raw = bytearray(out.read_bytes())
    raw[raw.index(b"BBBB") + 10] ^= 0xFF
    out.write_bytes(bytes(raw))
    report = verify_archive(out)
    assert not report.ok
    assert [i.entry for i in report.issues] == ["b.txt"]
    assert report.entries_skipped == 1


def test_unrecognised_file_is_unsupported(tmp_path: Path) -> None:
    bogus = tmp_path / "x.zip"
    bogus.write_bytes(b"this is not an archive at all")
    for call in (list_archive, archive_info, verify_archive):
        with pytest.raises(UnsupportedFormat):
            call(bogus)


def test_options_object_is_not_mutated_by_the_pipeline(tmp_path: Path, archive: Path) -> None:
    options = ExtractOptions(only=["tree/top.txt"], policy=SafetyPolicy())
    extract_archive(archive, tmp_path / "d", options)
    assert options.only == ["tree/top.txt"]
    assert CreateOptions().password is None
