"""Creating over an older split set must not leave that set's files behind."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers.trees import make_tree, random_bytes
from zipwise.cli.main import main
from zipwise.errors import UsageError
from zipwise.model import CreateOptions
from zipwise.ops import create_archive, list_archive
from zipwise.split import MANIFEST_SUFFIX, NOTES_NAME, existing_set, find_parts, recipient_notes


def _tree(root: Path, size: int) -> Path:
    return make_tree(root, {"data.bin": random_bytes(5, size)})


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


@pytest.fixture
def old_set(tmp_path: Path) -> Path:
    """A three-part set ``out/s.zip.001`` ... with manifest and notes."""
    out = tmp_path / "out"
    big = _tree(tmp_path / "big", 30_000)
    create_archive([big], out / "s.zip", CreateOptions(level=0, split_size=11_000))
    assert _names(out) == [
        NOTES_NAME,
        "s.zip.001",
        "s.zip.002",
        "s.zip.003",
        "s.zip.parts.json",
    ]
    return out


def test_old_three_part_set_replaced_by_a_single_archive(old_set: Path, tmp_path: Path) -> None:
    small = _tree(tmp_path / "small", 200)
    report = create_archive([small], old_set / "s.zip", CreateOptions(split_size=1_000_000))
    assert [i.code for i in report.issues] == ["split-not-needed"]
    assert _names(old_set) == ["s.zip"]
    assert [e.name for e in list_archive(old_set / "s.zip")] == ["small", "small/data.bin"]


def test_old_three_part_set_replaced_by_a_two_part_set(old_set: Path, tmp_path: Path) -> None:
    medium = _tree(tmp_path / "medium", 18_000)
    create_archive([medium], old_set / "s.zip", CreateOptions(level=0, split_size=11_000))
    assert _names(old_set) == [NOTES_NAME, "s.zip.001", "s.zip.002", "s.zip.parts.json"]
    parts = find_parts(old_set / "s.zip.001")
    assert len(parts.parts) == 2 and parts.manifest is not None
    assert [e.name for e in list_archive(old_set / "s.zip.001")] == ["medium", "medium/data.bin"]


def test_unrelated_files_with_similar_names_are_untouched(old_set: Path, tmp_path: Path) -> None:
    bystanders = [
        "s.zip.txt",
        "s.zip.001.bak",
        "s.zip.01",
        "s.zip.1",
        "s2.zip.001",
        "t.zip.001",
        "t.zip" + MANIFEST_SUFFIX,
        "s.zip.partial.old",
        "notes.txt",
    ]
    for name in bystanders:
        (old_set / name).write_bytes(b"keep me")
    small = _tree(tmp_path / "small", 200)
    create_archive([small], old_set / "s.zip", CreateOptions(split_size=1_000_000))
    assert _names(old_set) == sorted([*bystanders, "s.zip"])
    assert all((old_set / n).read_bytes() == b"keep me" for n in bystanders)


def test_notes_written_for_another_file_are_kept(old_set: Path, tmp_path: Path) -> None:
    other = [old_set / "other.zip.001", old_set / "other.zip.002"]
    for part in other:
        part.write_bytes(b"x")
    recipient_notes(other, "other.zip")  # the single notes file now belongs to "other.zip"
    small = _tree(tmp_path / "small", 200)
    create_archive([small], old_set / "s.zip", CreateOptions(split_size=1_000_000))
    assert (old_set / NOTES_NAME).is_file()
    assert "other.zip" in (old_set / NOTES_NAME).read_text(encoding="utf-8")
    assert not list(old_set.glob("s.zip.0*")) and not (old_set / "s.zip.parts.json").exists()


def test_a_failed_create_keeps_the_old_set(old_set: Path, tmp_path: Path) -> None:
    before = _names(old_set)
    with pytest.raises(UsageError):
        create_archive([tmp_path / "nothing"], old_set / "s.zip", CreateOptions(split_size=5_000))
    assert _names(old_set) == before


def test_existing_set_finds_only_a_real_set(tmp_path: Path, old_set: Path) -> None:
    found = existing_set(old_set / "s.zip")
    assert found is not None and len(found.parts) == 3
    assert existing_set(tmp_path / "none.zip") is None
    (old_set / "s.zip.002").unlink()
    assert existing_set(old_set / "s.zip") is None  # a gap: not identifiable, so never deleted


def test_cli_force_with_split_removes_old_parts(old_set: Path, tmp_path: Path) -> None:
    small = _tree(tmp_path / "small", 200)
    assert main(["create", "--split", "1M", str(old_set / "s.zip"), str(small)]) == 2
    assert (old_set / "s.zip.003").is_file()  # without --force nothing is touched
    assert main(["create", "--force", "--split", "1M", str(old_set / "s.zip"), str(small)]) == 0
    assert _names(old_set) == ["s.zip"]
