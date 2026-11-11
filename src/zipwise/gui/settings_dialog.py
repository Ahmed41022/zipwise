"""Preferences dialog; values are saved through ``zipwise.settings``."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..errors import ZipwiseError
from ..i18n import available_languages, tr
from ..model import ConflictAction, FormatId
from ..settings import Settings, save_settings
from ..split import parse_size
from .create_dialog import LEVELS, creatable_formats, level_text
from .extract_dialog import POLICIES, policy_text
from .textutil import localize_buttons

LANGUAGE_NAMES = {"en": "English", "ar": "العربية"}
SANITIZE_MODES = ("auto", "always", "never")


def sanitize_text(mode: str) -> str:
    if mode == "always":
        return tr("sanitize.always")
    if mode == "never":
        return tr("sanitize.never")
    return tr("sanitize.auto")


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: Settings,
        path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._path = path
        self.setWindowTitle(tr("settings.title"))
        self.setMinimumWidth(440)

        self.language_combo = QComboBox()
        self.language_combo.setAccessibleName(tr("settings.language"))
        for code in available_languages():
            self.language_combo.addItem(LANGUAGE_NAMES.get(code, code), code)
        self.format_combo = QComboBox()
        self.format_combo.setAccessibleName(tr("settings.default_format"))
        for fmt in creatable_formats():
            self.format_combo.addItem(fmt.value, fmt.value)
        self.level_combo = QComboBox()
        self.level_combo.setAccessibleName(tr("settings.default_level"))
        for level in LEVELS:
            self.level_combo.addItem(level_text(level), level)
        self.conflict_combo = QComboBox()
        self.conflict_combo.setAccessibleName(tr("settings.default_conflict"))
        for action in POLICIES:
            self.conflict_combo.addItem(policy_text(action), action.value)
        self.sanitize_combo = QComboBox()
        self.sanitize_combo.setAccessibleName(tr("settings.sanitize"))
        for mode in SANITIZE_MODES:
            self.sanitize_combo.addItem(sanitize_text(mode), mode)

        self.ratio_spin = QDoubleSpinBox()
        self.ratio_spin.setRange(1, 1_000_000)
        self.ratio_spin.setAccessibleName(tr("settings.max_ratio"))
        self.entries_spin = QSpinBox()
        self.entries_spin.setRange(1, 2_000_000_000)
        self.entries_spin.setAccessibleName(tr("settings.max_entries"))
        self.disk_limited_check = QCheckBox(tr("settings.limit_by_disk"))
        self.disk_limited_check.toggled.connect(self._sync_total)
        self.total_edit = QLineEdit()
        self.total_edit.setPlaceholderText(tr("settings.total_placeholder"))
        self.total_edit.setAccessibleName(tr("settings.max_total"))
        self.verify_check = QCheckBox(tr("create.verify"))

        form = QFormLayout()
        form.addRow(tr("settings.language"), self.language_combo)
        form.addRow(tr("settings.default_format"), self.format_combo)
        form.addRow(tr("settings.default_level"), self.level_combo)
        form.addRow("", self.verify_check)
        form.addRow(tr("settings.default_conflict"), self.conflict_combo)
        form.addRow(tr("settings.sanitize"), self.sanitize_combo)
        form.addRow(tr("settings.max_ratio"), self.ratio_spin)
        form.addRow(tr("settings.max_entries"), self.entries_spin)
        form.addRow(tr("settings.max_total"), self.disk_limited_check)
        form.addRow("", self.total_edit)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        localize_buttons(self.buttons)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addLayout(form)
        layout.addWidget(self.buttons)
        self.load(settings)

    def _sync_total(self) -> None:
        self.total_edit.setEnabled(not self.disk_limited_check.isChecked())

    @staticmethod
    def _select(combo: QComboBox, value: object) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def load(self, s: Settings) -> None:
        self._select(self.language_combo, s.language)
        self._select(self.format_combo, s.default_format)
        self._select(self.level_combo, s.default_level)
        self._select(self.conflict_combo, s.on_conflict)
        self._select(self.sanitize_combo, s.sanitize_windows_names)
        self.ratio_spin.setValue(s.max_ratio)
        self.entries_spin.setValue(s.max_entries)
        self.verify_check.setChecked(s.verify_after_create)
        self.disk_limited_check.setChecked(s.max_total_bytes is None)
        self.total_edit.setText("" if s.max_total_bytes is None else str(s.max_total_bytes))
        self._sync_total()

    def total_is_valid(self) -> bool:
        if self.disk_limited_check.isChecked():
            return True
        try:
            parse_size(self.total_edit.text())
        except ZipwiseError:
            return False
        return True

    def settings(self) -> Settings:
        """The edited values as a new ``Settings`` (other keys are kept)."""
        total: int | None = None
        if not self.disk_limited_check.isChecked() and self.total_is_valid():
            total = parse_size(self.total_edit.text())
        return dataclasses.replace(
            self._settings,
            language=str(self.language_combo.currentData()),
            default_format=str(self.format_combo.currentData() or FormatId.ZIP.value),
            default_level=str(self.level_combo.currentData()),
            on_conflict=str(self.conflict_combo.currentData() or ConflictAction.ASK.value),
            sanitize_windows_names=str(self.sanitize_combo.currentData()),
            max_ratio=float(self.ratio_spin.value()),
            max_entries=int(self.entries_spin.value()),
            max_total_bytes=total,
            verify_after_create=self.verify_check.isChecked(),
        )

    def accept(self) -> None:
        if not self.total_is_valid():
            self.total_edit.setFocus()
            return
        save_settings(self.settings(), self._path)
        super().accept()
