"""Single-file streams have no recorded size (-1): totals become "unknown", never negative."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from helpers.builders import compress_bytes
from helpers.jobs import stepping_context
from helpers.trees import random_bytes
from zipwise.jobs import ProgressEvent
from zipwise.model import ArchiveEntry, CreateOptions, EntryType, FormatId
from zipwise.ops import archive_info, create_archive, extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive
from zipwise.ops._input import progress_total

PAYLOAD = random_bytes(4, 3000) + b"text " * 2000
CODECS = [
    "gz",
    "bz2",
    "xz",
    pytest.param(
        "zst",
        marks=pytest.mark.skipif(
            importlib.util.find_spec("zstandard") is None, reason="zstandard is not installed"
        ),
    ),
]


def single(tmp_path: Path, codec: str) -> Path:
    path = tmp_path / f"data.bin.{codec}"
    path.write_bytes(compress_bytes(PAYLOAD, codec))
    return path


def assert_sane(events: list[ProgressEvent], phase: str) -> None:
    mine = [e for e in events if e.phase == phase]
    assert mine
    assert all(e.bytes_total == 0 for e in mine)  # unknown: an indeterminate bar
    assert all(e.bytes_done >= 0 and e.entries_total >= 0 for e in mine)
    assert mine[-1].bytes_done == len(PAYLOAD)


@pytest.mark.parametrize("codec", CODECS)
def test_info_never_reports_a_negative_total(tmp_path: Path, codec: str) -> None:
    info = archive_info(single(tmp_path, codec))
    assert info.total_size >= 0
    data = json.loads(json.dumps(info.to_dict()))
    if codec == "gz":  # the gzip trailer records the size
        assert info.total_size_known and info.total_size == len(PAYLOAD)
        assert data["total_size"] == len(PAYLOAD)
    else:
        assert not info.total_size_known and info.total_size == 0
        assert data["total_size"] is None


@pytest.mark.parametrize("codec", CODECS)
def test_list_keeps_the_entry_size_unknown(tmp_path: Path, codec: str) -> None:
    (entry,) = list_archive(single(tmp_path, codec))
    assert entry.size == -1 and entry.to_dict()["size"] == -1
    assert progress_total([entry]) == 0


@pytest.mark.parametrize("codec", CODECS)
def test_test_progress_has_an_unknown_total(tmp_path: Path, codec: str) -> None:
    ctx, _, events = stepping_context()
    report = verify_archive(single(tmp_path, codec), ctx=ctx)
    assert report.ok and report.bytes_done == len(PAYLOAD)
    assert_sane(events, "test")


@pytest.mark.parametrize("codec", CODECS)
def test_extract_progress_has_an_unknown_total(tmp_path: Path, codec: str) -> None:
    ctx, _, events = stepping_context()
    report = extract_archive(single(tmp_path, codec), tmp_path / "out", ctx=ctx)
    assert report.ok and (tmp_path / "out" / "data.bin").read_bytes() == PAYLOAD
    assert_sane(events, "extract")


@pytest.mark.parametrize("fmt", [FormatId.BZ2, FormatId.XZ])
def test_verify_after_create_has_an_unknown_total(tmp_path: Path, fmt: FormatId) -> None:
    src = tmp_path / "data.bin"
    src.write_bytes(PAYLOAD)
    ctx, _, events = stepping_context()
    create_archive([src], tmp_path / f"data.bin.{fmt.value}", CreateOptions(format=fmt), ctx)
    assert_sane(events, "verify")


def test_progress_total_sums_known_sizes() -> None:
    known = [
        ArchiveEntry("a", size=5),
        ArchiveEntry("d", type=EntryType.DIR),
        ArchiveEntry("b", size=7),
    ]
    assert progress_total(known) == 12
    assert progress_total([*known, ArchiveEntry("c", size=-1)]) == 0
    assert progress_total([]) == 0
