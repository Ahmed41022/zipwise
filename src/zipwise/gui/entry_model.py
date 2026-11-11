"""Table model for the entries of one folder of an archive, plus the pure logic behind it."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPersistentModelIndex, Qt
from PySide6.QtGui import QIcon

from ..i18n import tr
from ..model import ArchiveEntry, EntryType
from .textutil import ltr

COLUMNS = ("name", "size", "packed", "ratio", "modified", "type")
NUMERIC_COLUMNS = frozenset({1, 2, 3})
_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

_Index = QModelIndex | QPersistentModelIndex


@dataclass(frozen=True)
class Row:
    """One line of the table: a file, a link or a folder directly inside the current folder."""

    name: str
    path: str
    is_dir: bool
    size: int | None = None
    packed: int | None = None
    mtime: datetime | None = None
    is_link: bool = False
    entry: ArchiveEntry | None = None

    @property
    def extension(self) -> str:
        if self.is_dir or "." not in self.name.strip("."):
            return ""
        return self.name.rsplit(".", 1)[1]


def format_size(size: int | None) -> str:
    """``1536`` -> ``1.5 KB``; blank for an unknown size."""
    if size is None:
        return ""
    value = float(size)
    unit = 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {_UNITS[unit]}"


def ratio_value(size: int | None, packed: int | None) -> float | None:
    """Packed size as a fraction of the original; None when it cannot be told."""
    if size is None or packed is None or size <= 0:
        return None
    return packed / size


def format_ratio(size: int | None, packed: int | None) -> str:
    """Packed size as a percentage, blank unless packing made the data smaller."""
    ratio = ratio_value(size, packed)
    if ratio is None or ratio >= 1:
        return ""
    return f"{min(ratio * 100, 99):.0f}%"


def format_mtime(value: datetime | None) -> str:
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M")


def normalise_name(name: str) -> str:
    """Archive path without ``./`` parts, empty parts or a trailing slash."""
    return "/".join(p for p in name.replace("\\", "/").split("/") if p not in ("", "."))


def parent_dir(path: str) -> str:
    return path.rpartition("/")[0]


def join_dir(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def breadcrumb_parts(directory: str) -> list[str]:
    return [p for p in directory.split("/") if p]


def rows_in_dir(entries: Iterable[ArchiveEntry], directory: str = "") -> list[Row]:
    """Rows for the direct children of ``directory``.

    Folders that only exist because a deeper path names them get a synthetic row; an explicit
    directory entry wins over a synthetic one.
    """
    directory = normalise_name(directory)
    prefix = f"{directory}/" if directory else ""
    folders: dict[str, Row] = {}
    files: list[Row] = []
    for entry in entries:
        path = normalise_name(entry.name)
        if not path or not path.startswith(prefix) or path == directory:
            continue
        first, *rest = path[len(prefix) :].split("/")
        full = prefix + first
        if rest:
            folders.setdefault(first, Row(first, full, True))
        elif entry.type is EntryType.DIR:
            folders[first] = Row(first, full, True, None, None, entry.mtime, entry=entry)
        else:
            files.append(
                Row(
                    first,
                    full,
                    False,
                    entry.size,
                    entry.compressed_size,
                    entry.mtime,
                    is_link=entry.type in (EntryType.SYMLINK, EntryType.HARDLINK),
                    entry=entry,
                )
            )
    return [*folders.values(), *files]


def type_label(row: Row) -> str:
    if row.is_dir:
        return tr("type.folder")
    if row.is_link:
        return tr("type.link")
    if row.extension:
        return tr("type.file_ext", ext=row.extension.upper())
    return tr("type.file")


def _mtime_key(value: datetime | None) -> float:
    if value is None:
        return -1.0
    return value.toordinal() * 86400.0 + value.hour * 3600 + value.minute * 60 + value.second


def sort_key(row: Row, column: int) -> tuple[float, str] | str:
    """Sort key of ``row`` for ``column`` (index into :data:`COLUMNS`)."""
    if column == 1:
        return (-1.0 if row.size is None else float(row.size), row.name.casefold())
    if column == 2:
        return (-1.0 if row.packed is None else float(row.packed), row.name.casefold())
    if column == 3:
        ratio = ratio_value(row.size, row.packed)
        return (-1.0 if ratio is None else ratio, row.name.casefold())
    if column == 4:
        return (_mtime_key(row.mtime), row.name.casefold())
    if column == 5:
        return type_label(row).casefold() + "\0" + row.name.casefold()
    return row.name.casefold()


def sort_rows(rows: Sequence[Row], column: int, *, descending: bool = False) -> list[Row]:
    """Sort by ``column``; folders always stay in front of files."""
    folders = sorted((r for r in rows if r.is_dir), key=lambda r: sort_key(r, column))
    files = sorted((r for r in rows if not r.is_dir), key=lambda r: sort_key(r, column))
    if descending:
        folders.reverse()
        files.reverse()
    return [*folders, *files]


class EntryTableModel(QAbstractTableModel):
    """Shows the current folder of an archive; sorting is done here, not in a proxy."""

    def __init__(
        self,
        parent: QObject | None = None,
        icon_provider: Callable[[Row], QIcon] | None = None,
    ) -> None:
        super().__init__(parent)
        self._entries: list[ArchiveEntry] = []
        self._dir = ""
        self._rows: list[Row] = []
        self._sort_column = 0
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._icon_provider = icon_provider

    # -- data -----------------------------------------------------------------------------

    def set_entries(self, entries: Sequence[ArchiveEntry]) -> None:
        self._entries = list(entries)
        self._dir = ""
        self._rebuild()

    def clear(self) -> None:
        self.set_entries([])

    @property
    def current_dir(self) -> str:
        return self._dir

    def set_current_dir(self, path: str) -> None:
        self._dir = normalise_name(path)
        self._rebuild()

    def go_up(self) -> bool:
        if not self._dir:
            return False
        self.set_current_dir(parent_dir(self._dir))
        return True

    def can_go_up(self) -> bool:
        return bool(self._dir)

    def entry_at(self, row: int) -> Row:
        return self._rows[row]

    def rows(self) -> list[Row]:
        return list(self._rows)

    def selected_names(self, rows: Iterable[int]) -> list[str]:
        """Archive paths for the given table rows (a folder stands for everything below it)."""
        return [self._rows[r].path for r in sorted(set(rows)) if 0 <= r < len(self._rows)]

    def _rebuild(self) -> None:
        self.beginResetModel()
        rows = rows_in_dir(self._entries, self._dir)
        self._rows = sort_rows(
            rows, self._sort_column, descending=self._sort_order == Qt.SortOrder.DescendingOrder
        )
        self.endResetModel()

    def retranslate(self) -> None:
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(COLUMNS) - 1)
        self._rebuild()

    # -- Qt interface ---------------------------------------------------------------------

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr(f"column.{COLUMNS[section]}")
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                row.name,
                "" if row.is_dir else ltr(format_size(row.size)),
                "" if row.is_dir else ltr(format_size(row.packed)),
                "" if row.is_dir else ltr(format_ratio(row.size, row.packed)),
                ltr(format_mtime(row.mtime)),
                type_label(row),
            )[col]
        if role == Qt.ItemDataRole.DecorationRole and col == 0 and self._icon_provider:
            return self._icon_provider(row)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            horizontal = (
                Qt.AlignmentFlag.AlignTrailing
                if col in NUMERIC_COLUMNS
                else Qt.AlignmentFlag.AlignLeading
            )
            return int(horizontal | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole and col == 0:
            return row.path
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self._sort_column = column
        self._sort_order = order
        self._rebuild()
