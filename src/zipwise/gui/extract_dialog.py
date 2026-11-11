"""Dialog for extraction options, with a live preview of the entry names."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..encoding import CANDIDATES
from ..errors import ZipwiseError
from ..i18n import tr
from ..model import ConflictAction, ExtractOptions
from ..ops import archive_info, list_archive
from ..settings import Settings
from .textutil import localize_buttons

PREVIEW_LIMIT = 300
POLICIES = (
    ConflictAction.ASK,
    ConflictAction.SKIP,
    ConflictAction.RENAME,
    ConflictAction.OVERWRITE,
)


def policy_text(action: ConflictAction) -> str:
    if action is ConflictAction.SKIP:
        return tr("policy.skip")
    if action is ConflictAction.RENAME:
        return tr("policy.rename")
    if action is ConflictAction.OVERWRITE:
        return tr("policy.overwrite")
    return tr("policy.ask")


class ExtractDialog(QDialog):
    def __init__(
        self,
        archive: Path,
        dest: Path,
        settings: Settings | None = None,
        selected: list[str] | None = None,
        parent: QWidget | None = None,
        *,
        pick_directory: Callable[[], Path | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings or Settings()
        self._archive = archive
        self._selected = selected or None
        self._pick = pick_directory or self._default_pick_directory
        self.setWindowTitle(tr("extract.title"))
        self.setMinimumSize(520, 460)

        self.dest_edit = QLineEdit(str(dest))
        self.dest_edit.setAccessibleName(tr("extract.destination"))
        self.dest_edit.textChanged.connect(self._update_ok)
        self.browse_button = QPushButton(tr("button.browse"))
        self.browse_button.clicked.connect(self._on_browse)
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_edit, 1)
        dest_row.addWidget(self.browse_button)

        self.policy_combo = QComboBox()
        self.policy_combo.setAccessibleName(tr("extract.conflicts"))
        for action in POLICIES:
            self.policy_combo.addItem(policy_text(action), action)
        try:
            default = ConflictAction(self._settings.on_conflict)
        except ValueError:
            default = ConflictAction.ASK
        self.policy_combo.setCurrentIndex(POLICIES.index(default))

        self.detected: str | None = None
        try:
            self.detected = archive_info(archive).name_encoding
        except ZipwiseError:
            self.detected = None
        self.encoding_combo = QComboBox()
        self.encoding_combo.setAccessibleName(tr("extract.encoding"))
        auto = tr("extract.encoding_auto", encoding=self.detected or "?")
        self.encoding_combo.addItem(auto, None)
        names = list(CANDIDATES)
        if "utf-8" not in names:
            names.insert(0, "utf-8")
        for name in names:
            self.encoding_combo.addItem(name, name)
        self.encoding_combo.setEnabled(self.detected is not None)
        self.encoding_combo.currentIndexChanged.connect(self.refresh_preview)

        self.preview = QListWidget()
        self.preview.setAccessibleName(tr("extract.preview"))
        self.preview_status = QLabel("")

        self.resume_check = QCheckBox(tr("extract.resume"))
        self.resume_check.setAccessibleName(tr("extract.resume"))

        scope = (
            tr("extract.selected_only", count=len(self._selected))
            if self._selected
            else tr("extract.all_entries")
        )
        self.scope_label = QLabel(scope)

        form = QFormLayout()
        form.addRow(tr("extract.destination"), dest_row)
        form.addRow(tr("extract.conflicts"), self.policy_combo)
        form.addRow(tr("extract.encoding"), self.encoding_combo)
        form.addRow("", self.resume_check)
        form.addRow("", self.scope_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        localize_buttons(self.buttons)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addLayout(form)
        layout.addWidget(QLabel(tr("extract.preview")))
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.preview_status)
        layout.addWidget(self.buttons)
        self.refresh_preview()
        self._update_ok()

    def _default_pick_directory(self) -> Path | None:
        name = QFileDialog.getExistingDirectory(self, tr("extract.destination"))
        return Path(name) if name else None

    def _on_browse(self) -> None:
        picked = self._pick()
        if picked is not None:
            self.dest_edit.setText(str(picked))

    def _update_ok(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(self.dest_edit.text().strip())
        )

    def encoding(self) -> str | None:
        data = self.encoding_combo.currentData()
        return data if isinstance(data, str) else None

    def preview_names(self) -> list[str]:
        return [self.preview.item(i).text() for i in range(self.preview.count())]

    def refresh_preview(self) -> None:
        """Re-decode the entry names with the chosen encoding."""
        self.preview.clear()
        try:
            entries = list_archive(self._archive, name_encoding=self.encoding())
        except ZipwiseError as exc:
            self.preview_status.setText(exc.message)
            return
        for entry in entries[:PREVIEW_LIMIT]:
            self.preview.addItem(entry.name)
        extra = len(entries) - PREVIEW_LIMIT
        self.preview_status.setText(tr("extract.preview_more", count=extra) if extra > 0 else "")

    def destination(self) -> Path:
        return Path(self.dest_edit.text().strip())

    def options(self) -> ExtractOptions:
        action = self.policy_combo.currentData()
        return ExtractOptions(
            name_encoding=self.encoding(),
            policy=self._settings.to_policy(),
            on_conflict=action if isinstance(action, ConflictAction) else ConflictAction.ASK,
            only=list(self._selected) if self._selected else None,
            resume=self.resume_check.isChecked(),
        )
