"""The "Names encoding" choice of the main window, on archives with legacy (unflagged) names."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from helpers.names import ARABIC, extracted_files, legacy_zip
from helpers.recorder import Recorder
from helpers.trees import SHAPES, make_tree
from zipwise import i18n
from zipwise.errors import CorruptArchive
from zipwise.gui import main_window
from zipwise.gui.main_window import MainWindow
from zipwise.jobs import JobContext
from zipwise.model import CreateOptions
from zipwise.ops import create_archive

OpenWait = Callable[[MainWindow, Path | str], str]

ARABIC_TOP = {name.split("/")[0] for name in ARABIC}
# the same bytes read as CP437 are what a user sees when the archive is opened with the wrong page
GIBBERISH = [name.encode("cp1256").decode("cp437") for name in ARABIC]
GIBBERISH_TOP = {name.split("/")[0] for name in GIBBERISH}


@pytest.fixture
def arabic_zip(tmp_path: Path) -> Path:
    return legacy_zip(tmp_path / "arabic.zip", ARABIC, "cp1256")


@pytest.fixture
def unicode_zip(tmp_path: Path) -> Path:
    tree = make_tree(tmp_path / "uni" / "data", SHAPES["unicode"])
    dest = tmp_path / "unicode.zip"
    create_archive([tree], dest, CreateOptions(), JobContext())
    return dest


def top_names(window: MainWindow) -> set[str]:
    return {window.model.entry_at(r).name for r in range(window.model.rowCount())}


def choose(qtbot, window: MainWindow, encoding: str | None) -> None:
    """Pick an entry of the combo as the user would and wait for the re-listing to end."""
    combo = window.encoding_combo
    with qtbot.waitSignal(window.open_ended, timeout=10000):
        combo.setCurrentIndex(0 if encoding is None else combo.findData(encoding))


def test_automatic_shows_the_arabic_names_and_the_detection(
    win: tuple[MainWindow, Recorder], open_wait: OpenWait, arabic_zip: Path
) -> None:
    window, rec = win
    assert open_wait(window, arabic_zip) == "opened"
    assert top_names(window) == ARABIC_TOP
    assert window.encoding_combo.isEnabled()
    assert not window.encoding_box.isHidden()
    assert window.name_encoding() is None
    assert window.info is not None
    detected = window.info.name_encoding
    assert detected == "cp1256"
    percent = round((window.info.encoding_confidence or 0) * 100)
    assert window.encoding_combo.itemText(0) == f"Automatic (detected: cp1256, {percent}%)"
    assert window.encoding_combo.currentIndex() == 0
    assert "Names: cp1256" in window.status_label.text()
    assert not rec.errors


def test_combo_lists_automatic_and_every_candidate_code_page(
    win: tuple[MainWindow, Recorder], open_wait: OpenWait, arabic_zip: Path
) -> None:
    from zipwise.encoding import CANDIDATES

    window, _ = win
    open_wait(window, arabic_zip)
    items = [window.encoding_combo.itemData(i) for i in range(window.encoding_combo.count())]
    assert items[0] is None
    assert set(CANDIDATES) <= set(items[1:])


def test_choosing_cp437_shows_gibberish_and_automatic_restores_the_names(
    qtbot, win: tuple[MainWindow, Recorder], open_wait: OpenWait, arabic_zip: Path
) -> None:
    window, rec = win
    open_wait(window, arabic_zip)
    choose(qtbot, window, "cp437")
    assert top_names(window) == GIBBERISH_TOP
    assert top_names(window).isdisjoint(ARABIC_TOP)
    assert window.name_encoding() == "cp437"
    assert window.encoding_combo.currentData() == "cp437"
    assert window.encoding_combo.isEnabled()
    assert "Names: cp437" in window.status_label.text()
    # the detection is remembered, so the first item still says what automatic would pick
    assert window.encoding_combo.itemText(0).startswith("Automatic (detected: cp1256")

    choose(qtbot, window, None)
    assert top_names(window) == ARABIC_TOP
    assert window.name_encoding() is None
    assert "Names: cp1256" in window.status_label.text()
    assert not rec.errors


def test_relisting_with_an_encoding_runs_on_the_worker_with_name_encoding(
    qtbot,
    win: tuple[MainWindow, Recorder],
    open_wait: OpenWait,
    monkeypatch: pytest.MonkeyPatch,
    arabic_zip: Path,
) -> None:
    window, _ = win
    open_wait(window, arabic_zip)
    real = main_window.list_archive
    calls: list[tuple[threading.Thread, str | None]] = []

    def spy(path: Path | str, **kwargs: Any) -> Any:
        calls.append((threading.current_thread(), kwargs.get("name_encoding")))
        return real(path, **kwargs)

    monkeypatch.setattr(main_window, "list_archive", spy)
    choose(qtbot, window, "cp866")
    assert len(calls) == 1
    assert calls[0][0] is not threading.main_thread()
    assert calls[0][1] == "cp866"


def test_extract_dialog_starts_with_the_chosen_encoding(
    qtbot, win: tuple[MainWindow, Recorder], open_wait: OpenWait, arabic_zip: Path, tmp_path: Path
) -> None:
    window, _ = win
    open_wait(window, arabic_zip)
    seen: list[tuple[str | None, list[str]]] = []

    def inspect(dialog: Any) -> bool:
        seen.append((dialog.encoding(), dialog.preview_names()))
        return False

    window._exec_dialog = inspect  # type: ignore[method-assign]
    window.extract_all()
    choose(qtbot, window, "cp437")
    window.extract_all()
    assert seen[0][0] is None
    assert set(seen[0][1]) == set(ARABIC)
    assert seen[1][0] == "cp437"
    assert set(seen[1][1]) == set(GIBBERISH)


def test_extract_after_choosing_an_encoding_writes_files_with_those_names(
    qtbot, win: tuple[MainWindow, Recorder], open_wait: OpenWait, arabic_zip: Path, tmp_path: Path
) -> None:
    window, rec = win
    open_wait(window, arabic_zip)
    choose(qtbot, window, "cp437")
    out = tmp_path / "out437"

    def fill(dialog: Any) -> bool:
        dialog.dest_edit.setText(str(out))
        return True

    window._exec_dialog = fill  # type: ignore[method-assign]
    with qtbot.waitSignal(window.job_ended, timeout=10000) as blocker:
        window.extract_all()
    assert blocker.args == ["finished"]
    assert extracted_files(out) == sorted(GIBBERISH)
    assert not rec.errors


def test_extract_with_automatic_writes_the_arabic_names(
    qtbot, win: tuple[MainWindow, Recorder], open_wait: OpenWait, arabic_zip: Path, tmp_path: Path
) -> None:
    window, rec = win
    open_wait(window, arabic_zip)
    out = tmp_path / "out_auto"
    window._exec_dialog = lambda d: d.dest_edit.setText(str(out)) or True  # type: ignore[method-assign]
    with qtbot.waitSignal(window.job_ended, timeout=10000):
        window.extract_all()
    assert extracted_files(out) == sorted(ARABIC)
    assert not rec.errors


def test_utf8_archive_has_the_control_disabled(
    win: tuple[MainWindow, Recorder], open_wait: OpenWait, unicode_zip: Path
) -> None:
    window, _ = win
    assert open_wait(window, unicode_zip) == "opened"
    assert not window.encoding_combo.isEnabled()
    assert window.encoding_combo.itemText(0).startswith("Automatic (detected: utf-8")
    assert window.encoding_combo.toolTip() == i18n.tr("main.encoding_tip_unicode")
    assert "Names: utf-8" in window.status_label.text()


def test_plain_ascii_archive_has_the_control_disabled(
    win: tuple[MainWindow, Recorder], open_wait: OpenWait, sample_zip: Path
) -> None:
    window, _ = win
    open_wait(window, sample_zip)
    assert not window.encoding_combo.isEnabled()


def test_control_is_hidden_until_an_archive_is_open_and_follows_the_archive(
    win: tuple[MainWindow, Recorder], open_wait: OpenWait, arabic_zip: Path, unicode_zip: Path
) -> None:
    window, _ = win
    assert window.encoding_box.isHidden()
    open_wait(window, arabic_zip)
    assert window.encoding_combo.isEnabled()
    open_wait(window, unicode_zip)
    assert not window.encoding_combo.isEnabled()
    open_wait(window, arabic_zip)
    assert window.encoding_combo.isEnabled()
    assert window.name_encoding() is None


def test_opening_another_archive_resets_the_choice_to_automatic(
    qtbot, win: tuple[MainWindow, Recorder], open_wait: OpenWait, arabic_zip: Path, tmp_path: Path
) -> None:
    window, _ = win
    open_wait(window, arabic_zip)
    choose(qtbot, window, "cp437")
    again = legacy_zip(tmp_path / "again.zip", ARABIC, "cp1256")
    open_wait(window, again)
    assert window.name_encoding() is None
    assert window.encoding_combo.currentIndex() == 0
    assert top_names(window) == ARABIC_TOP


def test_combo_is_disabled_while_a_listing_is_running(
    qtbot,
    win: tuple[MainWindow, Recorder],
    open_wait: OpenWait,
    monkeypatch: pytest.MonkeyPatch,
    arabic_zip: Path,
) -> None:
    window, _ = win
    open_wait(window, arabic_zip)
    real = main_window.list_archive
    entered, release = threading.Event(), threading.Event()

    def blocked(path: Path | str, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(20)
        return real(path, **kwargs)

    monkeypatch.setattr(main_window, "list_archive", blocked)
    window.encoding_combo.setCurrentIndex(window.encoding_combo.findData("cp437"))
    qtbot.waitUntil(entered.is_set, timeout=10000)
    assert not window.encoding_combo.isEnabled()
    assert window.status_label.text() == "Opening arabic.zip..."
    with qtbot.waitSignal(window.open_ended, timeout=10000):
        release.set()
    assert window.encoding_combo.isEnabled()


def test_a_failed_relisting_keeps_the_names_and_resets_the_combo(
    qtbot,
    win: tuple[MainWindow, Recorder],
    open_wait: OpenWait,
    monkeypatch: pytest.MonkeyPatch,
    arabic_zip: Path,
) -> None:
    window, rec = win
    open_wait(window, arabic_zip)

    def broken(path: Path | str, **kwargs: Any) -> Any:
        raise CorruptArchive("cannot read the names")

    monkeypatch.setattr(main_window, "list_archive", broken)
    with qtbot.waitSignal(window.open_ended, timeout=10000) as blocker:
        window.encoding_combo.setCurrentIndex(window.encoding_combo.findData("cp437"))
    assert blocker.args == ["failed"]
    assert [e.code for e in rec.errors] == ["corrupt-archive"]  # type: ignore[attr-defined]
    assert window.encoding_combo.currentIndex() == 0
    assert window.name_encoding() is None
    assert top_names(window) == ARABIC_TOP
    assert window.encoding_combo.isEnabled()


def test_arabic_interface_translates_the_control_and_keeps_the_choice(
    qtbot, win: tuple[MainWindow, Recorder], open_wait: OpenWait, arabic_zip: Path
) -> None:
    window, _ = win
    open_wait(window, arabic_zip)
    choose(qtbot, window, "cp437")
    window.change_language("ar")
    ar = i18n.load("ar")
    assert window.encoding_label.text() == ar["main.encoding"]
    assert window.encoding_combo.currentData() == "cp437"
    assert window.encoding_combo.itemText(0).startswith("تلقائي (المكتشف: ")
    assert "cp1256" in window.encoding_combo.itemText(0)
    assert window.encoding_combo.toolTip() == ar["main.encoding_tip"]
    assert QApplication.layoutDirection().name == "RightToLeft"
    window.change_language("en")
    assert window.encoding_label.text() == "Names encoding"
