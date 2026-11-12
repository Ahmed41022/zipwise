"""Shared assertions: open an archive with Zipwise and compare it with the tree it came from."""

from __future__ import annotations

from pathlib import Path

from helpers.roundtrip import compare_snapshots
from helpers.trees import snapshot
from zipwise.model import ArchiveEntry, EntryType, ExtractOptions
from zipwise.ops import extract_archive, list_archive
from zipwise.ops import test_archive as verify_archive
from zipwise.safety import SafetyPolicy


def expected_names(tree: Path) -> set[str]:
    """Entry names an archive made from ``tree`` (keeping the folder name) should list."""
    return {tree.name, *(f"{tree.name}/{rel}" for rel in snapshot(tree))}


def check_listing(archive: Path, tree: Path, *, password: str | None = None) -> list[ArchiveEntry]:
    entries = list_archive(archive, password=password)
    names = {e.name for e in entries}
    assert names == expected_names(tree)
    by_name = {e.name: e for e in entries}
    for rel, (kind, _digest, _mtime, _mode) in snapshot(tree).items():
        entry = by_name[f"{tree.name}/{rel}"]
        assert (entry.type is EntryType.DIR) == (kind == "dir"), rel
    return entries


def check_archive(
    archive: Path,
    tree: Path,
    work: Path,
    *,
    password: str | None = None,
    names: bool = True,
) -> None:
    """List, test and extract ``archive`` with Zipwise; names, bytes, empty folders and
    modification times (within 2 s) must match ``tree``."""
    if names:
        check_listing(archive, tree, password=password)
    report = verify_archive(archive, password=password)
    assert report.ok, report.issues
    dest = work / "extracted"
    result = extract_archive(
        archive,
        dest,
        ExtractOptions(password=password, policy=SafetyPolicy(sanitize_windows_names="never")),
    )
    assert result.ok, result.issues
    compare_snapshots(snapshot(tree), snapshot(dest / tree.name), modes=False)
