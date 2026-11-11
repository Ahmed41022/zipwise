"""Architecture rules enforced by scanning imports."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "zipwise"
TESTS = Path(__file__).resolve().parents[1]


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                package = ".".join(
                    path.relative_to(SRC.parent).with_suffix("").parts[: -node.level]
                )
                base = f"{package}.{base}".strip(".") if base else package
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def package_of(path: Path) -> str:
    rel = path.relative_to(SRC).parts
    return rel[0] if len(rel) > 1 else ""


SOURCE_FILES = sorted(SRC.rglob("*.py"))


def test_source_tree_is_found() -> None:
    assert len(SOURCE_FILES) > 20


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: str(p.relative_to(SRC)))
def test_pyside6_only_inside_gui(path: Path) -> None:
    uses_qt = any(m.split(".")[0] == "PySide6" for m in imported_modules(path))
    if package_of(path) != "gui":
        assert not uses_qt, f"{path} imports PySide6 outside zipwise.gui"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: str(p.relative_to(SRC)))
def test_cli_and_gui_do_not_import_each_other(path: Path) -> None:
    mods = imported_modules(path)
    pkg = package_of(path)
    if pkg == "cli":
        assert not any(m == "zipwise.gui" or m.startswith("zipwise.gui.") for m in mods)
    if pkg == "gui":
        assert not any(m == "zipwise.cli" or m.startswith("zipwise.cli.") for m in mods)


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: str(p.relative_to(SRC)))
def test_core_does_not_import_front_ends(path: Path) -> None:
    if package_of(path) in ("cli", "gui"):
        return
    mods = imported_modules(path)
    bad = [m for m in mods if m.startswith(("zipwise.cli", "zipwise.gui"))]
    assert not bad, f"{path} imports a front end: {bad}"


def test_tests_do_not_import_pyside6_outside_gui_tests() -> None:
    offenders = []
    for path in TESTS.rglob("*.py"):
        if "gui" in path.relative_to(TESTS).parts[:1]:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(n.split(".")[0] == "PySide6" for n in names):
                offenders.append(path)
    assert not offenders
