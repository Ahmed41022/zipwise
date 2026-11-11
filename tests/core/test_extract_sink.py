"""The extraction sink driven by a stub backend, the way TAR and 7z backends will use it."""

from __future__ import annotations

import dataclasses
import io
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import pytest

from zipwise.backends.base import ArchiveReader, EntrySink
from zipwise.errors import SafetyViolation
from zipwise.jobs import JobContext
from zipwise.model import (
    ArchiveEntry,
    ArchiveInfo,
    ConflictAction,
    EntryType,
    ExtractOptions,
    FormatId,
    Issue,
)
from zipwise.ops import extract as extract_mod
from zipwise.ops.extract import extract_reader
from zipwise.safety import SafetyPolicy

T0 = datetime(2020, 1, 2, 3, 4, 6, tzinfo=UTC)
T1 = datetime(2021, 5, 6, 7, 8, 10, tzinfo=UTC)


class StubReader(ArchiveReader):
    format = FormatId.TAR

    def __init__(self, items: list[tuple[ArchiveEntry, bytes]]) -> None:
        super().__init__(Path("stub.tar"))
        self.items = [(dataclasses.replace(e, index=i), d) for i, (e, d) in enumerate(items)]

    def entries(self) -> list[ArchiveEntry]:
        return [e for e, _ in self.items]

    def extract(self, entries: Sequence[ArchiveEntry], sink: EntrySink, ctx: JobContext) -> None:
        data = {e.index: d for e, d in self.items}
        self.pull_extract(entries, sink, ctx, lambda e: io.BytesIO(data[e.index]))  # type: ignore[arg-type,return-value]

    def test(self, ctx: JobContext) -> list[Issue]:
        return []

    def info(self) -> ArchiveInfo:
        raise NotImplementedError

    def close(self) -> None:
        pass


def f(name: str, data: bytes = b"data", **kw: object) -> tuple[ArchiveEntry, bytes]:
    return ArchiveEntry(name, size=len(data), **kw), data  # type: ignore[arg-type]


def d(name: str, **kw: object) -> tuple[ArchiveEntry, bytes]:
    return ArchiveEntry(name, type=EntryType.DIR, **kw), b""  # type: ignore[arg-type]


def hard(name: str, target: str) -> tuple[ArchiveEntry, bytes]:
    return ArchiveEntry(name, type=EntryType.HARDLINK, link_target=target), b""


def link(name: str, target: str) -> tuple[ArchiveEntry, bytes]:
    return ArchiveEntry(name, type=EntryType.SYMLINK, link_target=target), b""


def run(tmp_path: Path, items: list[tuple[ArchiveEntry, bytes]], **opts: object):  # noqa: ANN201
    options = ExtractOptions(on_conflict=ConflictAction.OVERWRITE, **opts)  # type: ignore[arg-type]
    return extract_reader(StubReader(items), tmp_path / "dest", options)


def test_hardlinks_are_created_after_their_target(tmp_path: Path) -> None:
    report = run(tmp_path, [hard("b.txt", "a.txt"), f("a.txt", b"shared")])
    assert report.ok
    assert (tmp_path / "dest" / "b.txt").read_bytes() == b"shared"


def test_hardlink_to_a_missing_member_is_reported(tmp_path: Path) -> None:
    report = run(tmp_path, [hard("b.txt", "nothing.txt")])
    assert [i.code for i in report.issues] == ["hardlink-missing"]
    assert not (tmp_path / "dest" / "b.txt").exists()


def test_hardlink_target_follows_strip_components(tmp_path: Path) -> None:
    items = [f("top/a.txt", b"x"), hard("top/b.txt", "top/a.txt")]
    report = run(tmp_path, items, strip_components=1)
    assert report.ok
    assert (tmp_path / "dest" / "b.txt").read_bytes() == b"x"


def test_hardlink_to_outside_is_blocked_by_the_plan(tmp_path: Path) -> None:
    report = run(tmp_path, [hard("b.txt", "../victim")])
    assert [i.code for i in report.issues] == ["hardlink-escape"]


def test_symlink_creation_refused_by_the_os_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract_mod, "make_symlink", lambda *a, **k: False)
    report = run(tmp_path, [f("real.txt"), link("l", "real.txt")])
    assert report.ok
    assert [i.code for i in report.issues] == ["symlink-unsupported"]
    assert report.issues[0].severity == "warning"
    assert report.entries_skipped == 1
    assert (tmp_path / "dest" / "real.txt").exists()
    assert not os.path.lexists(tmp_path / "dest" / "l")


@pytest.mark.needs_symlink
def test_symlinks_are_created_last_so_they_never_get_written_through(tmp_path: Path) -> None:
    report = run(tmp_path, [link("real", "elsewhere"), f("elsewhere/x.txt")])
    assert report.ok
    assert (tmp_path / "dest" / "elsewhere" / "x.txt").exists()
    assert os.path.islink(tmp_path / "dest" / "real")


def test_directory_mtime_is_applied_after_the_files_inside(tmp_path: Path) -> None:
    items = [d("dir", mtime=T0), f("dir/file.txt", mtime=T1)]
    run(tmp_path, items)
    assert abs((tmp_path / "dest" / "dir").stat().st_mtime - T0.timestamp()) < 1
    assert abs((tmp_path / "dest" / "dir" / "file.txt").stat().st_mtime - T1.timestamp()) < 1


def test_nested_directories_get_their_own_mtimes(tmp_path: Path) -> None:
    items = [d("a", mtime=T0), d("a/b", mtime=T1), f("a/b/f.txt")]
    run(tmp_path, items)
    assert abs((tmp_path / "dest" / "a").stat().st_mtime - T0.timestamp()) < 1
    assert abs((tmp_path / "dest" / "a" / "b").stat().st_mtime - T1.timestamp()) < 1


def test_file_replacing_a_directory_entry_is_reported(tmp_path: Path) -> None:
    report = run(tmp_path, [f("x", b"file"), d("x")])
    assert [i.code for i in report.issues] == ["file-in-the-way"]


def test_final_containment_check_catches_a_planner_bug(tmp_path: Path) -> None:
    sink_cls = extract_mod._ExtractSink
    plan = extract_mod.plan_extraction(
        [ArchiveEntry("a.txt", size=1)], tmp_path, SafetyPolicy(), free_space=10**9
    )
    sink = sink_cls(
        tmp_path / "dest",
        plan,
        ExtractOptions(),
        JobContext(),
        extract_mod.ByteBudget(None),
        windows=False,
        case_insensitive=False,
    )
    with pytest.raises(SafetyViolation):
        sink._assert_inside(tmp_path / "elsewhere" / "x")
    with pytest.raises(SafetyViolation):
        sink._assert_inside(tmp_path / "dest")


def test_binary_stream_protocol_is_satisfied_by_the_sink_stream(tmp_path: Path) -> None:
    captured: list[BinaryIO] = []

    class Spy(StubReader):
        def extract(
            self, entries: Sequence[ArchiveEntry], sink: EntrySink, ctx: JobContext
        ) -> None:
            out = sink.open(entries[0])
            assert out is not None
            captured.append(out)
            out.write(b"abc")
            sink.close(entries[0], out, True)

    extract_reader(Spy([f("a.txt", b"abc")]), tmp_path / "dest")
    assert (tmp_path / "dest" / "a.txt").read_bytes() == b"abc"
    assert captured[0].closed
    assert not list((tmp_path / "dest").glob("*.zwpart"))
