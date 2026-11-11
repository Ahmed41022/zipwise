"""Dialog that collects everything needed to create an archive."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView,
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

from ..errors import ZipwiseError
from ..i18n import tr
from ..model import CreateOptions, FormatId
from ..registry import CAPABILITIES, capability, get_writer_class
from ..settings import Settings
from ..split import PRESETS, parse_size
from .textutil import localize_buttons

SPLIT_CHOICES = ("none", "email", "whatsapp", "telegram", "fat32", "custom")
LEVELS = ("fast", "normal", "best")


def creatable_formats() -> list[FormatId]:
    """Formats that can be written here: a writer exists and its backend loads."""
    result: list[FormatId] = []
    for fmt, cap in CAPABILITIES.items():
        if cap.writer is None:
            continue
        try:
            get_writer_class(fmt)
        except ZipwiseError:
            continue
        result.append(fmt)
    return result


def formats_for_sources(sources: list[Path]) -> list[FormatId]:
    """Single-file formats are offered only for exactly one regular file."""
    single_ok = len(sources) == 1 and sources[0].is_file()
    return [f for f in creatable_formats() if capability(f).multi_entry or single_ok]


def level_text(level: str) -> str:
    if level == "fast":
        return tr("level.fast")
    if level == "best":
        return tr("level.best")
    return tr("level.normal")


def split_text(choice: str) -> str:
    if choice == "email":
        return tr("split.email")
    if choice == "whatsapp":
        return tr("split.whatsapp")
    if choice == "telegram":
        return tr("split.telegram")
    if choice == "fat32":
        return tr("split.fat32")
    if choice == "custom":
        return tr("split.custom")
    return tr("split.none")


def suggest_destination(sources: list[Path], fmt: FormatId) -> Path | None:
    first = sources[0] if sources else None
    if first is None:
        return None
    base = first.name or "archive"
    if len(sources) > 1:
        base = first.parent.name or "archive"
    return first.parent / f"{base}.{fmt.value}"


class CreateDialog(QDialog):
    def __init__(
        self,
        sources: list[Path] | None = None,
        settings: Settings | None = None,
        parent: QWidget | None = None,
        *,
        pick_files: Callable[[], list[Path]] | None = None,
        pick_folder: Callable[[], Path | None] | None = None,
        pick_save: Callable[[str], Path | None] | None = None,
    ) -> None:
        super().__init__(parent)
        settings = settings or Settings()
        self.setWindowTitle(tr("create.title"))
        self.setMinimumWidth(520)
        self._pick_files = pick_files or self._default_pick_files
        self._pick_folder = pick_folder or self._default_pick_folder
        self._pick_save = pick_save or self._default_pick_save
        self._dest_edited = False

        self.source_list = QListWidget()
        self.source_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.source_list.setAccessibleName(tr("create.sources"))
        self.add_files_button = QPushButton(tr("create.add_files"))
        self.add_folder_button = QPushButton(tr("create.add_folder"))
        self.remove_button = QPushButton(tr("create.remove"))
        self.add_files_button.clicked.connect(self._on_add_files)
        self.add_folder_button.clicked.connect(self._on_add_folder)
        self.remove_button.clicked.connect(self._on_remove)
        side = QVBoxLayout()
        for b in (self.add_files_button, self.add_folder_button, self.remove_button):
            side.addWidget(b)
        side.addStretch(1)
        sources_row = QHBoxLayout()
        sources_row.addWidget(self.source_list, 1)
        sources_row.addLayout(side)

        self.dest_edit = QLineEdit()
        self.dest_edit.setAccessibleName(tr("create.destination"))
        self.dest_edit.textEdited.connect(self._on_dest_edited)
        self.dest_edit.textChanged.connect(self._update_state)
        self.browse_button = QPushButton(tr("button.browse"))
        self.browse_button.clicked.connect(self._on_browse)
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_edit, 1)
        dest_row.addWidget(self.browse_button)

        self.format_combo = QComboBox()
        self.format_combo.setAccessibleName(tr("create.format"))
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        self.level_combo = QComboBox()
        self.level_combo.setAccessibleName(tr("create.level"))
        for level in LEVELS:
            self.level_combo.addItem(level_text(level), level)
        idx = self.level_combo.findData(settings.default_level)
        self.level_combo.setCurrentIndex(idx if idx >= 0 else 1)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setAccessibleName(tr("field.password"))
        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_edit.setAccessibleName(tr("field.confirm_password"))
        self.password_edit.textChanged.connect(self._update_state)
        self.confirm_edit.textChanged.connect(self._update_state)
        self.mismatch_label = QLabel("")
        self.encrypt_names_check = QCheckBox(tr("create.encrypt_names"))
        self.encrypt_names_check.setAccessibleName(tr("create.encrypt_names"))

        self.split_combo = QComboBox()
        self.split_combo.setAccessibleName(tr("create.split"))
        for choice in SPLIT_CHOICES:
            self.split_combo.addItem(split_text(choice), choice)
        self.split_combo.currentIndexChanged.connect(self._update_state)
        self.split_edit = QLineEdit()
        self.split_edit.setPlaceholderText(tr("create.split_placeholder"))
        self.split_edit.setAccessibleName(tr("create.split_size"))
        self.split_edit.textChanged.connect(self._update_state)

        self.verify_check = QCheckBox(tr("create.verify"))
        self.verify_check.setAccessibleName(tr("create.verify"))
        self.verify_check.setChecked(settings.verify_after_create)

        form = QFormLayout()
        form.addRow(tr("create.destination"), dest_row)
        form.addRow(tr("create.format"), self.format_combo)
        form.addRow(tr("create.level"), self.level_combo)
        form.addRow(tr("field.password"), self.password_edit)
        form.addRow(tr("field.confirm_password"), self.confirm_edit)
        form.addRow("", self.mismatch_label)
        form.addRow("", self.encrypt_names_check)
        form.addRow(tr("create.split"), self.split_combo)
        form.addRow("", self.split_edit)
        form.addRow("", self.verify_check)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        localize_buttons(self.buttons)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(QLabel(tr("create.sources")))
        layout.addLayout(sources_row)
        layout.addLayout(form)
        layout.addWidget(self.buttons)

        self.setTabOrder(self.source_list, self.add_files_button)
        self.setTabOrder(self.add_files_button, self.add_folder_button)
        self.setTabOrder(self.add_folder_button, self.remove_button)
        self.setTabOrder(self.remove_button, self.dest_edit)
        self.setTabOrder(self.dest_edit, self.browse_button)
        self.setTabOrder(self.browse_button, self.format_combo)
        self.setTabOrder(self.format_combo, self.level_combo)
        self.setTabOrder(self.level_combo, self.password_edit)
        self.setTabOrder(self.password_edit, self.confirm_edit)

        wanted = settings.default_format
        self._preferred_format = wanted
        for src in sources or []:
            self.source_list.addItem(str(src))
        self._refresh_formats()
        self._update_state()

    # -- file pickers -----------------------------------------------------------------------

    def _default_pick_files(self) -> list[Path]:
        names, _ = QFileDialog.getOpenFileNames(self, tr("create.add_files"))
        return [Path(n) for n in names]

    def _default_pick_folder(self) -> Path | None:
        name = QFileDialog.getExistingDirectory(self, tr("create.add_folder"))
        return Path(name) if name else None

    def _default_pick_save(self, suggested: str) -> Path | None:
        name, _ = QFileDialog.getSaveFileName(self, tr("create.destination"), suggested)
        return Path(name) if name else None

    # -- state ------------------------------------------------------------------------------

    def sources(self) -> list[Path]:
        return [Path(self.source_list.item(i).text()) for i in range(self.source_list.count())]

    def destination(self) -> Path:
        return Path(self.dest_edit.text().strip())

    def selected_format(self) -> FormatId:
        data = self.format_combo.currentData()
        return data if isinstance(data, FormatId) else FormatId.ZIP

    def available_formats(self) -> list[FormatId]:
        return [self.format_combo.itemData(i) for i in range(self.format_combo.count())]

    def _add_source(self, path: Path) -> None:
        if path not in self.sources():
            self.source_list.addItem(str(path))

    def _on_add_files(self) -> None:
        for p in self._pick_files():
            self._add_source(p)
        self._sources_changed()

    def _on_add_folder(self) -> None:
        folder = self._pick_folder()
        if folder is not None:
            self._add_source(folder)
        self._sources_changed()

    def _on_remove(self) -> None:
        for item in self.source_list.selectedItems():
            self.source_list.takeItem(self.source_list.row(item))
        self._sources_changed()

    def _sources_changed(self) -> None:
        self._refresh_formats()
        self._update_state()

    def _refresh_formats(self) -> None:
        previous = self.format_combo.currentData()
        wanted = previous.value if isinstance(previous, FormatId) else self._preferred_format
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        for fmt in formats_for_sources(self.sources()):
            self.format_combo.addItem(fmt.value, fmt)
        idx = self.format_combo.findText(wanted)
        self.format_combo.setCurrentIndex(max(idx, 0))
        self.format_combo.blockSignals(False)
        self._on_format_changed()

    def _on_format_changed(self) -> None:
        if self.format_combo.count() and not self._dest_edited:
            dest = suggest_destination(self.sources(), self.selected_format())
            self.dest_edit.setText(str(dest) if dest else "")
        self._update_state()

    def _on_dest_edited(self) -> None:
        self._dest_edited = True

    def _on_browse(self) -> None:
        picked = self._pick_save(self.dest_edit.text())
        if picked is not None:
            self._dest_edited = True
            self.dest_edit.setText(str(picked))

    def can_encrypt(self) -> bool:
        return self.format_combo.count() > 0 and capability(self.selected_format()).can_encrypt

    def passwords_match(self) -> bool:
        return self.password_edit.text() == self.confirm_edit.text()

    def split_size(self) -> int | None:
        choice = self.split_combo.currentData()
        if choice == "none":
            return None
        if choice == "custom":
            return parse_size(self.split_edit.text())
        return PRESETS[str(choice)]

    def _split_valid(self) -> bool:
        try:
            self.split_size()
        except ZipwiseError:
            return False
        return True

    def _update_state(self) -> None:
        encrypt = self.can_encrypt()
        for widget in (self.password_edit, self.confirm_edit):
            widget.setEnabled(encrypt)
        if not encrypt:
            self.password_edit.clear()
            self.confirm_edit.clear()
        has_password = encrypt and bool(self.password_edit.text())
        self.encrypt_names_check.setEnabled(
            has_password
            and self.format_combo.count() > 0
            and self.selected_format() is FormatId.SEVENZ
        )
        if not self.encrypt_names_check.isEnabled():
            self.encrypt_names_check.setChecked(False)
        match = self.passwords_match()
        self.mismatch_label.setText("" if match else tr("create.password_mismatch"))
        self.split_edit.setEnabled(self.split_combo.currentData() == "custom")
        ok = (
            bool(self.sources())
            and self.format_combo.count() > 0
            and bool(self.dest_edit.text().strip())
            and match
            and self._split_valid()
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def ok_enabled(self) -> bool:
        return self.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()

    def options(self) -> CreateOptions:
        password = self.password_edit.text() if self.can_encrypt() else ""
        level = self.level_combo.currentData()
        return CreateOptions(
            format=self.selected_format(),
            level=str(level) if level else "normal",  # type: ignore[arg-type]
            password=password or None,
            encrypt_names=bool(password) and self.encrypt_names_check.isChecked(),
            verify=self.verify_check.isChecked(),
            split_size=self.split_size(),
        )
