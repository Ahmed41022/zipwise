"""Create an archive: scan, compress into ``name.partial``, verify, rename."""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from pathlib import Path

from ..errors import CorruptArchive, UsageError
from ..fsutil import AtomicFile, translate_oserror, walk_sources
from ..jobs import JobContext
from ..model import CreateOptions, EntryType, Issue, OperationReport
from ..registry import capability, get_reader, get_writer, get_writer_class
from ..smart import should_store


def _reject_split(options: CreateOptions) -> None:
    """Extension point: split-after-verify (``options.split_size``) arrives in a later milestone."""
    if options.split_size is not None:
        raise UsageError(
            "Splitting into volumes is not available yet",
            hint="Create the archive without a split size.",
        )


def _verify_created(
    path: Path, expected_entries: int, options: CreateOptions, ctx: JobContext
) -> None:
    """Re-open the finished file and test every entry; failure aborts the whole job."""
    reader = get_reader(path, password=options.password)
    with reader:
        entries = reader.entries()
        if len(entries) != expected_entries:
            raise CorruptArchive(
                f"Verification failed: wrote {expected_entries} entries but found {len(entries)}"
            )
        ctx.begin(
            "verify",
            bytes_total=sum(e.size for e in entries if e.type is EntryType.FILE),
            entries_total=len(entries),
        )
        problems = [i for i in reader.test(ctx) if i.severity == "error"]
    if problems:
        first = problems[0]
        raise CorruptArchive(f"Verification failed: {first.entry}: {first.message}")


def create_archive(
    sources: Sequence[Path | str],
    dest: Path | str,
    options: CreateOptions | None = None,
    ctx: JobContext | None = None,
) -> OperationReport:
    """Archive ``sources`` (files and directories) into ``dest``.

    The archive is written to ``dest.partial`` and renamed only after it passed verification,
    so an existing ``dest`` is never replaced by a broken file.
    """
    options = options or CreateOptions()
    ctx = ctx or JobContext()
    started = time.monotonic()
    _reject_split(options)

    inputs = [Path(s) for s in sources]
    if not inputs:
        raise UsageError("Nothing to archive: no input files given")
    get_writer_class(options.format)  # fail early with BackendUnavailable / UnsupportedFormat
    if options.password and not capability(options.format).can_encrypt:
        raise UsageError(f"{options.format.value} archives cannot be password protected")
    target = Path(os.path.abspath(dest))
    if target.is_dir():
        raise UsageError(f"Destination is a directory: {target}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise translate_oserror(exc, f"cannot create {target.parent}") from exc

    atomic = AtomicFile(target, ".partial")
    ctx.begin("scan")
    items = []
    for item in walk_sources(
        inputs,
        follow_symlinks=options.follow_symlinks,
        root_dir=options.root_dir,
        exclude=[target, atomic.temp],
    ):
        ctx.raise_if_cancelled()
        items.append(item)
    if not items:
        raise UsageError("Nothing to archive: the inputs contain no files")

    issues: list[Issue] = []
    skipped = 0
    total_bytes = sum(i.size for i in items if i.type is EntryType.FILE)
    ctx.begin("compress", bytes_total=total_bytes, entries_total=len(items))
    written_bytes = 0
    try:
        stream = atomic.open()
        writer = get_writer(stream, options, ctx)
        try:
            for item in items:
                ctx.raise_if_cancelled()
                if item.type is EntryType.FILE:
                    store = should_store(item.arcname, item.size, smart=options.smart_store)
                    writer.add_file(item.path, item.arcname, store=store)
                elif item.type is EntryType.DIR:
                    writer.add_dir(item.arcname, item.mtime, item.mode)
                elif item.type is EntryType.SYMLINK:
                    writer.add_symlink(item.arcname, item.link_target or "", item.mtime)
                else:
                    skipped += 1
                    issues.append(
                        Issue(
                            "warning", "special-file", item.arcname, "skipped: not a regular file"
                        )
                    )
                    ctx.entry_done()
            writer.close()
        except BaseException:
            writer.abort()
            raise
        written_bytes = ctx.bytes_done
        stream.close()
        if options.verify:
            _verify_created(atomic.temp, len(items) - skipped, options, ctx)
        ctx.raise_if_cancelled()
        atomic.commit()
    except BaseException:
        atomic.discard()
        raise
    ctx.finish()
    return OperationReport.build(
        issues,
        entries_done=len(items) - skipped,
        entries_skipped=skipped,
        bytes_done=written_bytes,
        output_paths=[target],
        elapsed=time.monotonic() - started,
    )
