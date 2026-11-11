from __future__ import annotations

import errno
import io
import os
import stat
import threading
from pathlib import Path

import pytest

from zipwise import fsutil
from zipwise.errors import Cancelled, IOFailure, UsageError
from zipwise.jobs import CancelToken, JobContext, ProgressEvent
from zipwise.model import EntryType


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_cancel_token_starts_clear() -> None:
    token = CancelToken()
    assert not token.cancelled
    token.raise_if_cancelled()


def test_cancel_token_raises_after_cancel() -> None:
    token = CancelToken()
    token.cancel()
    assert token.cancelled
    with pytest.raises(Cancelled):
        token.raise_if_cancelled()


def test_cancel_token_is_thread_safe() -> None:
    token = CancelToken()
    t = threading.Thread(target=token.cancel)
    t.start()
    t.join()
    assert token.cancelled


def test_progress_is_throttled_but_forced_events_pass() -> None:
    clock = FakeClock()
    events: list[ProgressEvent] = []
    ctx = JobContext(on_progress=events.append, clock=clock)
    ctx.report("compress", "a", 1, 10)
    ctx.report("compress", "a", 2, 10)  # same instant: dropped
    assert len(events) == 1
    clock.now += 0.06
    ctx.report("compress", "a", 3, 10)
    assert len(events) == 2
    ctx.report("compress", "a", 4, 10, force=True)
    assert len(events) == 3
    assert events[-1].bytes_done == 4


def test_progress_rate_is_about_twenty_per_second() -> None:
    clock = FakeClock()
    events: list[ProgressEvent] = []
    ctx = JobContext(on_progress=events.append, clock=clock)
    for _ in range(1000):
        clock.now += 0.001  # 1000 calls in one second
        ctx.report("scan")
    assert 15 <= len(events) <= 21


def test_begin_resets_counters_and_emits() -> None:
    events: list[ProgressEvent] = []
    ctx = JobContext(on_progress=events.append)
    ctx.begin("extract", bytes_total=100, entries_total=4)
    ctx.advance(10)
    ctx.entry_done()
    ctx.finish()
    assert events[0].phase == "extract"
    assert events[0].bytes_total == 100
    assert events[-1].bytes_done == 10
    assert events[-1].entries_done == 1
    assert events[-1].entries_total == 4
    ctx.begin("verify")
    assert (ctx.bytes_done, ctx.entries_done) == (0, 0)


def test_context_without_callback_is_silent() -> None:
    ctx = JobContext()
    ctx.begin("scan")
    ctx.advance(5)
    assert ctx.bytes_done == 5


def test_context_raise_if_cancelled_uses_token() -> None:
    token = CancelToken()
    ctx = JobContext(cancel=token)
    ctx.raise_if_cancelled()
    token.cancel()
    with pytest.raises(Cancelled):
        ctx.raise_if_cancelled()


@pytest.mark.parametrize("chunk", [1, 7, 4096, 1 << 20])
def test_copy_stream_copies_everything(chunk: int) -> None:
    data = os.urandom(10_000)
    dst = io.BytesIO()
    ctx = JobContext()
    n = fsutil.copy_stream(io.BytesIO(data), dst, ctx, chunk_size=chunk)
    assert n == len(data)
    assert dst.getvalue() == data
    assert ctx.bytes_done == len(data)


def test_copy_stream_reports_chunks() -> None:
    seen: list[int] = []
    fsutil.copy_stream(
        io.BytesIO(b"x" * 25), io.BytesIO(), JobContext(), chunk_size=10, on_chunk=seen.append
    )
    assert seen == [10, 10, 5]


def test_copy_stream_checks_token_every_chunk() -> None:
    token = CancelToken()
    ctx = JobContext(cancel=token)
    written: list[int] = []

    class Sink(io.BytesIO):
        def write(self, data: bytes) -> int:  # type: ignore[override]
            written.append(len(data))
            if len(written) == 2:
                token.cancel()
            return super().write(data)

    with pytest.raises(Cancelled):
        fsutil.copy_stream(io.BytesIO(b"x" * 100), Sink(), ctx, chunk_size=10)
    assert written == [10, 10]


def test_drain_stream_counts_bytes() -> None:
    ctx = JobContext()
    assert fsutil.drain_stream(io.BytesIO(b"abcdef"), ctx, chunk_size=4) == 6


# --- atomic files ---------------------------------------------------------------------------


def test_atomic_file_commit_renames(tmp_path: Path) -> None:
    final = tmp_path / "out.bin"
    atomic = fsutil.AtomicFile(final)
    atomic.open().write(b"hello")
    assert atomic.temp.name == "out.bin.partial"
    assert atomic.temp.exists()
    assert not final.exists()
    atomic.commit()
    assert final.read_bytes() == b"hello"
    assert not atomic.temp.exists()


def test_atomic_file_discard_removes_partial(tmp_path: Path) -> None:
    atomic = fsutil.AtomicFile(tmp_path / "out.bin")
    atomic.open().write(b"x")
    atomic.discard()
    assert list(tmp_path.iterdir()) == []


def test_atomic_file_context_manager_cleans_up_on_exception(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), fsutil.AtomicFile(tmp_path / "o") as atomic:
        atomic.stream.write(b"abc")
        raise RuntimeError("boom")
    assert list(tmp_path.iterdir()) == []


def test_atomic_file_replaces_existing_only_on_commit(tmp_path: Path) -> None:
    final = tmp_path / "o"
    final.write_bytes(b"old")
    atomic = fsutil.AtomicFile(final)
    atomic.open().write(b"new")
    assert final.read_bytes() == b"old"
    atomic.commit()
    assert final.read_bytes() == b"new"


def test_atomic_file_replaces_read_only_existing(tmp_path: Path) -> None:
    final = tmp_path / "o"
    final.write_bytes(b"old")
    os.chmod(final, stat.S_IREAD)
    atomic = fsutil.AtomicFile(final)
    atomic.open().write(b"new")
    atomic.commit()
    assert final.read_bytes() == b"new"


def test_atomic_file_custom_suffix(tmp_path: Path) -> None:
    assert fsutil.AtomicFile(tmp_path / "a.txt", ".zwpart").temp.name == "a.txt.zwpart"


def test_atomic_file_open_failure_is_io_failure(tmp_path: Path) -> None:
    with pytest.raises(IOFailure):
        fsutil.AtomicFile(tmp_path / "missing" / "o").open()


# --- paths ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_prefix"),
    [
        ("C:\\short\\path.txt", "C:\\short"),
        ("C:\\" + "a" * 300 + "\\f.txt", "\\\\?\\C:\\"),
        ("\\\\server\\share\\" + "b" * 300, "\\\\?\\UNC\\server\\share"),
        ("\\\\?\\C:\\" + "c" * 300, "\\\\?\\C:\\"),
    ],
    ids=["short", "long", "unc-long", "already-prefixed"],
)
def test_long_path_windows_rules(raw: str, expected_prefix: str) -> None:
    assert str(fsutil.long_path(raw, windows=True)).startswith(expected_prefix)


def test_long_path_is_plain_on_posix_rules(tmp_path: Path) -> None:
    assert fsutil.long_path(tmp_path / "x", windows=False) == Path(os.path.abspath(tmp_path / "x"))


def test_long_path_handles_300_character_nested_path(tmp_path: Path) -> None:
    deep = tmp_path
    for _ in range(4):
        deep = deep / ("d" * 80)
    target = fsutil.long_path(deep / "file.txt")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"deep")
    assert len(str(deep)) > 300
    assert target.read_bytes() == b"deep"


@pytest.mark.parametrize(
    ("err", "code"),
    [
        (errno.ENOSPC, "disk-full"),
        (errno.EACCES, "permission-denied"),
        (errno.EROFS, "permission-denied"),
        (errno.ENOENT, "io-failure"),
    ],
)
def test_translate_oserror(err: int, code: int) -> None:
    exc = fsutil.translate_oserror(OSError(err, os.strerror(err)), "writing")
    assert isinstance(exc, IOFailure)
    assert exc.code == code
    assert exc.message.startswith("writing:")


def test_translate_oserror_permission_error_class() -> None:
    assert fsutil.translate_oserror(PermissionError("denied")).code == "permission-denied"


def test_free_space_of_missing_path_uses_parent(tmp_path: Path) -> None:
    assert fsutil.free_space(tmp_path / "does" / "not" / "exist") > 0


def test_case_insensitivity_probe_matches_reality(tmp_path: Path) -> None:
    (tmp_path / "Probe.txt").write_text("x")
    reality = (tmp_path / "probe.TXT").exists()
    assert fsutil.is_case_insensitive(tmp_path) is reality
    assert list(tmp_path.iterdir()) == [tmp_path / "Probe.txt"]  # probe file removed


def test_ensure_writable_dir_creates_and_leaves_no_probe(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b"
    fsutil.ensure_writable_dir(target)
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_ensure_writable_dir_rejects_a_file(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    with pytest.raises(UsageError):
        fsutil.ensure_writable_dir(f)


def test_set_mtime_accepts_float_and_none(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    fsutil.set_mtime(f, 1_600_000_000.0)
    assert int(f.stat().st_mtime) == 1_600_000_000
    fsutil.set_mtime(f, None)
    assert int(f.stat().st_mtime) == 1_600_000_000


@pytest.mark.parametrize("mode", [0o644, 0o600, 0o755, 0o4755])
def test_apply_mode_masks_special_bits(tmp_path: Path, mode: int) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    before = stat.S_IMODE(f.stat().st_mode)
    fsutil.apply_mode(f, mode)
    expected = before if os.name == "nt" else mode & 0o777  # Windows cannot restore POSIX bits
    assert stat.S_IMODE(f.stat().st_mode) == expected


def test_apply_mode_none_is_noop(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    before = f.stat().st_mode
    fsutil.apply_mode(f, None)
    assert f.stat().st_mode == before


# --- walking sources ------------------------------------------------------------------------


def _names(items: list[fsutil.SourceItem]) -> list[str]:
    return [i.arcname for i in items]


def test_walk_includes_empty_dirs_and_keeps_top_folder(tmp_path: Path) -> None:
    (tmp_path / "t" / "empty").mkdir(parents=True)
    (tmp_path / "t" / "f.txt").write_text("x")
    items = list(fsutil.walk_sources([tmp_path / "t"]))
    assert _names(items) == ["t", "t/empty", "t/f.txt"]
    assert items[1].type is EntryType.DIR
    assert items[2].type is EntryType.FILE
    assert items[2].size == 1


def test_walk_is_sorted_and_deterministic(tmp_path: Path) -> None:
    for name in ["c", "a", "b"]:
        (tmp_path / name).write_text(name)
    assert _names(list(fsutil.walk_sources([tmp_path]))) == [tmp_path.name] + [
        f"{tmp_path.name}/{n}" for n in "abc"
    ]


def test_walk_root_dir_changes_naming_base(tmp_path: Path) -> None:
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "f").write_text("x")
    items = list(fsutil.walk_sources([tmp_path / "t"], root_dir=tmp_path / "t"))
    assert _names(items) == ["f"]


def test_walk_root_dir_must_contain_sources(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with pytest.raises(UsageError):
        list(fsutil.walk_sources([tmp_path / "a"], root_dir=tmp_path / "b"))


def test_walk_missing_input_is_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        list(fsutil.walk_sources([tmp_path / "nope"]))


def test_walk_excludes_given_paths(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / "out.zip.partial").write_text("p")
    items = list(fsutil.walk_sources([tmp_path], exclude=[tmp_path / "out.zip.partial"]))
    assert _names(items) == [tmp_path.name, f"{tmp_path.name}/keep.txt"]


@pytest.mark.needs_symlink
def test_walk_symlink_is_not_followed_by_default(tmp_path: Path) -> None:
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "real").write_text("x")
    os.symlink("real", tmp_path / "t" / "link")
    items = {i.arcname: i for i in fsutil.walk_sources([tmp_path / "t"])}
    assert items["t/link"].type is EntryType.SYMLINK
    assert items["t/link"].link_target == "real"


@pytest.mark.needs_symlink
def test_walk_follow_symlinks_and_loop_protection(tmp_path: Path) -> None:
    (tmp_path / "t" / "d").mkdir(parents=True)
    (tmp_path / "t" / "d" / "f").write_text("x")
    os.symlink(tmp_path / "t", tmp_path / "t" / "d" / "loop", target_is_directory=True)
    items = list(fsutil.walk_sources([tmp_path / "t"], follow_symlinks=True))
    assert "t/d/f" in _names(items)
    assert len(items) < 10


def test_make_symlink_returns_bool(tmp_path: Path) -> None:
    result = fsutil.make_symlink(tmp_path / "l", "target")
    assert isinstance(result, bool)
    assert result == os.path.islink(tmp_path / "l")
