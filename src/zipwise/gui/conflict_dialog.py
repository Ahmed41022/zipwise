"""Asks what to do when an extracted file already exists."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr
from ..model import ConflictAction


class ConflictDialog(QDialog):
    """Skip / Rename / Overwrite for one path, optionally for all further conflicts."""

    def __init__(self, path: Path | str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("conflict.title"))
        self.setMinimumWidth(440)
        self._action = ConflictAction.SKIP

        message = QLabel(tr("conflict.message", path=str(path)))
        message.setWordWrap(True)
        self.apply_all = QCheckBox(tr("conflict.apply_all"))
        self.apply_all.setAccessibleName(tr("conflict.apply_all"))

        row = QHBoxLayout()
        self.buttons: dict[ConflictAction, QPushButton] = {}
        for action, key in (
            (ConflictAction.SKIP, "conflict.skip"),
            (ConflictAction.RENAME, "conflict.rename"),
            (ConflictAction.OVERWRITE, "conflict.overwrite"),
        ):
            button = QPushButton(tr(key))
            button.clicked.connect(lambda _=False, a=action: self._choose(a))
            self.buttons[action] = button
            row.addWidget(button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(message)
        layout.addWidget(self.apply_all)
        layout.addLayout(row)

    def _choose(self, action: ConflictAction) -> None:
        self._action = action
        self.accept()

    def choice(self) -> tuple[ConflictAction, bool]:
        return self._action, self.apply_all.isChecked()
