"""Asks for an archive password."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr
from .textutil import localize_buttons


class PasswordDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, *, wrong: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("password.title"))
        self.setMinimumWidth(360)
        text = tr("password.wrong") if wrong else tr("password.prompt")
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setAccessibleName(tr("field.password"))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        localize_buttons(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addWidget(self.edit)
        layout.addWidget(buttons)

    def password(self) -> str:
        return self.edit.text()
