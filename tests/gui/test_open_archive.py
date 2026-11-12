"""Opening an archive runs on a worker thread and never blocks or overwrites newer state."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QTimer

from helpers.recorder import Recorder
from helpers.trees import make_tree
from zipwise.errors import Cancelled
from zipwise.gui import main_window
from zipwise.gui.main_window import MainWindow, _OpenRequest
from zipwise.gui.worker import JobWorker
from zipwise.jobs import JobContext
from zipwise.model import CreateOptions, FormatId
from zipwise.ops import create_archive

OpenWait = Callable[[MainWindow, Path | str], str]


class Gate:
    """Replaces ``list_archive`` in the main window: records the calling thread and blocks."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, block: Callable[[Path], bool]) -> None:
        self.real = main_window.list_archive
        self.block = block
        self.release = threading.Event()
        self.entered = threading.Event()
        self.threads: list[threading.Thread] = []
        self.calls: list[Path] = []
        monkeypatch.setattr(main_window, "list_archive", self)

    def __call__(self, path: Path | str, **kwargs: Any) -> Any:
        self.threads.append(threading.current_thread())
        self.calls.append(Path(path))
        if self.block(Path(path)):
            self.entered.set()
            if not self.release.wait(20):
                raise RuntimeError("the test never released the gate")
        return self.real(path, **kwargs)


def second_zip(tmp_path: Path) -> Path:
    tree = make_tree(tmp_path / "other" / "second", {"only.txt": b"second"})
    dest = tmp_path / "second.zip"
    create_archive([tree], dest, CreateOptions(), JobContext())
    return dest


def test_listing_runs_off_the_gui_thread(
    win: tuple[MainWindow, Recorder],
    open_wait: OpenWait,
    monkeypatch: pytest.MonkeyPatch,
    sample_zip: Path,
) -> None:
    window, rec = win
    gate = Gate(monkeypatch, block=lambda _p: False)
    assert open_wait(window, sample_zip) == "opened"
    assert gate.calls == [sample_zip]
    assert gate.threads[0] is not threading.main_thread()
    assert not rec.errors


def test_event_loop_keeps_running_while_listing_is_blocked(
    qtbot,
    win: tuple[MainWindow, Recorder],
    monkeypatch: pytest.MonkeyPatch,
    sample_zip: Path,
) -> None:
    window, _ = win
    gate = Gate(monkeypatch, block=lambda _p: True)
    fired: list[bool] = []
    window.open_archive(sample_zip)
    assert window.loading
    qtbot.waitUntil(gate.entered.is_set, timeout=10000)
    QTimer.singleShot(0, lambda: fired.append(True))
    qtbot.waitUntil(lambda: bool(fired), timeout=10000)
    assert window.loading and window.archive_path is None
    with qtbot.waitSignal(window.open_ended, timeout=10000):
        gate.release.set()
    assert window.archive_path == sample_zip
    assert not window.loading


def test_busy_state_while_loading_then_back_to_normal(
    qtbot,
    win: tuple[MainWindow, Recorder],
    open_wait: OpenWait,
    monkeypatch: pytest.MonkeyPatch,
    sample_zip: Path,
    tmp_path: Path,
) -> None:
    window, _ = win
    assert open_wait(window, sample_zip) == "opened"
    assert window.act_extract.isEnabled() and window.act_info.isEnabled()
    other = second_zip(tmp_path)
    gate = Gate(monkeypatch, block=lambda p: p == other)
    window.open_archive(other)
    qtbot.waitUntil(gate.entered.is_set, timeout=10000)
    assert window.status_label.text() == "Opening second.zip..."
    assert not window.open_progress.isHidden()
    assert window.open_progress.minimum() == window.open_progress.maximum() == 0
    for action in (window.act_extract, window.act_extract_sel, window.act_test, window.act_info):
        assert not action.isEnabled()
    assert window.act_open.isEnabled() and window.act_new.isEnabled()
    # the old archive is still what the table shows until the new one has arrived
    assert window.archive_path == sample_zip
    with qtbot.waitSignal(window.open_ended, timeout=10000):
        gate.release.set()
    assert window.open_progress.isHidden()
    assert window.act_extract.isEnabled() and window.act_info.isEnabled()
    assert "Opening" not in window.status_label.text()
    assert window.status_label.text().startswith("2 entries")


def test_actions_that_need_an_archive_do_nothing_while_loading(
    qtbot,
    win: tuple[MainWindow, Recorder],
    open_wait: OpenWait,
    monkeypatch: pytest.MonkeyPatch,
    sample_zip: Path,
    tmp_path: Path,
) -> None:
    window, rec = win
    open_wait(window, sample_zip)
    other = second_zip(tmp_path)
    gate = Gate(monkeypatch, block=lambda p: p == other)
    dialogs: list[object] = []
    window._exec_dialog = lambda d: dialogs.append(d) or False  # type: ignore[method-assign]
    window.open_archive(other)
    qtbot.waitUntil(gate.entered.is_set, timeout=10000)
    window.extract_all()
    window.test_current()
    assert not dialogs and not window.busy
    with qtbot.waitSignal(window.open_ended, timeout=10000):
        gate.release.set()
    assert not rec.errors


def test_second_open_supersedes_the_first(
    qtbot,
    win: tuple[MainWindow, Recorder],
    monkeypatch: pytest.MonkeyPatch,
    sample_zip: Path,
    tmp_path: Path,
) -> None:
    window, rec = win
    other = second_zip(tmp_path)
    gate = Gate(monkeypatch, block=lambda p: p == sample_zip)
    ended: list[str] = []
    window.open_ended.connect(ended.append)
    window.open_archive(sample_zip)
    qtbot.waitUntil(gate.entered.is_set, timeout=10000)
    first_worker = window._open_worker
    assert first_worker is not None
    with qtbot.waitSignal(window.open_ended, timeout=10000):
        window.open_archive(other)
    assert first_worker.token.cancelled
    assert window.archive_path == other
    assert [window.model.entry_at(r).name for r in range(window.model.rowCount())] == ["second"]
    # now the first listing finishes; it must not replace what is on screen
    gate.release.set()
    qtbot.waitUntil(lambda: not window._open_workers, timeout=10000)
    assert window.archive_path == other
    assert [window.model.entry_at(r).name for r in range(window.model.rowCount())] == ["second"]
    assert other.name in window.windowTitle()
    assert ended == ["opened"]
    assert not rec.errors
    assert not window.loading


def test_a_stale_result_is_ignored_even_if_it_reaches_the_handler(
    win: tuple[MainWindow, Recorder], open_wait: OpenWait, sample_zip: Path, tmp_path: Path
) -> None:
    window, rec = win
    assert open_wait(window, sample_zip) == "opened"
    stale = _OpenRequest(window._open_generation - 1, tmp_path / "x.zip", None, None, 0, True)
    window._open_finished(stale, JobWorker(lambda ctx: None), ([], window.info))
    window._open_failed(stale, JobWorker(lambda ctx: None), Cancelled())
    window._open_cancelled(stale, JobWorker(lambda ctx: None))
    assert window.archive_path == sample_zip
    assert window.model.rowCount() == 1
    assert not rec.errors


def test_opening_a_missing_file_reports_through_show_error(
    win: tuple[MainWindow, Recorder], open_wait: OpenWait, sample_zip: Path, tmp_path: Path
) -> None:
    window, rec = win
    assert open_wait(window, sample_zip) == "opened"
    assert open_wait(window, tmp_path / "missing.zip") == "failed"
    assert len(rec.errors) == 1
    assert not window.loading and window.open_progress.isHidden()
    assert window.status_label.text() != "Opening missing.zip..."
    assert "entries" in window.status_label.text()  # the previous archive is still open
    assert window.archive_path == sample_zip
    assert window.act_extract.isEnabled()


def test_opening_a_corrupt_file_reports_one_error(
    win: tuple[MainWindow, Recorder], open_wait: OpenWait, tmp_path: Path
) -> None:
    window, rec = win
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04" + b"x" * 40)
    assert open_wait(window, bad) == "failed"
    assert len(rec.errors) == 1
    assert window.archive_path is None
    assert window.stack.currentIndex() == 0


def locked_7z(tmp_path: Path, proj_tree: Path) -> Path:
    dest = tmp_path / "locked.7z"
    options = CreateOptions(format=FormatId.SEVENZ, password="pw", encrypt_names=True)
    create_archive([proj_tree], dest, options, JobContext())
    return dest


def test_encrypted_names_prompt_for_a_password_and_retry(
    win: tuple[MainWindow, Recorder], open_wait: OpenWait, tmp_path: Path, proj_tree: Path
) -> None:
    window, rec = win
    locked = locked_7z(tmp_path, proj_tree)
    asked: list[bool] = []
    answers = iter(["bad", "pw"])

    def ask(wrong: bool = False) -> str:
        asked.append(wrong)
        return next(answers)

    window._ask_password = ask  # type: ignore[method-assign]
    assert open_wait(window, locked) == "opened"
    assert asked == [False, True]  # first "required", then "wrong"
    assert not rec.errors
    assert window.archive_path == locked
    assert [window.model.entry_at(r).name for r in range(window.model.rowCount())] == ["proj"]


def test_password_given_at_open_is_reused_by_extract_and_test(
    qtbot,
    win: tuple[MainWindow, Recorder],
    open_wait: OpenWait,
    tmp_path: Path,
    proj_tree: Path,
) -> None:
    window, rec = win
    locked = locked_7z(tmp_path, proj_tree)
    window._ask_password = lambda wrong=False: "pw"  # type: ignore[method-assign]
    assert open_wait(window, locked) == "opened"

    def no_prompt(wrong: bool = False) -> str:
        raise AssertionError("the password was asked for again")

    window._ask_password = no_prompt  # type: ignore[method-assign]
    out = tmp_path / "out"
    window._exec_dialog = lambda d: d.dest_edit.setText(str(out)) or True  # type: ignore[method-assign]
    with qtbot.waitSignal(window.job_ended, timeout=10000) as blocker:
        window.extract_all()
    assert blocker.args == ["finished"]
    assert (out / "proj" / "top.txt").read_bytes() == b"top"
    assert not rec.errors


def test_declined_password_shows_the_error_and_keeps_the_old_archive(
    win: tuple[MainWindow, Recorder],
    open_wait: OpenWait,
    sample_zip: Path,
    tmp_path: Path,
    proj_tree: Path,
) -> None:
    window, rec = win
    locked = locked_7z(tmp_path, proj_tree)
    assert open_wait(window, sample_zip) == "opened"
    window._ask_password = lambda wrong=False: None  # type: ignore[method-assign]
    assert open_wait(window, locked) == "failed"
    assert [e.code for e in rec.errors] == ["password-required"]  # type: ignore[attr-defined]
    assert window.archive_path == sample_zip
    assert not window.loading


def test_three_wrong_passwords_end_with_a_wrong_password_error(
    win: tuple[MainWindow, Recorder], open_wait: OpenWait, tmp_path: Path, proj_tree: Path
) -> None:
    window, rec = win
    locked = locked_7z(tmp_path, proj_tree)
    asked: list[bool] = []

    def ask(wrong: bool = False) -> str:
        asked.append(wrong)
        return "bad"

    window._ask_password = ask  # type: ignore[method-assign]
    assert open_wait(window, locked) == "failed"
    assert asked == [False, True, True]
    assert [e.code for e in rec.errors] == ["wrong-password"]  # type: ignore[attr-defined]
    assert window.archive_path is None


def test_table_and_status_bar_fill_after_the_worker_finishes(
    win: tuple[MainWindow, Recorder], open_wait: OpenWait, sample_zip: Path
) -> None:
    window, rec = win
    assert open_wait(window, sample_zip) == "opened"
    assert window.stack.currentIndex() == 1
    assert [window.model.entry_at(r).name for r in range(window.model.rowCount())] == ["proj"]
    text = window.status_label.text()
    assert text.startswith("8 entries")
    assert "Total" in text and "Ratio" in text
    assert window.path_label.text() == "sample.zip"
    assert window.windowTitle() == "sample.zip - Zipwise"
    assert window.info is not None and window.info.entries_count == 8
    assert not rec.errors


def test_closing_the_window_while_loading_stops_the_worker(
    qtbot, win: tuple[MainWindow, Recorder], monkeypatch: pytest.MonkeyPatch, sample_zip: Path
) -> None:
    window, rec = win
    started = threading.Event()
    real = main_window.list_archive

    def cancellable(path: Path | str, **kwargs: Any) -> Any:
        ctx = kwargs["ctx"]
        started.set()
        gate = threading.Event()
        while not gate.wait(0.01):
            ctx.raise_if_cancelled()
        return real(path, **kwargs)

    monkeypatch.setattr(main_window, "list_archive", cancellable)
    window.open_archive(sample_zip)
    qtbot.waitUntil(started.is_set, timeout=10000)
    worker = window._open_worker
    assert worker is not None
    window.close()
    assert not worker.is_running()
    assert not window.loading
    assert window.archive_path is None
    assert not rec.errors
