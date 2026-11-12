from __future__ import annotations

import itertools
import threading
import zlib
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication

from helpers.builders import file_entry, write_raw_zip
from helpers.recorder import Recorder
from zipwise.errors import ZipwiseError
from zipwise.gui.main_window import (
    MainWindow,
    decide_drop,
    default_extract_dir,
)
from zipwise.jobs import JobContext
from zipwise.model import CreateOptions, ExtractOptions
from zipwise.ops import create_archive, extract_archive
from zipwise.ops import test_archive as verify_archive


def wait_job(qtbot, win: MainWindow, action) -> str:
    with qtbot.waitSignal(win.job_ended, timeout=10000) as blocker:
        action()
    return str(blocker.args[0])


def test_empty_state_before_anything_is_open(win: tuple[MainWindow, Recorder]) -> None:
    window, _ = win
    assert window.stack.currentIndex() == 0
    assert "Drop an archive" in window.empty_label.text()
    assert not window.act_extract.isEnabled()
    assert window.act_new.isEnabled()


def test_open_zip_fills_table_and_status(
    open_wait, win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, rec = win
    assert open_wait(window, sample_zip) == "opened"
    assert not rec.errors
    assert window.stack.currentIndex() == 1
    assert window.model.rowCount() == 1
    assert window.model.entry_at(0).name == "proj"
    text = window.status_label.text()
    assert "entries" in text and "Total" in text
    assert window.act_extract.isEnabled()
    assert sample_zip.name in window.windowTitle()


def test_open_missing_file_goes_to_show_error(
    open_wait, win: tuple[MainWindow, Recorder], tmp_path: Path
) -> None:
    window, rec = win
    assert open_wait(window, tmp_path / "nope.zip") == "failed"
    assert isinstance(rec.errors[0], ZipwiseError)
    assert window.archive_path is None


def test_open_non_archive_goes_to_show_error(
    open_wait, win: tuple[MainWindow, Recorder], tmp_path: Path
) -> None:
    window, rec = win
    plain = tmp_path / "plain.txt"
    plain.write_text("hello")
    assert open_wait(window, plain) == "failed"
    assert len(rec.errors) == 1


def test_open_dialog_uses_overridable_picker(
    qtbot, win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, _ = win
    window._pick_open_archive = lambda: sample_zip  # type: ignore[method-assign]
    with qtbot.waitSignal(window.open_ended, timeout=10000):
        window.open_dialog()
    assert window.archive_path == sample_zip


def test_navigation_double_click_and_up(
    open_wait, win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, _ = win
    open_wait(window, sample_zip)
    window._on_activated(window.model.index(0, 0))
    assert window.model.current_dir == "proj"
    names = [window.model.entry_at(r).name for r in range(window.model.rowCount())]
    assert names == ["d1", "top.txt"]
    assert "proj" in window.path_label.text()
    assert window.act_up.isEnabled()
    window.go_up()
    assert window.model.current_dir == ""
    assert not window.act_up.isEnabled()


def test_activating_a_file_does_not_navigate(
    open_wait, win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, _ = win
    open_wait(window, sample_zip)
    window.model.set_current_dir("proj")
    file_row = next(
        r for r in range(window.model.rowCount()) if not window.model.entry_at(r).is_dir
    )
    window._on_activated(window.model.index(file_row, 0))
    assert window.model.current_dir == "proj"


def test_selection_enables_extract_selected(
    open_wait, win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, _ = win
    open_wait(window, sample_zip)
    assert not window.act_extract_sel.isEnabled()
    window.view.selectRow(0)
    assert window.act_extract_sel.isEnabled()
    assert window.selected_paths() == ["proj"]


def test_sorting_through_the_view_keeps_folders_first(
    open_wait, win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, _ = win
    open_wait(window, sample_zip)
    window.model.set_current_dir("proj")
    window.view.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert window.model.entry_at(0).is_dir


def test_drop_decisions(tmp_path: Path, sample_zip: Path) -> None:
    plain = tmp_path / "x.txt"
    plain.write_text("x")
    assert decide_drop([sample_zip]) == "open"
    assert decide_drop([plain]) == "create"
    assert decide_drop([tmp_path]) == "create"
    assert decide_drop([sample_zip, plain]) == "create"
    assert decide_drop([]) == "none"


def _drop(window: MainWindow, *paths: Path) -> None:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    event = QDropEvent(
        QPointF(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)


def test_dropping_an_archive_opens_it(
    qtbot, win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, _ = win
    with qtbot.waitSignal(window.open_ended, timeout=10000):
        _drop(window, sample_zip)
    assert window.archive_path == sample_zip


def test_dropping_plain_files_opens_create_dialog(
    win: tuple[MainWindow, Recorder], tmp_path: Path
) -> None:
    window, _ = win
    seen: list[list[Path]] = []
    window.new_archive = lambda sources=None: seen.append(list(sources or []))  # type: ignore[method-assign]
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a")
    b.write_text("b")
    _drop(window, a, b)
    assert seen == [[a, b]]


def test_new_archive_dialog_is_prefilled_by_drop(
    win: tuple[MainWindow, Recorder], tmp_path: Path
) -> None:
    window, _ = win
    a = tmp_path / "a.txt"
    a.write_text("a")
    captured: list[object] = []

    def fake_exec(dialog) -> bool:
        captured.append(dialog.sources())
        return False

    window._exec_dialog = fake_exec  # type: ignore[method-assign]
    window.handle_drop([a])
    assert captured == [[a]]


def test_create_through_dialog_and_worker(
    qtbot, win: tuple[MainWindow, Recorder], tmp_path: Path, proj_tree: Path
) -> None:
    window, rec = win
    dest = tmp_path / "made.zip"

    def fill(dialog) -> bool:
        dialog.dest_edit.setText(str(dest))
        return True

    window._exec_dialog = fill  # type: ignore[method-assign]
    assert wait_job(qtbot, window, lambda: window.new_archive([proj_tree])) == "finished"
    qtbot.waitUntil(lambda: window.archive_path == dest and not window.loading, timeout=10000)
    assert not rec.errors
    assert verify_archive(dest).ok


def test_create_opens_the_archive_when_a_split_was_not_needed(
    qtbot, win: tuple[MainWindow, Recorder], tmp_path: Path, proj_tree: Path
) -> None:
    window, rec = win
    dest = tmp_path / "small.zip"
    options = CreateOptions(split_size=10**9)
    assert wait_job(qtbot, window, lambda: window.create_with([proj_tree], dest, options)) == (
        "finished"
    )
    qtbot.waitUntil(lambda: window.archive_path == dest and not window.loading, timeout=10000)
    assert not rec.errors
    assert not (tmp_path / "small.zip.001").exists()


def test_create_asks_before_replacing(
    win: tuple[MainWindow, Recorder], tmp_path: Path, proj_tree: Path
) -> None:
    window, _ = win
    dest = tmp_path / "exists.zip"
    dest.write_bytes(b"old")
    window._confirm = lambda title, text: False  # type: ignore[method-assign]
    window.create_with([proj_tree], dest, CreateOptions())
    assert dest.read_bytes() == b"old"
    assert not window.busy


def test_extract_through_dialog_and_worker(
    open_wait,
    qtbot,
    win: tuple[MainWindow, Recorder],
    tmp_path: Path,
    proj_tree: Path,
    sample_zip: Path,
) -> None:
    window, rec = win
    open_wait(window, sample_zip)
    out = tmp_path / "extracted"

    def fill(dialog) -> bool:
        dialog.dest_edit.setText(str(out))
        return True

    window._exec_dialog = fill  # type: ignore[method-assign]
    assert wait_job(qtbot, window, window.extract_all) == "finished"
    assert not rec.errors
    for src in proj_tree.rglob("*"):
        if src.is_file():
            assert (out / "proj" / src.relative_to(proj_tree)).read_bytes() == src.read_bytes()
    assert "Extracted" in window.status_label.text()


def test_extract_selected_only(
    open_wait, qtbot, win: tuple[MainWindow, Recorder], tmp_path: Path, sample_zip: Path
) -> None:
    window, _ = win
    open_wait(window, sample_zip)
    window.model.set_current_dir("proj")
    row = next(
        r for r in range(window.model.rowCount()) if window.model.entry_at(r).name == "top.txt"
    )
    window.view.selectRow(row)
    out = tmp_path / "only"

    def fill(dialog) -> bool:
        assert dialog.options().only == ["proj/top.txt"]
        dialog.dest_edit.setText(str(out))
        return True

    window._exec_dialog = fill  # type: ignore[method-assign]
    wait_job(qtbot, window, window.extract_selected)
    assert sorted(p.name for p in out.rglob("*") if p.is_file()) == ["top.txt"]


def test_extract_wrong_then_right_password(
    open_wait, qtbot, win: tuple[MainWindow, Recorder], tmp_path: Path, proj_tree: Path
) -> None:
    window, rec = win
    locked = tmp_path / "locked.zip"
    create_archive([proj_tree], locked, CreateOptions(password="pw"), JobContext())
    open_wait(window, locked)
    answers = iter(["bad", "pw"])
    window._ask_password = lambda wrong=False: next(answers)  # type: ignore[method-assign]
    out = tmp_path / "unlocked"

    def fill(dialog) -> bool:
        dialog.dest_edit.setText(str(out))
        return True

    window._exec_dialog = fill  # type: ignore[method-assign]
    assert wait_job(qtbot, window, window.extract_all) == "finished"
    assert not rec.errors
    assert (out / "proj" / "top.txt").read_bytes() == b"top"


def test_wrong_password_three_times_shows_error(
    open_wait, qtbot, win: tuple[MainWindow, Recorder], tmp_path: Path, proj_tree: Path
) -> None:
    window, rec = win
    locked = tmp_path / "locked.zip"
    create_archive([proj_tree], locked, CreateOptions(password="pw"), JobContext())
    open_wait(window, locked)
    window._ask_password = lambda wrong=False: "bad"  # type: ignore[method-assign]
    window._exec_dialog = lambda d: d.dest_edit.setText(str(tmp_path / "x")) or True  # type: ignore[method-assign]
    assert wait_job(qtbot, window, window.extract_all) == "failed"
    assert rec.errors[0].code == "wrong-password"  # type: ignore[attr-defined]


def test_damaged_entry_is_reported_by_the_test_action(
    qtbot, open_wait, win: tuple[MainWindow, Recorder], tmp_path: Path
) -> None:
    window, rec = win
    good = b"intact " * 20
    # the stored bytes of "b.txt" do not match its recorded CRC-32, which is always detected
    damaged = write_raw_zip(
        tmp_path / "damaged.zip",
        [
            file_entry("a.txt", good, method=0),
            file_entry("b.txt", good, method=0, declared_crc=zlib.crc32(good) ^ 0xFFFFFFFF),
        ],
    )
    assert open_wait(window, damaged) == "opened"
    assert wait_job(qtbot, window, window.test_current) == "finished"
    assert not rec.errors
    assert rec.messages == [("Test archive", "The archive has 1 damaged entries.")]


def test_unreadable_archive_error_is_routed_to_show_error_once(
    qtbot, open_wait, win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, rec = win
    assert open_wait(window, sample_zip) == "opened"
    sample_zip.write_bytes(b"PK" + b"x" * 40)  # no central directory: never readable
    assert wait_job(qtbot, window, window.test_current) == "failed"
    assert [e.code for e in rec.errors] == ["corrupt-archive"]  # type: ignore[attr-defined]
    assert not rec.messages


def test_test_action_reports_intact_archive(
    open_wait, qtbot, win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, rec = win
    open_wait(window, sample_zip)
    assert wait_job(qtbot, window, window.test_current) == "finished"
    assert "intact" in rec.messages[-1][1]


def test_info_action_lists_format_and_entries(
    open_wait, win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, rec = win
    open_wait(window, sample_zip)
    window.show_info()
    title, text = rec.messages[-1]
    assert "zip" in text and "Entries" in text


def test_cancel_through_progress_dialog(qtbot, win: tuple[MainWindow, Recorder]) -> None:
    window, _ = win
    started = threading.Event()

    def factory(_pw: str | None):
        def job(ctx: JobContext) -> None:
            started.set()
            gate = threading.Event()
            while not gate.wait(0.01):
                ctx.raise_if_cancelled()

        return job

    with qtbot.waitSignal(window.job_ended, timeout=10000) as blocker:
        window._start_job("t", factory, lambda r: None)
        qtbot.waitUntil(started.is_set, timeout=10000)
        assert window.busy and not window.act_new.isEnabled()
        progress = window._progress
        assert progress is not None
        progress.cancel_button.click()
        assert progress.cancel_button.text() == "Cancelling..."
        assert not progress.cancel_button.isEnabled()
    assert blocker.args == ["cancelled"]
    assert not window.busy


def test_cancel_mid_extract_through_progress_dialog_leaves_only_complete_files(
    qtbot,
    win: tuple[MainWindow, Recorder],
    tmp_path: Path,
    bulk_tree: Path,
    bulk_zip: Path,
    check_extraction,
) -> None:
    window, _ = win
    out = tmp_path / "cancelled"
    paused = threading.Event()

    def factory(_pw: str | None):
        def job(ctx: JobContext) -> object:
            inner = ctx.on_progress
            ticks = itertools.count(step=1.0)  # one progress event per entry, not per 50 ms
            ctx._clock = lambda: next(ticks)

            def hook(event) -> None:
                if inner:
                    inner(event)
                if event.phase == "extract" and event.entries_done >= 3 and not paused.is_set():
                    paused.set()
                    gate = threading.Event()
                    while not gate.wait(0.01):  # waits here until Cancel has been clicked
                        ctx.raise_if_cancelled()

            ctx.on_progress = hook
            return extract_archive(bulk_zip, out, ExtractOptions(), ctx)

        return job

    with qtbot.waitSignal(window.job_ended, timeout=10000) as blocker:
        window._start_job("t", factory, lambda r: None)
        qtbot.waitUntil(paused.is_set, timeout=10000)
        progress = window._progress
        assert progress is not None
        progress.cancel_button.click()
    assert blocker.args == ["cancelled"]
    assert not window.busy
    assert not list(out.rglob("*.zwpart"))
    done = check_extraction(out, bulk_tree)
    assert 1 <= len(done) < len(list(bulk_tree.iterdir()))


def test_closing_the_window_during_a_job_cancels_and_waits(
    qtbot, win: tuple[MainWindow, Recorder]
) -> None:
    window, _ = win
    started = threading.Event()

    def factory(_pw: str | None):
        def job(ctx: JobContext) -> None:
            started.set()
            gate = threading.Event()
            while not gate.wait(0.01):
                ctx.raise_if_cancelled()

        return job

    window._start_job("t", factory, lambda r: None)
    qtbot.waitUntil(started.is_set, timeout=10000)
    worker = window._worker
    assert worker is not None
    window.close()
    assert not worker.is_running()
    assert window._worker is None


def test_second_job_is_ignored_while_busy(
    qtbot, win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, _ = win
    started = threading.Event()

    def factory(_pw: str | None):
        def job(ctx: JobContext) -> None:
            started.set()
            gate = threading.Event()
            while not gate.wait(0.01):
                ctx.raise_if_cancelled()

        return job

    window._start_job("t", factory, lambda r: None)
    first = window._worker
    window._start_job("t2", factory, lambda r: None)
    assert window._worker is first
    window.close()


def test_default_extract_dir_names(tmp_path: Path) -> None:
    assert default_extract_dir(tmp_path / "a.zip") == tmp_path / "a"
    assert default_extract_dir(tmp_path / "a.tar.gz") == tmp_path / "a"
    assert default_extract_dir(tmp_path / "a.zip.001") == tmp_path / "a"


def test_shortcuts_are_set(win: tuple[MainWindow, Recorder]) -> None:
    window, _ = win
    assert window.act_open.shortcut().toString() == "Ctrl+O"
    assert window.act_new.shortcut().toString() == "Ctrl+N"
    assert window.act_extract.shortcut().toString() == "Ctrl+E"


def test_default_window_size(win: tuple[MainWindow, Recorder]) -> None:
    window, _ = win
    assert (window.width(), window.height()) == (1000, 640)
    assert QApplication.instance() is not None


@pytest.mark.parametrize("col", range(6))
def test_headers_are_translated_names(win: tuple[MainWindow, Recorder], col: int) -> None:
    window, _ = win
    assert window.model.headerData(col, Qt.Orientation.Horizontal)
