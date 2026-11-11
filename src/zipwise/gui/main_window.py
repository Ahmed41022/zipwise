"""Main window: browse an archive, start jobs, drag and drop."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStyle,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..detect import detect
from ..errors import Cancelled, WrongPassword, ZipwiseError
from ..i18n import available_languages, current_language, is_rtl, set_language, tr
from ..model import ArchiveInfo, ConflictAction, CreateOptions, OperationReport
from ..ops import archive_info, create_archive, extract_archive, list_archive, test_archive
from ..settings import Settings, load_settings, save_settings
from .conflict_dialog import ConflictDialog
from .create_dialog import CreateDialog
from .entry_model import (
    EntryTableModel,
    Row,
    breadcrumb_parts,
    format_ratio,
    format_size,
)
from .extract_dialog import ExtractDialog
from .password_dialog import PasswordDialog
from .progress_dialog import ProgressDialog
from .settings_dialog import LANGUAGE_NAMES, SettingsDialog
from .textutil import ltr
from .worker import Job, JobWorker

MAX_PASSWORD_ATTEMPTS = 3
JobFactory = Callable[[str | None], Job]


def apply_language(lang: str) -> str:
    """Select ``lang`` and mirror the layout of the whole application for right-to-left ones."""
    used = set_language(lang)
    app = QApplication.instance()
    if isinstance(app, QApplication):
        app.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft if is_rtl(used) else Qt.LayoutDirection.LeftToRight
        )
    return used


def paths_from_mime(urls: list[str]) -> list[Path]:
    return [Path(u) for u in urls if u]


def decide_drop(paths: list[Path]) -> str:
    """``"open"`` for a single recognised archive, ``"create"`` for anything else."""
    if len(paths) == 1 and paths[0].is_file():
        try:
            detect(paths[0])
        except ZipwiseError:
            return "create"
        return "open"
    return "create" if paths else "none"


def default_extract_dir(archive: Path) -> Path:
    name = archive.name
    if len(name) > 4 and name[-4] == "." and name[-3:].isdigit():
        name = name[:-4]
    stem = Path(name).stem
    if stem.lower().endswith(".tar"):
        stem = stem[:-4]
    return archive.parent / (stem or "extracted")


class MainWindow(QMainWindow):
    job_ended = Signal(str)  # "finished", "failed" or "cancelled"

    def __init__(self, settings: Settings | None = None, settings_path: Path | None = None) -> None:
        super().__init__()
        self.settings = settings or load_settings(settings_path)
        self.settings_path = settings_path
        self.archive_path: Path | None = None
        self.info: ArchiveInfo | None = None
        self._worker: JobWorker | None = None
        self._progress: ProgressDialog | None = None
        self.setAcceptDrops(True)
        self.resize(1000, 640)

        style = self.style()
        self.model = EntryTableModel(self, self._icon_for)
        self._folder_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self._file_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self._link_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileLinkIcon)

        self.path_label = QLabel("")
        self.path_label.setAccessibleName(tr("main.path"))
        self.path_label.setContentsMargins(4, 2, 4, 2)
        self.empty_label = QLabel("")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAccessibleName(tr("main.empty_hint"))

        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.view.setShowGrid(False)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().hide()
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setAccessibleName(tr("main.table"))
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.view.doubleClicked.connect(self._on_activated)
        self.view.activated.connect(self._on_activated)
        self.view.selectionModel().selectionChanged.connect(self._update_actions)
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Backspace), self.view)
        shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        shortcut.activated.connect(self.go_up)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(2)
        page_layout.addWidget(self.path_label)
        page_layout.addWidget(self.view, 1)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty_label)
        self.stack.addWidget(page)
        self.setCentralWidget(self.stack)

        self.status_label = QLabel("")
        self.statusBar().addWidget(self.status_label, 1)

        self._build_actions()
        self.setTabOrder(self.view, self.path_label)
        self.retranslate_ui()
        self._update_actions()

    # -- hooks that tests replace ---------------------------------------------------------

    def _pick_open_archive(self) -> Path | None:
        name, _ = QFileDialog.getOpenFileName(self, tr("action.open"))
        return Path(name) if name else None

    def _pick_directory(self) -> Path | None:
        name = QFileDialog.getExistingDirectory(self, tr("extract.destination"))
        return Path(name) if name else None

    def _pick_save_archive(self, suggested: str = "") -> Path | None:
        name, _ = QFileDialog.getSaveFileName(self, tr("create.destination"), suggested)
        return Path(name) if name else None

    def _exec_dialog(self, dialog: QDialog) -> bool:
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _show_progress(self, dialog: ProgressDialog) -> None:
        dialog.open()

    def _message(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(self, title, text)
        return answer == QMessageBox.StandardButton.Yes

    def _ask_password(self, wrong: bool = False) -> str | None:
        dialog = PasswordDialog(self, wrong=wrong)
        return dialog.password() if self._exec_dialog(dialog) else None

    def _ask_conflict(self, path: Path) -> tuple[ConflictAction, bool]:
        dialog = ConflictDialog(path, self)
        if self._exec_dialog(dialog):
            return dialog.choice()
        return ConflictAction.SKIP, False

    def show_error(self, exc: BaseException) -> None:
        if isinstance(exc, ZipwiseError):
            text = exc.message + (f"\n\n{exc.hint}" if exc.hint else "")
        else:
            text = f"{type(exc).__name__}: {exc}"
        QMessageBox.critical(self, tr("error.title"), text)

    # -- construction -----------------------------------------------------------------------

    def _icon_for(self, row: Row) -> QIcon:
        if row.is_dir:
            return self._folder_icon
        return self._link_icon if row.is_link else self._file_icon

    def _make_action(
        self, pixmap: QStyle.StandardPixmap, slot: Callable[[], object], shortcut: str = ""
    ) -> QAction:
        action = QAction(self.style().standardIcon(pixmap), "", self)
        action.triggered.connect(lambda _checked=False: slot())
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        return action

    def _build_actions(self) -> None:
        sp = QStyle.StandardPixmap
        self.act_new = self._make_action(sp.SP_FileDialogNewFolder, self.new_archive, "Ctrl+N")
        self.act_open = self._make_action(sp.SP_DialogOpenButton, self.open_dialog, "Ctrl+O")
        self.act_extract = self._make_action(sp.SP_DialogSaveButton, self.extract_all, "Ctrl+E")
        self.act_extract_sel = self._make_action(
            sp.SP_ArrowDown, self.extract_selected, "Ctrl+Shift+E"
        )
        self.act_test = self._make_action(sp.SP_DialogApplyButton, self.test_current, "Ctrl+T")
        self.act_info = self._make_action(sp.SP_MessageBoxInformation, self.show_info, "Ctrl+I")
        self.act_up = self._make_action(sp.SP_ArrowUp, self.go_up, "Alt+Up")
        self.act_settings = self._make_action(
            sp.SP_FileDialogDetailedView, self.open_settings, "Ctrl+,"
        )
        self.act_about = self._make_action(sp.SP_MessageBoxQuestion, self.show_about, "F1")
        self.act_quit = QAction(self)
        self.act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        self.act_quit.triggered.connect(self.close)

        self.language_actions: dict[str, QAction] = {}
        group = QActionGroup(self)
        for code in available_languages():
            act = QAction(LANGUAGE_NAMES.get(code, code), self)
            act.setCheckable(True)
            act.setActionGroup(group)
            act.triggered.connect(lambda _c=False, c=code: self.change_language(c))
            self.language_actions[code] = act

        self.file_menu = self.menuBar().addMenu("")
        for act in (self.act_new, self.act_open):
            self.file_menu.addAction(act)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.act_quit)
        self.archive_menu = self.menuBar().addMenu("")
        for act in (
            self.act_extract,
            self.act_extract_sel,
            self.act_test,
            self.act_info,
            self.act_up,
        ):
            self.archive_menu.addAction(act)
        self.tools_menu = self.menuBar().addMenu("")
        self.tools_menu.addAction(self.act_settings)
        self.language_menu = self.tools_menu.addMenu("")
        for act in self.language_actions.values():
            self.language_menu.addAction(act)
        self.help_menu = self.menuBar().addMenu("")
        self.help_menu.addAction(self.act_about)

        self.toolbar = self.addToolBar("main")
        self.toolbar.setMovable(False)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        for act in (
            self.act_new,
            self.act_open,
            self.act_extract,
            self.act_extract_sel,
            self.act_test,
            self.act_info,
            self.act_up,
        ):
            self.toolbar.addAction(act)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.act_settings)
        self.toolbar.addAction(self.act_about)

    def retranslate_ui(self) -> None:
        """Re-set every visible text for the active language."""
        name = self.archive_path.name if self.archive_path else None
        self.setWindowTitle(f"{name} - {tr('app.title')}" if name else tr("app.title"))
        for act, key in (
            (self.act_new, "action.new"),
            (self.act_open, "action.open"),
            (self.act_extract, "action.extract"),
            (self.act_extract_sel, "action.extract_selected"),
            (self.act_test, "action.test"),
            (self.act_info, "action.info"),
            (self.act_up, "action.up"),
            (self.act_settings, "action.settings"),
            (self.act_about, "action.about"),
            (self.act_quit, "action.quit"),
        ):
            act.setText(tr(key))
            act.setToolTip(tr(key))
        self.file_menu.setTitle(tr("menu.file"))
        self.archive_menu.setTitle(tr("menu.archive"))
        self.tools_menu.setTitle(tr("menu.tools"))
        self.language_menu.setTitle(tr("menu.language"))
        self.help_menu.setTitle(tr("menu.help"))
        self.language_actions[current_language()].setChecked(True)
        self.empty_label.setText(tr("main.empty_hint"))
        self.path_label.setAccessibleName(tr("main.path"))
        self.view.setAccessibleName(tr("main.table"))
        self.model.retranslate()
        self._refresh_path_label()
        self._refresh_status()
        self.stack.setCurrentIndex(1 if self.archive_path else 0)

    # -- state ------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._worker is not None

    def _update_actions(self, *_args: object) -> None:
        has = self.archive_path is not None and not self.busy
        selected = bool(self.view.selectionModel().selectedRows())
        self.act_extract.setEnabled(has)
        self.act_test.setEnabled(has)
        self.act_info.setEnabled(has)
        self.act_extract_sel.setEnabled(has and selected)
        self.act_up.setEnabled(has and self.model.can_go_up())
        self.act_new.setEnabled(not self.busy)
        self.act_open.setEnabled(not self.busy)

    def _refresh_path_label(self) -> None:
        if self.archive_path is None:
            self.path_label.setText("")
            return
        parts = [self.archive_path.name, *breadcrumb_parts(self.model.current_dir)]
        self.path_label.setText(" / ".join(parts))

    def _refresh_status(self) -> None:
        info = self.info
        if info is None:
            self.status_label.setText(tr("status.ready"))
            return
        pieces = [
            tr("status.entries", count=info.entries_count),
            tr("status.total", size=ltr(format_size(info.total_size))),
        ]
        ratio = format_ratio(info.total_size, info.compressed_size)
        if ratio:
            pieces.append(tr("status.ratio", ratio=ltr(ratio)))
        if info.name_encoding:
            pieces.append(tr("status.encoding", encoding=info.name_encoding))
        self.status_label.setText("   |   ".join(pieces))

    # -- opening and browsing ---------------------------------------------------------------

    def open_dialog(self) -> None:
        path = self._pick_open_archive()
        if path is not None:
            self.open_archive(path)

    def open_archive(self, path: Path | str) -> bool:
        path = Path(path)
        try:
            entries = list_archive(path)
            info = archive_info(path)
        except ZipwiseError as exc:
            self.show_error(exc)
            return False
        self.archive_path = path
        self.info = info
        self.model.set_entries(entries)
        self.view.clearSelection()
        self.retranslate_ui()
        self._update_actions()
        return True

    def _on_activated(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        row = self.model.entry_at(index.row())
        if row.is_dir:
            self.model.set_current_dir(row.path)
            self._after_navigation()

    def go_up(self) -> None:
        if self.model.go_up():
            self._after_navigation()

    def _after_navigation(self) -> None:
        self._refresh_path_label()
        self._update_actions()

    def selected_paths(self) -> list[str]:
        rows = [i.row() for i in self.view.selectionModel().selectedRows()]
        return self.model.selected_names(rows)

    # -- drag and drop ----------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        event.acceptProposedAction()
        self.handle_drop(paths_from_mime(urls))

    def handle_drop(self, paths: list[Path]) -> str:
        decision = decide_drop(paths)
        if decision == "open":
            self.open_archive(paths[0])
        elif decision == "create":
            self.new_archive(paths)
        return decision

    # -- jobs -------------------------------------------------------------------------------

    def _start_job(
        self,
        title: str,
        factory: JobFactory,
        on_done: Callable[[object], None],
        *,
        password: str | None = None,
        attempt: int = 0,
    ) -> None:
        if self.busy:
            return
        progress = ProgressDialog(title, self)
        worker = JobWorker(
            factory(password),
            conflict_handler=self._ask_conflict,
            password_handler=self._ask_password,
            parent=self,
        )
        self._worker, self._progress = worker, progress
        worker.progress.connect(progress.on_progress)
        progress.cancel_requested.connect(worker.cancel)
        worker.finished.connect(lambda result: self._job_finished(result, on_done))
        worker.failed.connect(lambda exc: self._job_failed(exc, title, factory, on_done, attempt))
        worker.cancelled.connect(self._job_cancelled)
        self._update_actions()
        self._show_progress(progress)
        worker.start()

    def _release_job(self) -> None:
        worker, progress = self._worker, self._progress
        self._worker = self._progress = None
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        if progress is not None:
            progress.deleteLater()
        self._update_actions()

    def _job_finished(self, result: object, on_done: Callable[[object], None]) -> None:
        progress = self._progress
        if progress is not None:
            progress.job_finished(result)
        self._release_job_keep_dialog(progress)
        try:
            on_done(result)
        except ZipwiseError as exc:
            self.show_error(exc)
        self.job_ended.emit("finished")

    def _release_job_keep_dialog(self, progress: ProgressDialog | None) -> None:
        """Like ``_release_job`` but leaves a report-showing dialog open until it is closed."""
        worker = self._worker
        self._worker = self._progress = None
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        if progress is not None:
            if progress.showing_report:
                progress.finished.connect(progress.deleteLater)
            else:
                progress.deleteLater()
        self._update_actions()

    def _job_failed(
        self,
        exc: object,
        title: str,
        factory: JobFactory,
        on_done: Callable[[object], None],
        attempt: int,
    ) -> None:
        progress = self._progress
        if progress is not None:
            progress.done(0)
        self._release_job()
        if isinstance(exc, WrongPassword) and attempt < MAX_PASSWORD_ATTEMPTS:
            password = self._ask_password(wrong=True)
            if password:
                self._start_job(title, factory, on_done, password=password, attempt=attempt + 1)
                return
        self.show_error(exc if isinstance(exc, BaseException) else Cancelled())
        self.job_ended.emit("failed")

    def _job_cancelled(self) -> None:
        if self._progress is not None:
            self._progress.done(0)
        self._release_job()
        self.status_label.setText(tr("status.cancelled"))
        self.job_ended.emit("cancelled")

    # -- operations -------------------------------------------------------------------------

    def new_archive(self, sources: list[Path] | None = None) -> None:
        dialog = CreateDialog(
            sources,
            self.settings,
            self,
            pick_save=self._pick_save_archive,
        )
        if not self._exec_dialog(dialog):
            return
        self.create_with(dialog.sources(), dialog.destination(), dialog.options())

    def create_with(self, sources: list[Path], dest: Path, options: CreateOptions) -> None:
        if dest.exists() and not self._confirm(
            tr("create.overwrite_title"), tr("create.overwrite_message", path=str(dest))
        ):
            return

        def factory(_password: str | None) -> Job:
            return lambda ctx: create_archive(sources, dest, options, ctx)

        def done(result: object) -> None:
            self.status_label.setText(tr("status.created", name=dest.name))
            # a split that was not needed still leaves one plain archive to open
            if isinstance(result, OperationReport) and len(result.output_paths) == 1:
                self.open_archive(dest)

        self._start_job(tr("create.progress_title"), factory, done)

    def _run_extract(self, dialog: ExtractDialog) -> None:
        archive = self.archive_path
        if archive is None:
            return
        dest, options = dialog.destination(), dialog.options()

        def factory(password: str | None) -> Job:
            opts = dataclasses.replace(options, password=password or options.password)
            return lambda ctx: extract_archive(archive, dest, opts, ctx)

        def done(_result: object) -> None:
            self.status_label.setText(tr("status.extracted", path=str(dest)))

        self._start_job(tr("extract.progress_title"), factory, done)

    def _extract(self, selected: list[str] | None) -> None:
        if self.archive_path is None:
            return
        dialog = ExtractDialog(
            self.archive_path,
            default_extract_dir(self.archive_path),
            self.settings,
            selected,
            self,
            pick_directory=self._pick_directory,
        )
        if self._exec_dialog(dialog):
            self._run_extract(dialog)

    def extract_all(self) -> None:
        self._extract(None)

    def extract_selected(self) -> None:
        names = self.selected_paths()
        if names:
            self._extract(names)

    def test_current(self) -> None:
        archive = self.archive_path
        if archive is None:
            return

        def factory(password: str | None) -> Job:
            return lambda ctx: test_archive(archive, password=password, ctx=ctx)

        def done(result: object) -> None:
            if isinstance(result, OperationReport) and result.ok:
                self._message(tr("test.title"), tr("test.ok", name=archive.name))
            elif isinstance(result, OperationReport):
                self._message(tr("test.title"), tr("test.failed", count=result.entries_skipped))

        self._start_job(tr("test.title"), factory, done)

    def info_text(self) -> str:
        info = self.info
        if info is None:
            return ""
        yes, no = tr("info.yes"), tr("info.no")
        lines = [
            tr("info.path", value=str(info.path)),
            tr("info.format", value=info.format.value),
            tr("info.entries", value=info.entries_count),
            tr("info.size", value=ltr(format_size(info.total_size))),
            tr("info.packed", value=ltr(format_size(info.compressed_size))),
            tr("info.encrypted", value=yes if info.encrypted else no),
            tr("info.multipart", value=yes if info.multipart else no),
        ]
        if info.name_encoding:
            lines.append(tr("info.encoding", value=info.name_encoding))
        if info.comment:
            lines.append(tr("info.comment", value=info.comment))
        return "\n".join(lines)

    def show_info(self) -> None:
        if self.info is not None:
            self._message(tr("info.title"), self.info_text())

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self.settings_path, self)
        if self._exec_dialog(dialog):
            self.settings = dialog.settings()
            if self.settings.language != current_language():
                self.change_language(self.settings.language, save=False)

    def change_language(self, lang: str, *, save: bool = True) -> None:
        """Applies at once: layout direction and every text of this window."""
        used = apply_language(lang)
        self.settings = dataclasses.replace(self.settings, language=used)
        if save:
            try:
                save_settings(self.settings, self.settings_path)
            except OSError as exc:
                self.status_label.setText(str(exc))
        self.retranslate_ui()

    def show_about(self) -> None:
        self._message(tr("about.title"), tr("about.text", version=__version__))

    # -- closing ----------------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        worker = self._worker
        if worker is not None:
            worker.shutdown()
            self._worker = None
        if self._progress is not None:
            self._progress.done(0)
            self._progress = None
        event.accept()
