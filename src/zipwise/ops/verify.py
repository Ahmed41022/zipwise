"""Test (verify) an archive without extracting it."""

from __future__ import annotations

import time
from pathlib import Path

from ..jobs import JobContext
from ..model import EntryType, OperationReport
from ._input import ensure_password, open_input_reader


def test_archive(
    archive: Path | str,
    *,
    password: str | None = None,
    ctx: JobContext | None = None,
) -> OperationReport:
    """Read every entry and check its checksum.

    A file that cannot be opened at all raises ``CorruptArchive``; damaged entries inside a
    readable archive are reported as error issues (``report.ok`` is False).
    """
    ctx = ctx or JobContext()
    started = time.monotonic()
    with open_input_reader(archive, password=password, ctx=ctx) as reader:
        entries = reader.entries()
        ensure_password(reader, entries, ctx)
        entries = reader.entries()
        ctx.begin(
            "test",
            bytes_total=sum(e.size for e in entries if e.type is EntryType.FILE),
            entries_total=len(entries),
        )
        issues = [*reader.issues(), *reader.test(ctx)]
        ctx.finish()
    bad = len({i.entry for i in issues if i.severity == "error"})
    return OperationReport.build(
        issues,
        entries_done=len(entries) - bad,
        entries_skipped=bad,
        bytes_done=ctx.bytes_done,
        output_paths=[Path(archive)],
        elapsed=time.monotonic() - started,
    )
