"""Progress window for a running job, turning into a short report when there are issues."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr
from ..jobs import ProgressEvent
from ..model import OperationReport
from .entry_model import format_size
from .textutil import ltr


def phase_text(phase: str) -> str:
    """Translated name of a job phase."""
    if phase == "scan":
        return tr("phase.scan")
    if phase == "compress":
        return tr("phase.compress")
    if phase == "verify":
        return tr("phase.verify")
    if phase == "split":
        return tr("phase.split")
    if phase == "extract":
        return tr("phase.extract")
    if phase == "test":
        return tr("phase.test")
    if phase == "join":
        return tr("phase.join")
    return phase


class ProgressDialog(QDialog):
    """Shows phase, current entry and bytes; the Cancel button asks the job to stop."""

    cancel_requested = Signal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self._cancelling = False
        self._showing_report = False
        self._finished_flag = False

        self.phase_label = QLabel(tr("progress.preparing"))
        self.phase_label.setAccessibleName(tr("progress.phase"))
        self.entry_label = QLabel("")
        self.entry_label.setAccessibleName(tr("progress.entry"))
        self.entry_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.entry_label.setMinimumWidth(380)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setAccessibleName(tr("progress.bar"))
        self.bytes_label = QLabel("")
        self.bytes_label.setAccessibleName(tr("progress.bytes"))

        self.issues_list = QListWidget()
        self.issues_list.setAccessibleName(tr("progress.issues"))
        self.issues_list.hide()
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.hide()

        self.cancel_button = QPushButton(tr("button.cancel"))
        self.cancel_button.clicked.connect(self._on_cancel)
        self.close_button = QPushButton(tr("button.close"))
        self.close_button.clicked.connect(self.accept)
        self.close_button.hide()
        buttons = QDialogButtonBox()
        buttons.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.addButton(self.close_button, QDialogButtonBox.ButtonRole.AcceptRole)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        for widget in (
            self.phase_label,
            self.entry_label,
            self.bar,
            self.bytes_label,
            self.summary_label,
            self.issues_list,
            buttons,
        ):
            layout.addWidget(widget)

    # -- progress -------------------------------------------------------------------------

    def on_progress(self, event: ProgressEvent) -> None:
        if self._showing_report:
            return
        if not self._cancelling:
            self.phase_label.setText(phase_text(event.phase))
        self.entry_label.setText(
            self.entry_label.fontMetrics().elidedText(
                event.entry_name, Qt.TextElideMode.ElideMiddle, 420
            )
        )
        if event.bytes_total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(min(1000, int(event.bytes_done * 1000 / event.bytes_total)))
            self.bytes_label.setText(
                tr(
                    "progress.bytes_of",
                    done=ltr(format_size(event.bytes_done)),
                    total=ltr(format_size(event.bytes_total)),
                )
            )
        elif event.entries_total > 0:
            self.bar.setRange(0, event.entries_total)
            self.bar.setValue(min(event.entries_total, event.entries_done))
            self.bytes_label.setText(
                tr("progress.entries_of", done=event.entries_done, total=event.entries_total)
            )
        else:
            self.bar.setRange(0, 0)
            self.bytes_label.setText(format_size(event.bytes_done) if event.bytes_done else "")

    def _on_cancel(self) -> None:
        if self._cancelling:
            return
        self._cancelling = True
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText(tr("button.cancelling"))
        self.phase_label.setText(tr("progress.cancelling"))
        self.cancel_requested.emit()

    # -- end of the job -------------------------------------------------------------------

    def job_finished(self, result: object) -> None:
        """Close, or switch to the report view when the result lists issues."""
        if isinstance(result, OperationReport) and result.issues:
            self.show_report(result)
        else:
            self.accept()

    def show_report(self, report: OperationReport) -> None:
        self._showing_report = True
        self.phase_label.setText(tr("progress.done_with_issues"))
        self.entry_label.hide()
        self.bar.hide()
        self.bytes_label.hide()
        self.cancel_button.hide()
        self.close_button.show()
        self.close_button.setDefault(True)
        self.summary_label.setText(
            tr(
                "progress.summary",
                done=report.entries_done,
                skipped=report.entries_skipped,
                issues=len(report.issues),
            )
        )
        self.summary_label.show()
        self.issues_list.clear()
        for issue in report.issues:
            where = f"{issue.entry}: " if issue.entry else ""
            item = QListWidgetItem(f"[{issue.severity}] {where}{issue.message}")
            item.setToolTip(issue.code)
            self.issues_list.addItem(item)
        self.issues_list.show()

    @property
    def showing_report(self) -> bool:
        return self._showing_report

    @property
    def cancelling(self) -> bool:
        return self._cancelling

    def closeEvent(self, event: QCloseEvent) -> None:
        # Closing the window while the job runs means "cancel"; the owner closes it afterwards.
        if not self._showing_report and self.isVisible() and not self._finished_flag:
            self._on_cancel()
            event.ignore()
            return
        super().closeEvent(event)

    def done(self, result: int) -> None:
        self._finished_flag = True
        super().done(result)

    def reject(self) -> None:
        if self._showing_report or self._finished_flag:
            super().reject()
        else:
            self._on_cancel()
