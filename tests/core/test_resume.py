"""Resumable extraction: journal, fingerprint, skipping finished entries."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from helpers.builders import file_entry, write_raw_zip
from helpers.jobs import stepping_context
from helpers.names import extracted_files
from helpers.trees import random_bytes
from zipwise.errors import Cancelled, CorruptArchive
from zipwise.jobs import JobContext
from zipwise.model import ConflictAction, ExtractOptions
from zipwise.ops import extract_archive
from zipwise.ops import test_archive as verify_archive
from zipwise.resume import (
    JOURNAL_VERSION,
    SAMPLE_SIZE,
    Fingerprint,
    ResumeJournal,
    fingerprint_archive,
    journal_path,
)
from zipwise.safety import SafetyPolicy
from zipwise.split import split_file

NAMES = [f"file{i}.txt" for i in range(1, 7)]
PLAIN = SafetyPolicy(sanitize_windows_names="never")
RESUME = ExtractOptions(resume=True, on_conflict=ConflictAction.OVERWRITE, policy=PLAIN)
PLAIN_RUN = ExtractOptions(on_conflict=ConflictAction.OVERWRITE, policy=PLAIN)


def content(name: str) -> bytes:
    return random_bytes(sum(name.encode()), 4000)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    entries = [file_entry(n, content(n)) for n in NAMES] + [file_entry("sub/deep.bin", b"deep")]
    return write_raw_zip(tmp_path / "set.zip", entries)


def journal_for(dest: Path, name: str = "set.zip") -> Path:
    return journal_path(dest, name)


def interrupt(archive: Path, dest: Path, at: str) -> None:
    """Run an extraction that is cancelled when it reaches ``at``."""
    ctx, _, _ = stepping_context(at)
    with pytest.raises(Cancelled):
        extract_archive(archive, dest, PLAIN_RUN, ctx)


# --- the main scenario ----------------------------------------------------------------------


def test_cancel_leaves_finished_files_and_a_journal(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    assert extracted_files(dest) == ["file1.txt", "file2.txt", "file3.txt"]
    data = json.loads(journal_for(dest).read_text(encoding="utf-8"))
    assert data["version"] == JOURNAL_VERSION
    assert [e["name"] for e in data["entries"]] == ["file1.txt", "file2.txt", "file3.txt"]
    assert all(e["size"] == 4000 for e in data["entries"])
    assert not list(dest.glob("*.zwpart"))


def test_resume_skips_finished_entries_and_completes(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    # mark the finished files so that a rewrite would be noticed (same size, other bytes)
    for name in ("file1.txt", "file2.txt", "file3.txt"):
        (dest / name).write_bytes(b"#" * 4000)
    report = extract_archive(archive, dest, RESUME)
    assert report.ok
    for name in ("file1.txt", "file2.txt", "file3.txt"):
        assert (dest / name).read_bytes() == b"#" * 4000  # not rewritten
    for name in ("file4.txt", "file5.txt", "file6.txt"):
        assert (dest / name).read_bytes() == content(name)
    assert (dest / "sub" / "deep.bin").read_bytes() == b"deep"
    assert [i.code for i in report.issues] == ["resume-skipped"]
    assert report.entries_done == 7 and report.entries_skipped == 0


def test_resumed_result_equals_a_clean_extraction(tmp_path: Path, archive: Path) -> None:
    interrupted = tmp_path / "interrupted"
    interrupt(archive, interrupted, "file5.txt")
    extract_archive(archive, interrupted, RESUME)
    clean = tmp_path / "clean"
    extract_archive(archive, clean, PLAIN_RUN)
    assert extracted_files(interrupted) == extracted_files(clean)
    for rel in extracted_files(clean):
        assert (interrupted / rel).read_bytes() == (clean / rel).read_bytes()
        assert (interrupted / rel).stat().st_size == (clean / rel).stat().st_size


def test_journal_is_removed_after_success(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    assert journal_for(dest).exists()
    extract_archive(archive, dest, RESUME)
    assert not journal_for(dest).exists()
    assert not list(dest.glob("*.tmp"))
    assert sorted(p.name for p in dest.iterdir()) == [*NAMES, "sub"]


def test_a_clean_run_leaves_no_journal(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    extract_archive(archive, dest)
    assert not journal_for(dest).exists()


def test_finished_files_keep_their_inode_data(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file3.txt")
    os.utime(dest / "file1.txt", (1_000_000_000, 1_000_000_000))  # a rewrite would reset this
    extract_archive(archive, dest, RESUME)
    assert os.stat(dest / "file1.txt").st_mtime_ns == 1_000_000_000 * 10**9


def test_progress_counts_skipped_entries(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    ctx, _, events = stepping_context()
    extract_archive(archive, dest, RESUME, ctx)
    last = [e for e in events if e.phase == "extract"][-1]
    assert last.entries_done == last.entries_total == 7
    assert last.bytes_done == last.bytes_total


def test_stale_partial_files_are_overwritten(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    (dest / "file4.txt.zwpart").write_bytes(b"half a file from the crashed run")
    extract_archive(archive, dest, RESUME)
    assert (dest / "file4.txt").read_bytes() == content("file4.txt")
    assert not list(dest.glob("*.zwpart"))


def test_journalled_entry_whose_file_vanished_is_extracted_again(
    tmp_path: Path, archive: Path
) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    (dest / "file2.txt").unlink()
    report = extract_archive(archive, dest, RESUME)
    assert (dest / "file2.txt").read_bytes() == content("file2.txt")
    assert report.entries_done == 7


def test_journalled_entry_with_a_changed_size_is_extracted_again(
    tmp_path: Path, archive: Path
) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    (dest / "file2.txt").write_bytes(b"truncated")
    extract_archive(archive, dest, RESUME)
    assert (dest / "file2.txt").read_bytes() == content("file2.txt")


def test_resume_respects_the_conflict_policy_for_other_existing_files(
    tmp_path: Path, archive: Path
) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    (dest / "file5.txt").write_bytes(b"user data")  # not part of the interrupted run
    report = extract_archive(
        archive, dest, ExtractOptions(resume=True, on_conflict=ConflictAction.SKIP, policy=PLAIN)
    )
    assert (dest / "file5.txt").read_bytes() == b"user data"
    assert "skipped-existing" in [i.code for i in report.issues]


# --- journal handling -----------------------------------------------------------------------


def test_without_resume_an_existing_journal_is_ignored_and_replaced(
    tmp_path: Path, archive: Path
) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    (dest / "file1.txt").write_bytes(b"#" * 4000)
    report = extract_archive(archive, dest, PLAIN_RUN)  # no resume: everything is written again
    assert (dest / "file1.txt").read_bytes() == content("file1.txt")
    assert "resume-skipped" not in [i.code for i in report.issues]
    assert not journal_for(dest).exists()


def test_a_second_interruption_extends_the_journal(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file3.txt")
    ctx, _, _ = stepping_context("file5.txt")
    with pytest.raises(Cancelled):
        extract_archive(archive, dest, RESUME, ctx)
    names = [
        e["name"] for e in json.loads(journal_for(dest).read_text(encoding="utf-8"))["entries"]
    ]
    assert names == ["file1.txt", "file2.txt", "file3.txt", "file4.txt"]
    extract_archive(archive, dest, RESUME)
    assert not journal_for(dest).exists()


def test_journal_is_discarded_when_the_archive_changed(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    (dest / "file1.txt").write_bytes(b"#" * 4000)
    entries = [file_entry(n, b"new " + content(n)) for n in NAMES]
    write_raw_zip(archive, entries)  # same name, different archive
    report = extract_archive(archive, dest, RESUME)
    assert [i.code for i in report.issues] == ["resume-discarded"]
    assert report.issues[0].severity == "info"
    assert "changed" in report.issues[0].message
    for name in NAMES:
        assert (dest / name).read_bytes() == b"new " + content(name)
    assert not journal_for(dest).exists()


def test_journal_is_discarded_when_the_archive_changed_but_kept_its_size(
    tmp_path: Path, archive: Path
) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    entries = [file_entry(n, content(n), date_time=(2020, 1, 2, 3, 4, 6)) for n in NAMES]
    entries.append(file_entry("sub/deep.bin", b"deep"))
    before = archive.stat().st_size
    write_raw_zip(archive, entries)
    assert archive.stat().st_size == before  # only a timestamp differs
    report = extract_archive(archive, dest, RESUME)
    assert "resume-discarded" in [i.code for i in report.issues]


def test_unreadable_journal_is_discarded(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    journal_for(dest).write_text("{broken", encoding="utf-8")
    report = extract_archive(archive, dest, RESUME)
    assert [i.code for i in report.issues] == ["resume-discarded"]
    assert "unreadable" in report.issues[0].message
    assert (dest / "file1.txt").read_bytes() == content("file1.txt")


@pytest.mark.parametrize(
    "document",
    [
        {"version": 99, "archive": {}, "entries": []},
        {"version": JOURNAL_VERSION},
        [],
        {"version": JOURNAL_VERSION, "archive": {"size": "x"}, "entries": []},
    ],
)
def test_malformed_journals_are_discarded(tmp_path: Path, archive: Path, document: object) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    journal_for(dest).write_text(json.dumps(document), encoding="utf-8")
    report = extract_archive(archive, dest, RESUME)
    assert report.ok
    assert [i.code for i in report.issues] == ["resume-discarded"]


def test_resume_flag_without_any_journal_is_silent(tmp_path: Path, archive: Path) -> None:
    report = extract_archive(archive, tmp_path / "out", RESUME)
    assert report.issues == () and report.entries_done == 7


def test_journal_for_another_archive_name_is_not_used(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    (dest / "file1.txt").write_bytes(b"#" * 4000)
    other = tmp_path / "other.zip"
    other.write_bytes(archive.read_bytes())
    extract_archive(other, dest, RESUME)  # other.zip has no journal in dest
    assert (dest / "file1.txt").read_bytes() == content("file1.txt")
    assert journal_for(dest).exists()  # set.zip's own journal is still there


def test_error_mid_extraction_keeps_the_journal(tmp_path: Path) -> None:
    entries = [
        file_entry("ok1.txt", b"1" * 100),
        file_entry("ok2.txt", b"2" * 100),
        file_entry("bad.txt", b"x" * 500, declared_crc=1),
        file_entry("after.txt", b"3" * 100),
    ]
    archive = write_raw_zip(tmp_path / "bad.zip", entries)
    dest = tmp_path / "out"
    with pytest.raises(CorruptArchive):
        extract_archive(archive, dest, PLAIN_RUN)
    data = json.loads(journal_for(dest, "bad.zip").read_text(encoding="utf-8"))
    assert [e["name"] for e in data["entries"]] == ["ok1.txt", "ok2.txt"]


# --- multi-part input and fingerprint -------------------------------------------------------


def test_resume_works_for_multipart_archives(tmp_path: Path, archive: Path) -> None:
    parts = split_file(archive, 5000, keep_source=False)
    dest = tmp_path / "out"
    ctx, _, _ = stepping_context("file4.txt")
    with pytest.raises(Cancelled):
        extract_archive(parts[1], dest, PLAIN_RUN, ctx)  # any part opens the set
    assert journal_for(dest, "set.zip.001").exists()  # named after the first part
    (dest / "file1.txt").write_bytes(b"#" * 4000)
    extract_archive(parts[0], dest, RESUME)
    assert (dest / "file1.txt").read_bytes() == b"#" * 4000
    assert (dest / "file6.txt").read_bytes() == content("file6.txt")
    assert not journal_for(dest, "set.zip.001").exists()


def test_fingerprint_depends_on_size_and_both_ends(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    data = bytearray(random_bytes(1, 3 * SAMPLE_SIZE))
    f.write_bytes(bytes(data))
    base = fingerprint_archive(f)
    assert base.size == 3 * SAMPLE_SIZE
    for position in (0, SAMPLE_SIZE - 1, 3 * SAMPLE_SIZE - 1, 3 * SAMPLE_SIZE - SAMPLE_SIZE):
        changed = bytearray(data)
        changed[position] ^= 1
        f.write_bytes(bytes(changed))
        assert fingerprint_archive(f) != base, position
    middle = bytearray(data)
    middle[SAMPLE_SIZE + 5] ^= 1  # outside both sampled blocks: not part of the fingerprint
    f.write_bytes(bytes(middle))
    assert fingerprint_archive(f).head == base.head and fingerprint_archive(f).tail == base.tail


def test_fingerprint_of_a_part_set_equals_that_of_the_whole(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(random_bytes(2, 200_000))
    whole = fingerprint_archive(f)
    parts = split_file(f, 30_000)
    joined = fingerprint_archive(parts[0], multipart=True)
    assert (joined.size, joined.head, joined.tail) == (whole.size, whole.head, whole.tail)


def test_fingerprint_round_trips_through_a_dict() -> None:
    fp = Fingerprint(10, 20, "h", "t")
    assert Fingerprint.from_dict(fp.to_dict()) == fp
    assert Fingerprint.from_dict({"size": 1}) is None
    assert Fingerprint.from_dict("nope") is None


# --- ResumeJournal unit tests ---------------------------------------------------------------


FP = Fingerprint(1, 2, "a", "b")


def test_journal_flushes_every_n_entries(tmp_path: Path) -> None:
    journal = ResumeJournal(tmp_path / "j.json", FP, flush_every=3, flush_seconds=1e9)
    journal.mark_done(0, "a", 1)
    journal.mark_done(1, "b", 1)
    assert not (tmp_path / "j.json").exists()
    journal.mark_done(2, "c", 1)
    assert len(json.loads((tmp_path / "j.json").read_text(encoding="utf-8"))["entries"]) == 3


def test_journal_flushes_on_time(tmp_path: Path) -> None:
    now = [0.0]
    journal = ResumeJournal(
        tmp_path / "j.json", FP, clock=lambda: now[0], flush_every=1000, flush_seconds=2.0
    )
    journal.mark_done(0, "a", 1)
    assert not (tmp_path / "j.json").exists()
    now[0] = 2.5
    journal.mark_done(1, "b", 1)
    assert (tmp_path / "j.json").exists()


def test_explicit_flush_writes_pending_entries(tmp_path: Path) -> None:
    journal = ResumeJournal(tmp_path / "j.json", FP, flush_every=1000, flush_seconds=1e9)
    journal.mark_done(0, "a", 1)
    journal.flush()
    assert (tmp_path / "j.json").exists()
    assert not (tmp_path / "j.json.tmp").exists()  # written through a temp file and renamed


def test_flush_with_nothing_done_writes_nothing(tmp_path: Path) -> None:
    ResumeJournal(tmp_path / "j.json", FP).flush()
    assert not (tmp_path / "j.json").exists()


def test_is_done_checks_name_size_and_file(tmp_path: Path) -> None:
    journal = ResumeJournal(tmp_path / "j.json", FP)
    target = tmp_path / "f.txt"
    target.write_bytes(b"12345")
    journal.mark_done(3, "f.txt", 5)
    assert journal.is_done(3, "f.txt", 5, target)
    assert not journal.is_done(4, "f.txt", 5, target)  # other index
    assert not journal.is_done(3, "g.txt", 5, target)  # other name
    assert not journal.is_done(3, "f.txt", 6, target)  # other recorded size
    target.write_bytes(b"1234")
    assert not journal.is_done(3, "f.txt", 5, target)  # file changed
    target.unlink()
    assert not journal.is_done(3, "f.txt", 5, target)  # file gone


def test_is_done_rejects_directories(tmp_path: Path) -> None:
    journal = ResumeJournal(tmp_path / "j.json", FP)
    (tmp_path / "d").mkdir()
    journal.mark_done(0, "d", (tmp_path / "d").stat().st_size)
    assert not journal.is_done(0, "d", (tmp_path / "d").stat().st_size, tmp_path / "d")


def test_start_loads_a_matching_journal(tmp_path: Path) -> None:
    archive = tmp_path / "a.bin"
    archive.write_bytes(random_bytes(4, 5000))
    dest = tmp_path / "dest"
    dest.mkdir()
    first, _ = ResumeJournal.start(dest, archive, resume=True)
    first.mark_done(0, "x", 10)
    first.flush()
    second, issues = ResumeJournal.start(dest, archive, resume=True)
    assert issues == [] and second.completed == {0: ("x", 10)}
    third, issues = ResumeJournal.start(dest, archive, resume=False)
    assert issues == [] and third.completed == {}


def test_remove_deletes_the_journal(tmp_path: Path) -> None:
    journal = ResumeJournal(tmp_path / "j.json", FP, flush_every=1)
    journal.mark_done(0, "a", 1)
    assert (tmp_path / "j.json").exists()
    journal.remove()
    assert not (tmp_path / "j.json").exists()


def test_journal_name_is_hidden_and_tied_to_the_archive(tmp_path: Path) -> None:
    assert journal_path(tmp_path, "photos.zip").name == ".photos.zip.zipwise-resume.json"


def test_verify_still_works_after_a_resumed_extraction(tmp_path: Path, archive: Path) -> None:
    dest = tmp_path / "out"
    interrupt(archive, dest, "file4.txt")
    extract_archive(archive, dest, RESUME)
    assert verify_archive(archive).ok
    assert JobContext().bytes_done == 0
