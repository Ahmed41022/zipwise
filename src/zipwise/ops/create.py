"""Create an archive: scan, compress into ``name.partial``, verify, rename."""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from pathlib import Path

from ..errors import CorruptArchive, UsageError
from ..fsutil import AtomicFile, remove_quietly, translate_oserror, walk_sources
from ..jobs import JobContext
from ..model import CreateOptions, EntryType, Issue, OperationReport
from ..registry import capability, get_reader, get_writer, get_writer_class
from ..smart import should_store
from ..split import manifest_name, recipient_notes, split_file
from ._input import progress_total

# Issue code reported when a split was asked for but the archive fits in one part.
SPLIT_NOT_NEEDED = "split-not-needed"


def _fits_in_one_part(path: Path, part_size: int) -> bool:
    try:
        return os.path.getsize(path) <= part_size
    except OSError as exc:
        raise translate_oserror(exc, f"cannot read {path.name}") from exc


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
        ctx.begin("verify", bytes_total=progress_total(entries), entries_total=len(entries))
        problems = [i for i in reader.test(ctx) if i.severity == "error"]
    if problems:
        first = problems[0]
        raise CorruptArchive(f"Verification failed: {first.entry}: {first.message}")


def _split_verified(whole: Path, target: Path, part_size: int, ctx: JobContext) -> list[Path]:
    """Cut the finished archive into ``target.001...``; returns parts, manifest and notes."""
    parts = split_file(whole, part_size, ctx, name=target.name, out_dir=target.parent)
    try:
        notes = recipient_notes(parts, target.name)
    except BaseException:
        for part in parts:
            remove_quietly(part)
        remove_quietly(target.parent / manifest_name(target.name))
        raise
    return [*parts, target.parent / manifest_name(target.name), notes]


def create_archive(
    sources: Sequence[Path | str],
    dest: Path | str,
    options: CreateOptions | None = None,
    ctx: JobContext | None = None,
) -> OperationReport:
    """Archive ``sources`` (files and directories) into ``dest``.

    The archive is written to ``dest.partial`` and renamed only after it passed verification,
    so an existing ``dest`` is never replaced by a broken file. With ``split_size`` the verified
    archive is cut into numbered parts, unless it already fits in one part: then it is kept as
    ``dest`` and the report carries an info issue with code ``split-not-needed``.
    """
    options = options or CreateOptions()
    ctx = ctx or JobContext()
    started = time.monotonic()
    if options.split_size is not None and options.split_size < 1:
        raise UsageError(f"Part size must be at least 1 byte, got {options.split_size}")

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
        if options.split_size is None:
            atomic.commit()
            outputs = [target]
        elif _fits_in_one_part(atomic.temp, options.split_size):
            atomic.commit()
            outputs = [target]
            issues.append(
                Issue(
                    "info",
                    SPLIT_NOT_NEEDED,
                    None,
                    f"The archive fits in one part of {options.split_size} bytes, "
                    "so it was kept whole",
                )
            )
        else:
            # The verified file is cut straight from ``.partial`` and then removed, so a
            # cancelled split leaves neither parts nor a half-finished archive.
            outputs = _split_verified(atomic.temp, target, options.split_size, ctx)
            atomic.discard()
    except BaseException:
        atomic.discard()
        raise
    ctx.finish()
    return OperationReport.build(
        issues,
        entries_done=len(items) - skipped,
        entries_skipped=skipped,
        bytes_done=written_bytes,
        output_paths=outputs,
        elapsed=time.monotonic() - started,
    )
