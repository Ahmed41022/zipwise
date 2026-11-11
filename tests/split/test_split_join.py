from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from helpers.jobs import stepping_context
from helpers.trees import random_bytes
from zipwise.errors import CorruptArchive, UsageError
from zipwise.fsutil import CHUNK_SIZE
from zipwise.jobs import JobContext
from zipwise.split import (
    MANIFEST_SUFFIX,
    find_parts,
    join_parts,
    manifest_name,
    read_manifest,
    split_file,
    verify_parts,
)


def make_file(folder: Path, size: int, name: str = "big.bin", seed: int = 1) -> Path:
    path = folder / name
    path.write_bytes(random_bytes(seed, size))
    return path


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


@pytest.mark.parametrize(
    ("size", "part", "count"),
    [
        (1000, 100, 10),  # exact multiple
        (1001, 100, 11),  # one byte over
        (999, 100, 10),  # one byte under
        (50, 100, 1),  # smaller than one part
        (100, 100, 1),  # exactly one part
        (101, 100, 2),
        (1, 1, 1),
        (7, 3, 3),
        (0, 100, 1),  # empty file: one empty part
    ],
)
def test_split_then_join_is_identity(tmp_path: Path, size: int, part: int, count: int) -> None:
    src = make_file(tmp_path, size)
    original = src.read_bytes()
    parts = split_file(src, part)
    assert len(parts) == count
    assert [p.name for p in parts] == [f"big.bin.{i:03d}" for i in range(1, count + 1)]
    assert all(p.stat().st_size == part for p in parts[:-1])
    assert sum(p.stat().st_size for p in parts) == size
    out = join_parts(parts[0], tmp_path / "joined.bin")
    assert out.read_bytes() == original


def test_the_source_is_kept_by_default(tmp_path: Path) -> None:
    src = make_file(tmp_path, 500)
    split_file(src, 200)
    assert src.exists()


def test_keep_source_false_removes_it(tmp_path: Path) -> None:
    src = make_file(tmp_path, 500)
    split_file(src, 200, keep_source=False)
    assert not src.exists()
    assert (tmp_path / "big.bin.001").exists()


def test_any_part_can_start_a_join(tmp_path: Path) -> None:
    src = make_file(tmp_path, 900)
    parts = split_file(src, 300)
    for part in parts:
        out = join_parts(part, tmp_path / f"out{part.suffix}")
        assert out.read_bytes() == src.read_bytes()


def test_exactly_999_parts_keep_three_digits(tmp_path: Path) -> None:
    src = make_file(tmp_path, 999)
    parts = split_file(src, 1)
    assert parts[0].name == "big.bin.001" and parts[-1].name == "big.bin.999"


def test_one_byte_parts_beyond_999_switch_to_four_digits_and_join_back(tmp_path: Path) -> None:
    src = make_file(tmp_path, 1000)
    parts = split_file(src, 1)
    assert len(parts) == 1000
    assert parts[0].name == "big.bin.0001" and parts[-1].name == "big.bin.1000"
    assert join_parts(parts[0], tmp_path / "joined").read_bytes() == src.read_bytes()


def test_parts_may_go_to_another_folder_and_name(tmp_path: Path) -> None:
    src = make_file(tmp_path, 300)
    out = tmp_path / "out"
    out.mkdir()
    parts = split_file(src, 100, name="renamed.iso", out_dir=out)
    assert [p.name for p in parts] == ["renamed.iso.001", "renamed.iso.002", "renamed.iso.003"]
    assert all(p.parent == out for p in parts)
    assert manifest_name("renamed.iso") in names(out)


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_part_size_is_rejected(tmp_path: Path, bad: int) -> None:
    src = make_file(tmp_path, 10)
    with pytest.raises(UsageError):
        split_file(src, bad)
    assert names(tmp_path) == ["big.bin"]


def test_missing_source_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        split_file(tmp_path / "nope.bin", 10)


def test_multi_chunk_files(tmp_path: Path) -> None:
    src = make_file(tmp_path, CHUNK_SIZE * 2 + 17)
    parts = split_file(src, CHUNK_SIZE + 5)
    assert len(parts) == 3
    assert join_parts(parts[0], tmp_path / "j").read_bytes() == src.read_bytes()


# --- cleanup --------------------------------------------------------------------------------


def test_cancel_during_split_leaves_nothing_behind(tmp_path: Path) -> None:
    src = make_file(tmp_path, 1000)
    ctx, _, _ = stepping_context("big.bin.003")
    from zipwise.errors import Cancelled

    with pytest.raises(Cancelled):
        split_file(src, 100, ctx)
    assert names(tmp_path) == ["big.bin"]


def test_failure_while_splitting_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = make_file(tmp_path, 1000)
    from zipwise import split

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("manifest failed")

    monkeypatch.setattr(split, "write_manifest", boom)
    with pytest.raises(RuntimeError):
        split_file(src, 100)
    assert names(tmp_path) == ["big.bin"]


def test_resplitting_removes_stale_parts_of_the_old_set(tmp_path: Path) -> None:
    src = make_file(tmp_path, 1000)
    split_file(src, 100)  # 10 parts
    split_file(src, 400)  # 3 parts
    assert [n for n in names(tmp_path) if ".0" in n and not n.endswith(".json")] == [
        "big.bin.001",
        "big.bin.002",
        "big.bin.003",
    ]
    assert join_parts(tmp_path / "big.bin.001", tmp_path / "j").read_bytes() == src.read_bytes()


def test_progress_covers_every_byte(tmp_path: Path) -> None:
    src = make_file(tmp_path, 1000)
    ctx, _, events = stepping_context()
    split_file(src, 300, ctx)
    last = [e for e in events if e.phase == "split"][-1]
    assert (last.bytes_done, last.bytes_total, last.entries_done) == (1000, 1000, 4)


def test_join_progress_uses_the_join_phase(tmp_path: Path) -> None:
    src = make_file(tmp_path, 600)
    parts = split_file(src, 300)
    ctx, _, events = stepping_context()
    join_parts(parts[0], tmp_path / "j", ctx)
    assert {e.phase for e in events} == {"join"}
    assert events[-1].bytes_done == 600


def test_cancel_during_join_leaves_no_output(tmp_path: Path) -> None:
    src = make_file(tmp_path, 600)
    parts = split_file(src, 100)
    ctx, _, _ = stepping_context("big.bin.004")
    from zipwise.errors import Cancelled

    out = tmp_path / "joined.bin"
    with pytest.raises(Cancelled):
        join_parts(parts[0], out, ctx)
    assert not out.exists()
    assert not (tmp_path / "joined.bin.partial").exists()


# --- join details ---------------------------------------------------------------------------


def test_join_into_a_folder_uses_the_original_name(tmp_path: Path) -> None:
    src = make_file(tmp_path, 300, "movie.mkv")
    parts = split_file(src, 100)
    target = tmp_path / "dest"
    target.mkdir()
    assert join_parts(parts[0], target) == target / "movie.mkv"


def test_join_without_destination_writes_next_to_the_parts(tmp_path: Path) -> None:
    src = make_file(tmp_path, 300, "movie.mkv")
    parts = split_file(src, 100, keep_source=False)
    out = join_parts(parts[0])
    assert out == tmp_path / "movie.mkv"
    assert out.stat().st_size == 300


def test_join_replaces_an_existing_output_atomically(tmp_path: Path) -> None:
    src = make_file(tmp_path, 300)
    parts = split_file(src, 100)
    out = tmp_path / "joined"
    out.write_bytes(b"old")
    join_parts(parts[0], out)
    assert out.read_bytes() == src.read_bytes()


def test_join_refuses_to_overwrite_a_part(tmp_path: Path) -> None:
    src = make_file(tmp_path, 300)
    parts = split_file(src, 100)
    with pytest.raises(UsageError):
        join_parts(parts[0], parts[1])


def test_join_without_manifest_still_works(tmp_path: Path) -> None:
    src = make_file(tmp_path, 300)
    parts = split_file(src, 100, manifest=False)
    assert not (tmp_path / manifest_name("big.bin")).exists()
    assert join_parts(parts[0], tmp_path / "j").read_bytes() == src.read_bytes()
    assert verify_parts(parts[0]) is False


def test_join_without_manifest_cannot_notice_a_flipped_byte(tmp_path: Path) -> None:
    src = make_file(tmp_path, 300)
    parts = split_file(src, 100, manifest=False)
    data = bytearray(parts[1].read_bytes())
    data[5] ^= 0xFF
    parts[1].write_bytes(bytes(data))
    assert join_parts(parts[0], tmp_path / "j").read_bytes() != src.read_bytes()


# --- manifest -------------------------------------------------------------------------------


def test_manifest_content(tmp_path: Path) -> None:
    src = make_file(tmp_path, 250)
    parts = split_file(src, 100)
    manifest = read_manifest(tmp_path / ("big.bin" + MANIFEST_SUFFIX))
    assert manifest.original_name == "big.bin"
    assert manifest.total_size == 250
    assert manifest.part_size == 100
    assert manifest.sha256 == hashlib.sha256(src.read_bytes()).hexdigest()
    assert [(p.name, p.size) for p in manifest.parts] == [
        ("big.bin.001", 100),
        ("big.bin.002", 100),
        ("big.bin.003", 50),
    ]
    for listed, part in zip(manifest.parts, parts, strict=True):
        assert listed.sha256 == hashlib.sha256(part.read_bytes()).hexdigest()


def test_manifest_is_valid_json_with_a_format_marker(tmp_path: Path) -> None:
    src = make_file(tmp_path, 50)
    split_file(src, 100)
    data = json.loads((tmp_path / "big.bin.parts.json").read_text(encoding="utf-8"))
    assert data["format"] == "zipwise-parts" and data["version"] == 1


def test_manifest_verifies_a_clean_set(tmp_path: Path) -> None:
    src = make_file(tmp_path, 300)
    parts = split_file(src, 100)
    assert verify_parts(parts[0]) is True


@pytest.mark.parametrize("which", [0, 1, 2])
def test_corrupted_part_is_named_in_the_error(tmp_path: Path, which: int) -> None:
    src = make_file(tmp_path, 300)
    parts = split_file(src, 100)
    data = bytearray(parts[which].read_bytes())
    data[10] ^= 0x01  # same size, different content
    parts[which].write_bytes(bytes(data))
    with pytest.raises(CorruptArchive) as info:
        join_parts(parts[0], tmp_path / "j")
    assert parts[which].name in info.value.message
    assert info.value.code == "part-corrupt"
    assert not (tmp_path / "j").exists()
    with pytest.raises(CorruptArchive, match=parts[which].name):
        verify_parts(parts[0])


def test_truncated_part_is_a_size_error(tmp_path: Path) -> None:
    src = make_file(tmp_path, 300)
    parts = split_file(src, 100)
    parts[1].write_bytes(parts[1].read_bytes()[:-1])
    with pytest.raises(CorruptArchive) as info:
        join_parts(parts[0], tmp_path / "j")
    assert parts[1].name in info.value.message and info.value.code == "part-size"


def test_unreadable_manifest_is_reported_when_joining(tmp_path: Path) -> None:
    src = make_file(tmp_path, 300)
    parts = split_file(src, 100)
    (tmp_path / "big.bin.parts.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptArchive) as info:
        join_parts(parts[0], tmp_path / "j")
    assert info.value.code == "bad-manifest"
    (tmp_path / "big.bin.parts.json").unlink()
    assert join_parts(parts[0], tmp_path / "j").read_bytes() == src.read_bytes()


def test_wrong_manifest_total_hash_is_detected(tmp_path: Path) -> None:
    src = make_file(tmp_path, 300)
    parts = split_file(src, 100)
    path = tmp_path / "big.bin.parts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sha256"] = "0" * 64
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CorruptArchive, match="manifest"):
        join_parts(parts[0], tmp_path / "j")


def test_default_context_is_optional(tmp_path: Path) -> None:
    src = make_file(tmp_path, 10)
    parts = split_file(src, 4, JobContext())
    assert len(parts) == 3
    assert os.path.getsize(join_parts(parts[0], tmp_path / "j")) == 10
    assert find_parts(parts[2]).first == parts[0]
