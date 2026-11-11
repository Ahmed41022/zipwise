"""Small helpers shared by the widgets."""

from __future__ import annotations

from PySide6.QtWidgets import QDialogButtonBox

from ..i18n import is_rtl, tr

_LRI = "⁦"
_PDI = "⁩"


def ltr(text: str) -> str:
    """Keep numbers, sizes and dates in reading order inside right-to-left text."""
    return f"{_LRI}{text}{_PDI}" if text and is_rtl() else text


def localize_buttons(box: QDialogButtonBox) -> None:
    """Qt ships no catalogue for the standard buttons, so set their texts from ours."""
    ok = box.button(QDialogButtonBox.StandardButton.Ok)
    if ok is not None:
        ok.setText(tr("button.ok"))
    cancel = box.button(QDialogButtonBox.StandardButton.Cancel)
    if cancel is not None:
        cancel.setText(tr("button.cancel"))
