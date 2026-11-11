"""Create -> test -> extract -> compare."""

from __future__ import annotations

import os
from pathlib import Path

from zipwise.jobs import JobContext
from zipwise.model import CreateOptions, ExtractOptions, OperationReport
from zipwise.ops import create_archive, extract_archive
from zipwise.ops import test_archive as verify_archive
from zipwise.safety import SafetyPolicy

from .trees import snapshot

POSIX = os.name != "nt"


def compare_snapshots(
    before: dict[str, tuple], after: dict[str, tuple], *, tolerance: float = 2.0, modes: bool = True
) -> None:
    assert sorted(before) == sorted(after), "different set of paths"
    for rel, (kind, payload, mtime, mode) in before.items():
        a_kind, a_payload, a_mtime, a_mode = after[rel]
        assert kind == a_kind, rel
        assert payload == a_payload, f"content differs: {rel}"
        if mtime is not None:
            assert abs(mtime - a_mtime) <= tolerance, f"mtime differs: {rel}"
        if modes and POSIX and kind != "link":
            assert mode == a_mode, f"mode differs: {rel}"


def assert_roundtrip(
    work: Path,
    tree: Path,
    options: CreateOptions | None = None,
    *,
    password: str | None = None,
    tolerance: float = 2.0,
) -> OperationReport:
    """Archive ``tree`` (keeping its folder name), test it, extract it elsewhere, compare."""
    options = options or CreateOptions()
    archive = work / "out.archive"
    create_archive([tree], archive, options, JobContext())
    report = verify_archive(archive, password=password or options.password)
    assert report.ok, report.issues
    dest = work / "extracted"
    extract_opts = ExtractOptions(
        password=password or options.password,
        policy=SafetyPolicy(sanitize_windows_names="never"),
    )
    result = extract_archive(archive, dest, extract_opts)
    assert result.ok, result.issues
    compare_snapshots(snapshot(tree), snapshot(dest / tree.name), tolerance=tolerance)
    return result
