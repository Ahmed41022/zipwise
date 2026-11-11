from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialogButtonBox

from helpers.names import ARABIC, legacy_zip
from zipwise.gui.conflict_dialog import ConflictDialog
from zipwise.gui.create_dialog import CreateDialog, creatable_formats
from zipwise.gui.extract_dialog import ExtractDialog
from zipwise.gui.password_dialog import PasswordDialog
from zipwise.gui.progress_dialog import ProgressDialog
from zipwise.gui.settings_dialog import SettingsDialog
from zipwise.jobs import ProgressEvent
from zipwise.model import ConflictAction, FormatId, Issue, OperationReport
from zipwise.settings import Settings, load_settings


@pytest.fixture
def two_files(tmp_path: Path) -> list[Path]:
    files = [tmp_path / "one.txt", tmp_path / "two.txt"]
    for p in files:
        p.write_text(p.name)
    return files


def test_create_dialog_lists_only_creatable_formats(qtbot, two_files: list[Path]) -> None:
    dlg = CreateDialog(two_files)
    qtbot.addWidget(dlg)
    formats = dlg.available_formats()
    assert FormatId.ZIP in formats
    assert FormatId.RAR not in formats
    assert FormatId.GZ not in formats  # single-file formats need exactly one file
    assert set(formats) <= set(creatable_formats())


def test_single_file_formats_appear_for_one_file(qtbot, two_files: list[Path]) -> None:
    dlg = CreateDialog(two_files[:1])
    qtbot.addWidget(dlg)
    assert FormatId.GZ in dlg.available_formats()


def test_single_file_formats_hidden_for_a_folder(qtbot, tmp_path: Path) -> None:
    dlg = CreateDialog([tmp_path])
    qtbot.addWidget(dlg)
    assert FormatId.GZ not in dlg.available_formats()


def test_add_and_remove_sources_use_injected_pickers(
    qtbot, two_files: list[Path], tmp_path: Path
) -> None:
    dlg = CreateDialog(
        None,
        pick_files=lambda: two_files,
        pick_folder=lambda: tmp_path,
    )
    qtbot.addWidget(dlg)
    assert not dlg.ok_enabled()
    dlg._on_add_files()
    dlg._on_add_folder()
    assert dlg.sources() == [*two_files, tmp_path]
    assert dlg.ok_enabled()
    dlg.source_list.item(0).setSelected(True)
    dlg._on_remove()
    assert dlg.sources() == [two_files[1], tmp_path]


def test_destination_is_suggested_and_follows_format(qtbot, two_files: list[Path]) -> None:
    dlg = CreateDialog(two_files[:1])
    qtbot.addWidget(dlg)
    assert dlg.destination() == two_files[0].parent / "one.txt.zip"
    dlg.format_combo.setCurrentIndex(dlg.format_combo.findData(FormatId.TAR_GZ))
    assert dlg.destination().name == "one.txt.tar.gz"


def test_manual_destination_is_kept(qtbot, two_files: list[Path], tmp_path: Path) -> None:
    dlg = CreateDialog(two_files, pick_save=lambda s: tmp_path / "chosen.zip")
    qtbot.addWidget(dlg)
    dlg._on_browse()
    dlg.format_combo.setCurrentIndex(dlg.format_combo.findData(FormatId.TAR))
    assert dlg.destination() == tmp_path / "chosen.zip"


def test_password_fields_follow_format_capability(qtbot, two_files: list[Path]) -> None:
    dlg = CreateDialog(two_files)
    qtbot.addWidget(dlg)
    assert dlg.password_edit.isEnabled()
    dlg.format_combo.setCurrentIndex(dlg.format_combo.findData(FormatId.TAR))
    assert not dlg.password_edit.isEnabled()
    assert not dlg.confirm_edit.isEnabled()
    assert dlg.options().password is None


def test_password_mismatch_disables_ok(qtbot, two_files: list[Path]) -> None:
    dlg = CreateDialog(two_files)
    qtbot.addWidget(dlg)
    dlg.password_edit.setText("secret")
    assert not dlg.ok_enabled()
    assert dlg.mismatch_label.text()
    dlg.confirm_edit.setText("secret")
    assert dlg.ok_enabled()
    assert dlg.options().password == "secret"


def test_encrypt_names_is_7z_only(qtbot, two_files: list[Path]) -> None:
    dlg = CreateDialog(two_files)
    qtbot.addWidget(dlg)
    dlg.password_edit.setText("pw")
    dlg.confirm_edit.setText("pw")
    assert not dlg.encrypt_names_check.isEnabled()
    if FormatId.SEVENZ not in dlg.available_formats():
        pytest.skip("7z backend not available")
    dlg.format_combo.setCurrentIndex(dlg.format_combo.findData(FormatId.SEVENZ))
    assert dlg.encrypt_names_check.isEnabled()
    dlg.encrypt_names_check.setChecked(True)
    opts = dlg.options()
    assert opts.format is FormatId.SEVENZ and opts.encrypt_names


def test_split_presets_and_custom_validation(qtbot, two_files: list[Path]) -> None:
    dlg = CreateDialog(two_files)
    qtbot.addWidget(dlg)
    assert dlg.options().split_size is None
    dlg.split_combo.setCurrentIndex(dlg.split_combo.findData("email"))
    assert dlg.options().split_size == 25 * 1000**2
    dlg.split_combo.setCurrentIndex(dlg.split_combo.findData("custom"))
    assert dlg.split_edit.isEnabled()
    assert not dlg.ok_enabled()
    dlg.split_edit.setText("banana")
    assert not dlg.ok_enabled()
    dlg.split_edit.setText("700M")
    assert dlg.ok_enabled()
    assert dlg.options().split_size == 700 * 1000**2


def test_options_defaults_and_level(qtbot, two_files: list[Path]) -> None:
    dlg = CreateDialog(two_files, Settings(default_level="best", verify_after_create=True))
    qtbot.addWidget(dlg)
    opts = dlg.options()
    assert opts.level == "best" and opts.verify and opts.format is FormatId.ZIP
    dlg.verify_check.setChecked(False)
    assert not dlg.options().verify


def test_conflict_dialog_choices(qtbot) -> None:
    for action in (ConflictAction.SKIP, ConflictAction.RENAME, ConflictAction.OVERWRITE):
        dlg = ConflictDialog(Path("x/y.txt"))
        qtbot.addWidget(dlg)
        dlg.apply_all.setChecked(action is ConflictAction.RENAME)
        dlg.buttons[action].click()
        assert dlg.choice() == (action, action is ConflictAction.RENAME)
    dlg = ConflictDialog(Path("x/y.txt"))
    qtbot.addWidget(dlg)
    assert dlg.choice() == (ConflictAction.SKIP, False)


def test_password_dialog(qtbot) -> None:
    dlg = PasswordDialog(wrong=True)
    qtbot.addWidget(dlg)
    dlg.edit.setText("abc")
    assert dlg.password() == "abc"


def test_progress_dialog_determinate_and_busy(qtbot) -> None:
    dlg = ProgressDialog("t")
    qtbot.addWidget(dlg)
    assert dlg.bar.maximum() == 0
    dlg.on_progress(ProgressEvent("compress", "a/b.txt", 50, 100, 1, 4))
    assert dlg.bar.maximum() > 0 and dlg.bar.value() == 500
    assert "50" in dlg.bytes_label.text()
    dlg.on_progress(ProgressEvent("scan", "", 0, 0, 0, 0))
    assert dlg.bar.maximum() == 0


def test_progress_dialog_cancel_button(qtbot) -> None:
    dlg = ProgressDialog("t")
    qtbot.addWidget(dlg)
    with qtbot.waitSignal(dlg.cancel_requested, timeout=1000):
        dlg.cancel_button.click()
    assert not dlg.cancel_button.isEnabled()
    assert dlg.cancel_button.text() == "Cancelling..."
    emitted: list[int] = []
    dlg.cancel_requested.connect(lambda: emitted.append(1))
    dlg._on_cancel()
    assert emitted == []


def test_progress_dialog_closes_on_clean_finish(qtbot) -> None:
    dlg = ProgressDialog("t")
    qtbot.addWidget(dlg)
    dlg.show()
    dlg.job_finished(OperationReport.build([]))
    assert not dlg.isVisible()


def test_progress_dialog_shows_report_issues(qtbot) -> None:
    dlg = ProgressDialog("t")
    qtbot.addWidget(dlg)
    dlg.show()
    report = OperationReport.build(
        [Issue("warning", "path-traversal", "../evil", "blocked entry")],
        entries_done=3,
        entries_skipped=1,
    )
    dlg.job_finished(report)
    assert dlg.isVisible() and dlg.showing_report
    assert dlg.issues_list.count() == 1
    assert "blocked entry" in dlg.issues_list.item(0).text()
    dlg.close_button.click()
    assert not dlg.isVisible()


def test_extract_dialog_options_and_scope(qtbot, sample_zip: Path, tmp_path: Path) -> None:
    dlg = ExtractDialog(sample_zip, tmp_path / "dest", Settings(on_conflict="rename"), ["proj/d1"])
    qtbot.addWidget(dlg)
    opts = dlg.options()
    assert opts.on_conflict is ConflictAction.RENAME
    assert opts.only == ["proj/d1"]
    assert not opts.resume
    assert "1" in dlg.scope_label.text()
    dlg.resume_check.setChecked(True)
    assert dlg.options().resume
    assert dlg.destination() == tmp_path / "dest"
    assert "proj/top.txt" in dlg.preview_names()


def test_extract_dialog_browse_uses_injected_picker(
    qtbot, sample_zip: Path, tmp_path: Path
) -> None:
    dlg = ExtractDialog(sample_zip, tmp_path, pick_directory=lambda: tmp_path / "picked")
    qtbot.addWidget(dlg)
    dlg._on_browse()
    assert dlg.destination() == tmp_path / "picked"


def test_extract_dialog_encoding_changes_preview(qtbot, tmp_path: Path) -> None:
    archive = legacy_zip(tmp_path / "legacy.zip", ARABIC, "cp1256")
    dlg = ExtractDialog(archive, tmp_path / "d")
    qtbot.addWidget(dlg)
    assert dlg.encoding_combo.isEnabled()
    assert dlg.encoding_combo.itemText(0).startswith("Auto (detected:")
    dlg.encoding_combo.setCurrentIndex(dlg.encoding_combo.findData("cp1256"))
    assert sorted(dlg.preview_names()) == sorted(ARABIC)
    assert dlg.options().name_encoding == "cp1256"
    dlg.encoding_combo.setCurrentIndex(dlg.encoding_combo.findData("cp866"))
    assert sorted(dlg.preview_names()) != sorted(ARABIC)
    dlg.encoding_combo.setCurrentIndex(0)
    assert dlg.options().name_encoding is None


def test_extract_dialog_bad_archive_shows_message_not_crash(qtbot, tmp_path: Path) -> None:
    bad = tmp_path / "missing.zip"
    dlg = ExtractDialog(bad, tmp_path)
    qtbot.addWidget(dlg)
    assert dlg.preview.count() == 0
    assert dlg.preview_status.text()


def test_settings_dialog_round_trip(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    dlg = SettingsDialog(Settings(), path)
    qtbot.addWidget(dlg)
    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("ar"))
    dlg.format_combo.setCurrentIndex(dlg.format_combo.findData("tar.gz"))
    dlg.level_combo.setCurrentIndex(dlg.level_combo.findData("best"))
    dlg.conflict_combo.setCurrentIndex(dlg.conflict_combo.findData("rename"))
    dlg.sanitize_combo.setCurrentIndex(dlg.sanitize_combo.findData("always"))
    dlg.ratio_spin.setValue(99)
    dlg.entries_spin.setValue(1234)
    dlg.disk_limited_check.setChecked(False)
    dlg.total_edit.setText("2G")
    dlg.accept()
    saved = load_settings(path)
    assert saved.language == "ar"
    assert saved.default_format == "tar.gz"
    assert saved.default_level == "best"
    assert saved.on_conflict == "rename"
    assert saved.sanitize_windows_names == "always"
    assert saved.max_ratio == 99
    assert saved.max_entries == 1234
    assert saved.max_total_bytes == 2 * 1000**3
    again = SettingsDialog(saved, path)
    qtbot.addWidget(again)
    assert again.settings() == saved


def test_settings_dialog_default_limit_is_free_disk_space(qtbot, tmp_path: Path) -> None:
    dlg = SettingsDialog(Settings(), tmp_path / "s.json")
    qtbot.addWidget(dlg)
    assert dlg.disk_limited_check.isChecked()
    assert not dlg.total_edit.isEnabled()
    assert dlg.settings().max_total_bytes is None


def test_settings_dialog_rejects_invalid_total(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    dlg = SettingsDialog(Settings(), path)
    qtbot.addWidget(dlg)
    dlg.disk_limited_check.setChecked(False)
    dlg.total_edit.setText("lots")
    dlg.accept()
    assert not path.exists()
    assert dlg.buttons.button(QDialogButtonBox.StandardButton.Ok) is not None
