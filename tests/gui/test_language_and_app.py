from __future__ import annotations

import re
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from helpers.recorder import Recorder
from zipwise import i18n
from zipwise.gui import app
from zipwise.gui.main_window import MainWindow
from zipwise.settings import load_settings


def test_arabic_switches_direction_and_texts(
    win: tuple[MainWindow, Recorder], tmp_path: Path
) -> None:
    window, _ = win
    english_open = window.act_open.text()
    window.change_language("ar")
    assert QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft
    assert window.act_open.text() == i18n.load("ar")["action.open"] != english_open
    assert window.file_menu.title() == i18n.load("ar")["menu.file"]
    assert window.empty_label.text() == i18n.load("ar")["main.empty_hint"]
    assert load_settings(tmp_path / "settings.json").language == "ar"


def test_switching_back_restores_ltr(win: tuple[MainWindow, Recorder]) -> None:
    window, _ = win
    window.change_language("ar")
    window.change_language("en")
    assert QApplication.layoutDirection() == Qt.LayoutDirection.LeftToRight
    assert window.act_open.text() == "Open"


def test_language_menu_actions_trigger_switch(win: tuple[MainWindow, Recorder]) -> None:
    window, _ = win
    window.language_actions["ar"].trigger()
    assert i18n.current_language() == "ar"
    assert window.language_actions["ar"].isChecked()


def test_headers_and_status_retranslate_with_archive_open(
    win: tuple[MainWindow, Recorder], sample_zip: Path
) -> None:
    window, _ = win
    window.open_archive(sample_zip)
    window.change_language("ar")
    assert window.model.headerData(0, Qt.Orientation.Horizontal) == i18n.load("ar")["column.name"]
    assert "عنصر" in window.status_label.text()
    assert window.model.data(window.model.index(0, 5)) == i18n.load("ar")["type.folder"]


def test_every_translation_key_used_in_gui_exists_in_english() -> None:
    src = Path(app.__file__).parent
    en = i18n.load("en")
    prefixes = {k.split(".")[0] for k in en}
    used: set[str] = set()
    literal = re.compile(r'"([a-z0-9_]+\.[a-z0-9_.]+)"')
    for py in src.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        used.update(re.findall(r'\btr\(\s*"([^"]+)"', text))
        used.update(m for m in literal.findall(text) if m.split(".")[0] in prefixes)
    missing = sorted(k for k in used if k not in en)
    assert not missing
    assert len(used) > 80
    assert {f"column.{c}" for c in ("name", "size", "packed", "ratio", "modified", "type")} <= set(
        en
    )


def test_smoke_flag_returns_zero(qtbot, tmp_path: Path) -> None:
    assert app.main(["--smoke", "--settings", str(tmp_path / "s.json")]) == 0


def test_smoke_with_archive_argument(tmp_path: Path, sample_zip: Path) -> None:
    code = app.main(["--smoke", "--settings", str(tmp_path / "s.json"), str(sample_zip)])
    assert code == 0


def test_smoke_applies_saved_arabic(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text('{"language": "ar"}', encoding="utf-8")
    assert app.main(["--smoke", "--settings", str(path)]) == 0
    assert QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_window_builds_in_each_language(win: tuple[MainWindow, Recorder], lang: str) -> None:
    window, _ = win
    window.change_language(lang)
    assert window.windowTitle()


def test_ltr_isolates_numbers_only_in_rtl() -> None:
    from zipwise.gui.textutil import ltr

    assert ltr("5 KB") == "5 KB"
    i18n.set_language("ar")
    assert ltr("5 KB") == "\u20665 KB\u2069"
    assert ltr("") == ""


def test_dialog_buttons_use_catalogue_texts(qtbot) -> None:
    from PySide6.QtWidgets import QDialogButtonBox

    from zipwise.gui.password_dialog import PasswordDialog

    i18n.set_language("ar")
    dlg = PasswordDialog()
    qtbot.addWidget(dlg)
    box = dlg.findChild(QDialogButtonBox)
    assert box.button(QDialogButtonBox.StandardButton.Ok).text() == i18n.load("ar")["button.ok"]
