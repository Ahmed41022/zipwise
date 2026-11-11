from __future__ import annotations

from datetime import datetime

import pytest
from PySide6.QtCore import Qt

from zipwise.gui.entry_model import (
    EntryTableModel,
    Row,
    breadcrumb_parts,
    format_mtime,
    format_ratio,
    format_size,
    normalise_name,
    parent_dir,
    rows_in_dir,
    sort_rows,
    type_label,
)
from zipwise.model import ArchiveEntry, EntryType


def f(name: str, size: int = 10, packed: int | None = 5, **kw: object) -> ArchiveEntry:
    return ArchiveEntry(name=name, size=size, compressed_size=packed, **kw)  # type: ignore[arg-type]


def d(name: str) -> ArchiveEntry:
    return ArchiveEntry(name=name, type=EntryType.DIR)


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (None, ""),
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (5 * 1024**2, "5.0 MB"),
        (3 * 1024**3, "3.0 GB"),
        (2 * 1024**4, "2.0 TB"),
    ],
)
def test_format_size(size: int | None, text: str) -> None:
    assert format_size(size) == text


@pytest.mark.parametrize(
    ("size", "packed", "text"),
    [
        (100, 50, "50%"),
        (100, 100, ""),
        (7, 9, ""),
        (1000, 999, "99%"),
        (1000, 1, "0%"),
        (100, None, ""),
        (0, 0, ""),
        (None, 5, ""),
    ],
)
def test_format_ratio(size: int | None, packed: int | None, text: str) -> None:
    assert format_ratio(size, packed) == text


def test_format_mtime() -> None:
    assert format_mtime(datetime(2024, 3, 5, 9, 7, 59)) == "2024-03-05 09:07"
    assert format_mtime(None) == ""


@pytest.mark.parametrize(
    ("raw", "clean"),
    [("a/b/", "a/b"), ("./a//b", "a/b"), ("a\\b", "a/b"), ("", ""), ("/", "")],
)
def test_normalise_name(raw: str, clean: str) -> None:
    assert normalise_name(raw) == clean


def test_parent_dir_and_breadcrumbs() -> None:
    assert parent_dir("a/b/c") == "a/b"
    assert parent_dir("a") == ""
    assert breadcrumb_parts("a/b") == ["a", "b"]
    assert breadcrumb_parts("") == []


def test_implied_directories_become_folder_rows() -> None:
    rows = rows_in_dir([f("a/b/c.txt"), f("a/d.txt"), f("top.txt")])
    assert [(r.name, r.is_dir) for r in rows] == [("a", True), ("top.txt", False)]
    assert rows[0].entry is None


def test_explicit_directory_entry_wins_over_implied() -> None:
    rows = rows_in_dir([f("a/x.txt"), d("a/")])
    assert len(rows) == 1
    assert rows[0].entry is not None


def test_rows_in_subdirectory() -> None:
    entries = [f("a/b/c.txt"), f("a/d.txt"), d("a"), f("z.txt")]
    rows = rows_in_dir(entries, "a")
    assert sorted((r.name, r.path) for r in rows) == [("b", "a/b"), ("d.txt", "a/d.txt")]


def test_directory_entry_itself_is_not_listed_inside_itself() -> None:
    assert rows_in_dir([d("a")], "a") == []


def test_sibling_with_common_prefix_is_not_a_child() -> None:
    assert rows_in_dir([f("ab/x.txt")], "a") == []


def test_link_rows_and_type_labels() -> None:
    rows = rows_in_dir(
        [ArchiveEntry(name="l", type=EntryType.SYMLINK), f("x.TXT"), f("noext"), d("dir")]
    )
    labels = {r.name: type_label(r) for r in rows}
    assert labels == {"l": "Link", "x.TXT": "TXT file", "noext": "File", "dir": "Folder"}


def test_sort_keeps_folders_first_both_directions() -> None:
    rows = [
        Row("b", "b", False, 1),
        Row("z", "z", True),
        Row("a", "a", False, 9),
        Row("m", "m", True),
    ]
    for descending in (False, True):
        out = sort_rows(rows, 0, descending=descending)
        assert [r.is_dir for r in out] == [True, True, False, False]
    assert [r.name for r in sort_rows(rows, 0)] == ["m", "z", "a", "b"]
    assert [r.name for r in sort_rows(rows, 1, descending=True)] == ["z", "m", "a", "b"]


def test_sort_by_size_and_ratio() -> None:
    rows = [
        Row("a", "a", False, 100, 90),
        Row("b", "b", False, 10, 1),
        Row("c", "c", False, 50, None),
    ]
    assert [r.name for r in sort_rows(rows, 1)] == ["b", "c", "a"]
    assert [r.name for r in sort_rows(rows, 3)] == ["c", "b", "a"]  # unknown ratio first


@pytest.fixture
def model(qtbot) -> EntryTableModel:
    m = EntryTableModel()
    m.set_entries(
        [
            f("docs/readme.txt", 1000, 400),
            f("docs/img/a.png", 2048, 2000),
            f("zeta.bin", 5, 5),
            f("alpha.txt", 70, 20),
        ]
    )
    return m


def cell(m: EntryTableModel, row: int, col: int) -> object:
    return m.data(m.index(row, col))


def test_model_columns_and_top_rows(model: EntryTableModel) -> None:
    assert model.columnCount() == 6
    assert model.headerData(0, Qt.Orientation.Horizontal) == "Name"
    assert model.headerData(5, Qt.Orientation.Horizontal) == "Type"
    assert [cell(model, r, 0) for r in range(model.rowCount())] == ["docs", "alpha.txt", "zeta.bin"]
    assert cell(model, 1, 1) == "70 B"
    assert cell(model, 1, 3) == "29%"
    assert cell(model, 0, 1) == ""


def test_model_alignment_numeric_columns_trailing(model: EntryTableModel) -> None:
    role = Qt.ItemDataRole.TextAlignmentRole
    size_align = model.data(model.index(1, 1), role)
    name_align = model.data(model.index(1, 0), role)
    assert size_align & int(Qt.AlignmentFlag.AlignTrailing)
    assert name_align & int(Qt.AlignmentFlag.AlignLeading)


def test_model_navigation_in_and_up(model: EntryTableModel) -> None:
    model.set_current_dir("docs")
    assert [cell(model, r, 0) for r in range(model.rowCount())] == ["img", "readme.txt"]
    assert model.can_go_up()
    assert model.go_up()
    assert model.current_dir == ""
    assert not model.go_up()


def test_model_sort_by_size_descending_keeps_folder_first(model: EntryTableModel) -> None:
    model.sort(1, Qt.SortOrder.DescendingOrder)
    assert [cell(model, r, 0) for r in range(model.rowCount())] == ["docs", "alpha.txt", "zeta.bin"]
    model.sort(0, Qt.SortOrder.DescendingOrder)
    assert [cell(model, r, 0) for r in range(model.rowCount())] == ["docs", "zeta.bin", "alpha.txt"]


def test_selected_names_and_entry_at(model: EntryTableModel) -> None:
    assert model.selected_names([2, 0, 0, 99]) == ["docs", "zeta.bin"]
    assert model.entry_at(1).name == "alpha.txt"


def test_set_entries_resets_directory(model: EntryTableModel) -> None:
    model.set_current_dir("docs")
    model.set_entries([f("q.txt")])
    assert model.current_dir == ""
    assert model.rowCount() == 1
