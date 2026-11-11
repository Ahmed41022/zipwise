from __future__ import annotations

import threading
from pathlib import Path

import pytest

from helpers.trees import SHAPES, make_tree
from zipwise.errors import CorruptArchive, WrongPassword
from zipwise.gui.worker import JobWorker
from zipwise.jobs import JobContext
from zipwise.model import ConflictAction, CreateOptions, ExtractOptions
from zipwise.ops import create_archive, extract_archive
from zipwise.ops import test_archive as verify_archive


def run(qtbot, worker: JobWorker, signal: str = "finished"):
    with qtbot.waitSignal(getattr(worker, signal), timeout=10000) as blocker:
        worker.start()
    assert worker.wait(10000)
    return blocker.args


def test_result_is_delivered(qtbot) -> None:
    worker = JobWorker(lambda ctx: 42)
    assert run(qtbot, worker) == [42]


def test_progress_events_reach_the_gui_thread(qtbot, tmp_path: Path, proj_tree: Path) -> None:
    events: list[object] = []
    worker = JobWorker(
        lambda ctx: create_archive([proj_tree], tmp_path / "o.zip", CreateOptions(), ctx)
    )
    worker.progress.connect(events.append)
    run(qtbot, worker)
    assert events
    assert {e.phase for e in events} >= {"compress"}  # type: ignore[attr-defined]


def test_create_runs_and_archive_passes_test(qtbot, tmp_path: Path, proj_tree: Path) -> None:
    dest = tmp_path / "made.zip"
    worker = JobWorker(lambda ctx: create_archive([proj_tree], dest, CreateOptions(), ctx))
    (report,) = run(qtbot, worker)
    assert report.ok
    assert verify_archive(dest).ok


def test_extract_matches_sources(qtbot, tmp_path: Path, proj_tree: Path, sample_zip: Path) -> None:
    out = tmp_path / "out"
    worker = JobWorker(lambda ctx: extract_archive(sample_zip, out, ExtractOptions(), ctx))
    run(qtbot, worker)
    for src in proj_tree.rglob("*"):
        if src.is_file():
            assert (out / "proj" / src.relative_to(proj_tree)).read_bytes() == src.read_bytes()


def test_zipwise_error_is_reported_as_failed(qtbot, tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04" + b"x" * 40)
    worker = JobWorker(lambda ctx: verify_archive(bad, ctx=ctx))
    (exc,) = run(qtbot, worker, "failed")
    assert isinstance(exc, CorruptArchive)


def test_unexpected_exception_is_reported_as_failed(qtbot) -> None:
    def boom(ctx: JobContext) -> None:
        raise RuntimeError("boom")

    (exc,) = run(qtbot, JobWorker(boom), "failed")
    assert isinstance(exc, RuntimeError)


def test_cancel_mid_create_leaves_no_output(qtbot, tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "many", SHAPES["many_small"])
    dest = tmp_path / "out" / "c.zip"
    holder: list[JobWorker] = []

    def job(ctx: JobContext) -> object:
        inner = ctx.on_progress

        def hook(event) -> None:
            if inner:
                inner(event)
            if event.phase == "compress":
                holder[0].cancel()

        ctx.on_progress = hook
        return create_archive([tree], dest, CreateOptions(verify=False), ctx)

    worker = JobWorker(job)
    holder.append(worker)
    run(qtbot, worker, "cancelled")
    assert not dest.exists()
    assert not list(dest.parent.glob("*.partial"))


def test_cancel_mid_extract_leaves_no_partial_files(
    qtbot, tmp_path: Path, sample_zip: Path
) -> None:
    out = tmp_path / "ex"
    holder: list[JobWorker] = []

    def job(ctx: JobContext) -> object:
        inner = ctx.on_progress

        def hook(event) -> None:
            if inner:
                inner(event)
            if event.phase == "extract":
                holder[0].cancel()

        ctx.on_progress = hook
        return extract_archive(sample_zip, out, ExtractOptions(), ctx)

    worker = JobWorker(job)
    holder.append(worker)
    run(qtbot, worker, "cancelled")
    assert not list(out.rglob("*.zwpart")) if out.exists() else True


def _conflict_setup(tmp_path: Path) -> tuple[Path, Path]:
    tree = make_tree(tmp_path / "t", {"a.txt": b"new-a", "b.txt": b"new-b", "c.txt": b"new-c"})
    archive = tmp_path / "c.zip"
    create_archive(sorted(tree.glob("*.txt")), archive, CreateOptions(), JobContext())
    out = tmp_path / "dest"
    out.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (out / name).write_bytes(b"old")
    return archive, out


def test_conflict_apply_to_all_asks_once(qtbot, tmp_path: Path) -> None:
    archive, out = _conflict_setup(tmp_path)
    asked: list[Path] = []

    def answer(path: Path) -> tuple[ConflictAction, bool]:
        asked.append(path)
        return ConflictAction.OVERWRITE, True

    worker = JobWorker(
        lambda ctx: extract_archive(archive, out, ExtractOptions(), ctx), conflict_handler=answer
    )
    run(qtbot, worker)
    assert len(asked) == 1
    assert {p.read_bytes() for p in out.glob("*.txt")} == {b"new-a", b"new-b", b"new-c"}


def test_conflict_without_remember_asks_each_time(qtbot, tmp_path: Path) -> None:
    archive, out = _conflict_setup(tmp_path)
    asked: list[Path] = []
    answers = iter(
        [
            (ConflictAction.SKIP, False),
            (ConflictAction.OVERWRITE, False),
            (ConflictAction.RENAME, False),
        ]
    )

    def answer(path: Path) -> tuple[ConflictAction, bool]:
        asked.append(path)
        return next(answers)

    worker = JobWorker(
        lambda ctx: extract_archive(archive, out, ExtractOptions(), ctx), conflict_handler=answer
    )
    run(qtbot, worker)
    assert len(asked) == 3
    assert (out / "a.txt").read_bytes() == b"old"
    assert (out / "b.txt").read_bytes() == b"new-b"
    assert (out / "c (2).txt").read_bytes() == b"new-c"


def test_conflict_without_handler_skips(qtbot, tmp_path: Path) -> None:
    archive, out = _conflict_setup(tmp_path)
    worker = JobWorker(lambda ctx: extract_archive(archive, out, ExtractOptions(), ctx))
    run(qtbot, worker)
    assert {p.read_bytes() for p in out.glob("*.txt")} == {b"old"}


@pytest.fixture
def locked_zip(tmp_path: Path, proj_tree: Path) -> Path:
    dest = tmp_path / "locked.zip"
    create_archive([proj_tree], dest, CreateOptions(password="pw"), JobContext())
    return dest


def test_password_question_wrong_then_right(qtbot, tmp_path: Path, locked_zip: Path) -> None:
    attempts = iter(["nope", "pw"])
    worker = JobWorker(
        lambda ctx: extract_archive(locked_zip, tmp_path / "o1", ExtractOptions(), ctx),
        password_handler=lambda: next(attempts),
    )
    (exc,) = run(qtbot, worker, "failed")
    assert isinstance(exc, WrongPassword)
    worker2 = JobWorker(
        lambda ctx: extract_archive(locked_zip, tmp_path / "o2", ExtractOptions(), ctx),
        password_handler=lambda: next(attempts),
    )
    run(qtbot, worker2)
    assert (tmp_path / "o2" / "proj" / "top.txt").read_bytes() == b"top"


def test_password_declined_fails_cleanly(qtbot, tmp_path: Path, locked_zip: Path) -> None:
    worker = JobWorker(
        lambda ctx: extract_archive(locked_zip, tmp_path / "o", ExtractOptions(), ctx),
        password_handler=lambda: None,
    )
    (exc,) = run(qtbot, worker, "failed")
    assert exc.code == "password-required"


def test_cancel_while_waiting_for_an_answer(qtbot, tmp_path: Path, locked_zip: Path) -> None:
    holder: list[JobWorker] = []

    def handler() -> str | None:
        holder[0].cancel()
        return "pw"

    worker = JobWorker(
        lambda ctx: extract_archive(locked_zip, tmp_path / "o", ExtractOptions(), ctx),
        password_handler=handler,
    )
    holder.append(worker)
    run(qtbot, worker, "cancelled")


def test_shutdown_cancels_a_running_job(qtbot) -> None:
    started = threading.Event()

    def job(ctx: JobContext) -> None:
        started.set()
        gate = threading.Event()
        while not gate.wait(0.01):
            ctx.raise_if_cancelled()

    worker = JobWorker(job)
    worker.start()
    qtbot.waitUntil(started.is_set, timeout=10000)
    assert worker.is_running()
    assert worker.shutdown(10000)
    assert not worker.is_running()
