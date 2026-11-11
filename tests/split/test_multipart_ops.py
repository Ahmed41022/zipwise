"""Create with a split size, and list / test / extract / info straight from ``.001`` parts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from helpers.builders import file_entry, write_raw_zip
from helpers.jobs import stepping_context
from helpers.names import ARABIC, extracted_files, legacy_zip
from helpers.trees import SHAPES, make_tree, random_bytes, snapshot
from zipwise import registry
from zipwise.errors import BackendUnavailable, Cancelled, CorruptArchive, UsageError
from zipwise.model import CreateOptions, ExtractOptions, FormatId
from zipwise.ops import archive_info, create_archive, extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive
from zipwise.ops._input import open_input_reader
from zipwise.split import (
    MANIFEST_SUFFIX,
    NOTES_NAME,
    MultiPartFile,
    find_parts,
    join_parts,
    split_file,
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return make_tree(tmp_path / "src" / "tree", SHAPES["mixed"])


@pytest.fixture
def parts(tmp_path: Path, tree: Path) -> list[Path]:
    out = tmp_path / "out"
    create_archive([tree], out / "t.zip", CreateOptions(split_size=20_000))
    return sorted(out.glob("t.zip.0*"))


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


# --- create with split ----------------------------------------------------------------------


def test_create_with_split_lists_parts_manifest_and_notes(tmp_path: Path, tree: Path) -> None:
    out = tmp_path / "out"
    report = create_archive([tree], out / "t.zip", CreateOptions(split_size=20_000))
    on_disk = listing(out)
    assert "t.zip" not in on_disk  # the whole archive is gone
    assert not any(n.endswith(".partial") or n.endswith(".zwpart") for n in on_disk)
    assert on_disk[0] == "HOW-TO-OPEN.txt" and "t.zip.parts.json" in on_disk
    part_names = [n for n in on_disk if n.startswith("t.zip.0")]
    assert len(part_names) >= 3
    assert [p.name for p in report.output_paths] == [*part_names, "t.zip.parts.json", NOTES_NAME]
    assert report.ok and report.exit_code == 0


def test_every_part_but_the_last_has_the_requested_size(parts: list[Path]) -> None:
    assert all(p.stat().st_size == 20_000 for p in parts[:-1])
    assert 0 < parts[-1].stat().st_size <= 20_000


def test_joined_parts_are_a_valid_archive(tmp_path: Path, parts: list[Path]) -> None:
    whole = join_parts(parts[0], tmp_path / "whole.zip")
    assert verify_archive(whole).ok


def test_manifest_and_notes_sit_next_to_the_parts(parts: list[Path]) -> None:
    folder = parts[0].parent
    assert (folder / ("t.zip" + MANIFEST_SUFFIX)).is_file()
    notes = (folder / NOTES_NAME).read_text(encoding="utf-8-sig")
    assert "t.zip.001" in notes and "طريقة فتح" in notes and "HOW TO OPEN" in notes


def test_split_larger_than_the_archive_keeps_it_whole(tmp_path: Path, tree: Path) -> None:
    out = tmp_path / "out"
    report = create_archive([tree], out / "t.zip", CreateOptions(split_size=10**9))
    assert report.output_paths == (out / "t.zip",)
    assert listing(out) == ["t.zip"]  # no parts, manifest or notes
    assert [(i.severity, i.code) for i in report.issues] == [("info", "split-not-needed")]
    assert report.ok and report.exit_code == 0
    assert verify_archive(out / "t.zip").ok


def test_split_exactly_the_archive_size_keeps_it_whole(tmp_path: Path, tree: Path) -> None:
    whole = create_archive([tree], tmp_path / "w.zip").output_paths[0]
    size = whole.stat().st_size
    out = tmp_path / "out"
    report = create_archive([tree], out / "t.zip", CreateOptions(split_size=size))
    assert listing(out) == ["t.zip"] and report.issues[0].code == "split-not-needed"
    bigger = create_archive([tree], out / "u.zip", CreateOptions(split_size=size - 1))
    assert not bigger.issues and (out / "u.zip.002").is_file()


def test_create_without_verify_can_still_split(tmp_path: Path, tree: Path) -> None:
    create_archive([tree], tmp_path / "t.zip", CreateOptions(split_size=30_000, verify=False))
    assert (tmp_path / "t.zip.001").exists() and not (tmp_path / "t.zip").exists()


def test_splitting_does_not_touch_an_older_whole_archive(tmp_path: Path, tree: Path) -> None:
    old = tmp_path / "t.zip"
    old.write_bytes(b"old whole file")
    create_archive([tree], old, CreateOptions(split_size=30_000))
    assert old.read_bytes() == b"old whole file"
    assert (tmp_path / "t.zip.001").exists()


def test_split_phase_is_reported(tmp_path: Path, tree: Path) -> None:
    ctx, _, events = stepping_context()
    create_archive([tree], tmp_path / "t.zip", CreateOptions(split_size=30_000), ctx)
    phases = [e.phase for e in events]
    assert phases.index("verify") < phases.index("split")
    last = [e for e in events if e.phase == "split"][-1]
    assert last.bytes_done == last.bytes_total > 0


def test_cancel_during_split_leaves_no_parts_and_no_archive(tmp_path: Path, tree: Path) -> None:
    out = tmp_path / "out"
    ctx, _, _ = stepping_context("t.zip.002")
    with pytest.raises(Cancelled):
        create_archive([tree], out / "t.zip", CreateOptions(split_size=20_000), ctx)
    assert listing(out) == []


def test_cancel_during_compression_leaves_nothing(tmp_path: Path, tree: Path) -> None:
    out = tmp_path / "out"
    ctx, _, _ = stepping_context("tree/random.bin")
    with pytest.raises(Cancelled):
        create_archive([tree], out / "t.zip", CreateOptions(split_size=20_000), ctx)
    assert listing(out) == []


def test_failure_in_the_notes_step_removes_the_parts(
    tmp_path: Path, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zipwise.ops import create as create_mod

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("cannot write notes")

    monkeypatch.setattr(create_mod, "recipient_notes", boom)
    out = tmp_path / "out"
    with pytest.raises(RuntimeError):
        create_archive([tree], out / "t.zip", CreateOptions(split_size=20_000))
    assert listing(out) == []


def test_free_space_check_happens_before_splitting(
    tmp_path: Path, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zipwise import split as split_mod
    from zipwise.errors import IOFailure

    monkeypatch.setattr(split_mod, "free_space", lambda path: 10)
    out = tmp_path / "out"
    with pytest.raises(IOFailure) as info:
        create_archive([tree], out / "t.zip", CreateOptions(split_size=20_000))
    assert info.value.code == "disk-full"
    assert listing(out) == []


# --- reading directly from the parts --------------------------------------------------------


def test_list_from_the_first_part(tmp_path: Path, tree: Path, parts: list[Path]) -> None:
    whole = tmp_path / "ref.zip"
    create_archive([tree], whole)
    assert [e.name for e in list_archive(parts[0])] == [e.name for e in list_archive(whole)]


@pytest.mark.parametrize("which", [0, 1, -1])
def test_any_part_opens_the_whole_set(parts: list[Path], which: int) -> None:
    assert len(list_archive(parts[which])) == len(list_archive(parts[0]))


def test_test_from_parts(parts: list[Path]) -> None:
    report = verify_archive(parts[0])
    assert report.ok and report.entries_skipped == 0 and report.entries_done > 0


def test_extract_from_parts_equals_extract_from_the_whole(
    tmp_path: Path, tree: Path, parts: list[Path]
) -> None:
    whole = tmp_path / "ref.zip"
    create_archive([tree], whole)
    extract_archive(parts[0], tmp_path / "a")
    extract_archive(whole, tmp_path / "b")
    assert snapshot(tmp_path / "a" / "tree") == snapshot(tmp_path / "b" / "tree")
    assert (tmp_path / "a" / "tree" / "void").is_dir()


def test_info_from_parts_is_flagged_multipart(parts: list[Path]) -> None:
    info = archive_info(parts[0])
    assert info.multipart is True and info.format is FormatId.ZIP
    assert info.entries_count == len(list_archive(parts[0]))


def test_info_of_a_plain_archive_is_not_multipart(tmp_path: Path, tree: Path) -> None:
    create_archive([tree], tmp_path / "t.zip")
    assert archive_info(tmp_path / "t.zip").multipart is False


def test_reader_gets_the_parts_as_a_file_object(parts: list[Path]) -> None:
    with open_input_reader(parts[0]) as reader:
        assert isinstance(reader.source, MultiPartFile)
        assert reader.multipart is True
        assert reader.origin == parts[0]
    assert reader.source.closed  # the cleanup closed it with the reader


def test_a_failing_open_does_not_leak_the_file_object(tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip.001"
    junk.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
    with pytest.raises(CorruptArchive):
        list_archive(junk)


def test_missing_part_is_a_clear_error_for_every_operation(
    tmp_path: Path, parts: list[Path]
) -> None:
    parts[1].unlink()
    for call in (list_archive, archive_info, verify_archive):
        with pytest.raises(CorruptArchive, match=r"t\.zip\.002"):
            call(parts[0])
    with pytest.raises(CorruptArchive, match=r"t\.zip\.002"):
        extract_archive(parts[0], tmp_path / "d")


def test_missing_last_part_is_caught_through_the_manifest(
    tmp_path: Path, parts: list[Path]
) -> None:
    parts[-1].unlink()
    with pytest.raises(CorruptArchive, match=parts[-1].name):
        list_archive(parts[0])


def test_manifest_is_optional_when_reading(tmp_path: Path, parts: list[Path]) -> None:
    (parts[0].parent / "t.zip.parts.json").unlink()
    assert verify_archive(parts[0]).ok


def test_archive_that_merely_looks_like_a_part_is_opened_normally(
    tmp_path: Path, tree: Path
) -> None:
    odd = tmp_path / "backup.2024"
    create_archive([tree], tmp_path / "t.zip")
    shutil.copy(tmp_path / "t.zip", odd)
    assert verify_archive(odd).ok
    assert archive_info(odd).multipart is False


def test_a_lone_numbered_file_that_is_not_an_archive_names_the_missing_part(
    tmp_path: Path,
) -> None:
    (tmp_path / "x.zip.002").write_bytes(b"not an archive")
    with pytest.raises(CorruptArchive, match=r"x\.zip\.001"):
        list_archive(tmp_path / "x.zip.002")


def test_not_found_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        list_archive(tmp_path / "nothing.zip.001")


def test_legacy_names_inside_split_archives(tmp_path: Path) -> None:
    whole = legacy_zip(tmp_path / "ar.zip", ARABIC, "cp1256")
    split_file(whole, 200, keep_source=False)
    dest = tmp_path / "out"
    extract_archive(tmp_path / "ar.zip.001", dest)
    assert extracted_files(dest) == sorted(ARABIC)
    assert archive_info(tmp_path / "ar.zip.002").name_encoding == "cp1256"


def test_four_digit_part_sets_open_too(tmp_path: Path) -> None:
    whole = write_raw_zip(
        tmp_path / "t.zip", [file_entry("r.bin", random_bytes(5, 1000), method=0)]
    )
    parts = split_file(whole, 1, keep_source=False)  # one byte per part: over 999 parts
    assert len(parts) > 999 and parts[0].name == "t.zip.0001"
    assert verify_archive(parts[0]).ok
    assert find_parts(parts[500]).first == parts[0]


# --- formats whose backend does not take a file object --------------------------------------


def test_backends_without_source_support_get_a_temporary_joined_copy(
    tmp_path: Path, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    create_archive([tree], out / "t.zip", CreateOptions(split_size=20_000))
    monkeypatch.setattr("zipwise.ops._input.reader_accepts_source", lambda fmt: False)
    with open_input_reader(out / "t.zip.001") as reader:
        assert reader.source is None
        assert reader.path.name == "t.zip" and reader.path.parent != out
        assert reader.origin == out / "t.zip.001"
        temp = reader.path
        assert temp.exists()
        assert len(reader.entries()) > 0
    assert not temp.exists()  # removed with the reader
    extract_archive(out / "t.zip.001", tmp_path / "d")
    assert (tmp_path / "d" / "tree" / "text.txt").is_file()


def test_zip_reader_declares_the_source_parameter() -> None:
    assert registry.reader_accepts_source(FormatId.ZIP) is True


@pytest.mark.parametrize("fmt", [FormatId.SEVENZ, FormatId.TAR_GZ])
def test_other_formats_can_be_split_and_read_back(
    tmp_path: Path, tree: Path, fmt: FormatId
) -> None:
    out = tmp_path / "out"
    try:
        report = create_archive([tree], out / "a.bin", CreateOptions(format=fmt, split_size=15_000))
    except BackendUnavailable:
        pytest.skip(f"no {fmt.value} backend")
    first = report.output_paths[0]
    assert first.name == "a.bin.001"
    assert verify_archive(first).ok
    extract_archive(first, tmp_path / "d")
    assert (tmp_path / "d" / "tree" / "sub" / "deep" / "notes.md").is_file()


def test_extract_options_still_apply_to_parts(tmp_path: Path, parts: list[Path]) -> None:
    report = extract_archive(parts[0], tmp_path / "d", ExtractOptions(only=["tree/sub"]))
    assert report.ok
    assert extracted_files(tmp_path / "d") == ["tree/sub/archive.zip", "tree/sub/deep/notes.md"]
