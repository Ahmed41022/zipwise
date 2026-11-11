"""Extraction pipeline: plan -> safety -> naming -> conflicts -> atomic write -> metadata.

Backends push entries into :class:`_ExtractSink`, which owns every decision about the
destination file system.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import stat
import time
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO, cast

from ..backends.base import ArchiveReader
from ..conflicts import ConflictResolver, unique_path
from ..errors import SafetyViolation, UsageError
from ..fsutil import (
    apply_mode,
    clear_readonly,
    ensure_writable_dir,
    free_space,
    is_case_insensitive,
    long_path,
    make_symlink,
    remove_quietly,
    set_mtime,
    translate_oserror,
)
from ..jobs import JobContext
from ..model import (
    ArchiveEntry,
    ConflictAction,
    EntryType,
    ExtractOptions,
    Issue,
    OperationReport,
)
from ..naming import CaseClashResolver, sanitize_component
from ..safety import ByteBudget, ExtractionPlan, PlannedEntry, plan_extraction
from ._input import ensure_password, open_input_reader

PART_SUFFIX = ".zwpart"
_REPARSE_POINT = 0x400


def _reject_resume(options: ExtractOptions) -> None:
    """Extension point: resumable extraction (``options.resume``) arrives in a later milestone."""
    if options.resume:
        raise UsageError(
            "Resuming an extraction is not available yet",
            hint="Run the extraction again without --resume.",
        )


def _norm(name: str) -> str:
    return "/".join(p for p in name.replace("\\", "/").split("/") if p not in ("", "."))


def _select(entries: Sequence[ArchiveEntry], options: ExtractOptions) -> list[ArchiveEntry]:
    """Apply ``only`` and ``strip_components``. Indices are kept so readers can find the entry."""
    chosen = list(entries)
    if options.only is not None:
        wanted = [_norm(o) for o in options.only]
        chosen = [
            e
            for e in chosen
            if any(_norm(e.name) == w or _norm(e.name).startswith(w + "/") for w in wanted)
        ]
        if not chosen:
            raise UsageError("None of the requested names are in the archive")
    if options.strip_components > 0:
        stripped: list[ArchiveEntry] = []
        for e in chosen:
            parts = _norm(e.name).split("/")[options.strip_components :]
            if not parts or parts == [""]:
                continue
            link = e.link_target
            if e.type is EntryType.HARDLINK and link is not None:
                link = "/".join(_norm(link).split("/")[options.strip_components :])
            stripped.append(dataclasses.replace(e, name="/".join(parts), link_target=link))
        chosen = stripped
    return chosen


def _is_link_or_reparse(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


class _OutFile:
    """Write-only stream onto ``name.zwpart`` that charges every byte to the budget."""

    def __init__(self, final: Path, budget: ByteBudget) -> None:
        self.final = final
        self.temp = final.with_name(final.name + PART_SUFFIX)
        self.written = 0
        self._budget = budget
        try:
            self._fh = long_path(self.temp).open("wb")
        except OSError as exc:
            raise translate_oserror(exc, f"cannot create {final.name}") from exc

    def write(self, data: bytes) -> int:
        self._budget.add(len(data))  # raises before anything is written
        try:
            n = self._fh.write(data)
        except OSError as exc:
            raise translate_oserror(exc, f"cannot write {self.final.name}") from exc
        self.written += len(data)
        return n

    def close(self) -> None:
        self._fh.close()

    @property
    def closed(self) -> bool:
        return self._fh.closed


class _ExtractSink:
    """Implements ``EntrySink``: all file-system writes of an extraction go through here."""

    def __init__(
        self,
        dest: Path,
        plan: ExtractionPlan,
        options: ExtractOptions,
        ctx: JobContext,
        budget: ByteBudget,
        *,
        windows: bool,
        case_insensitive: bool,
    ) -> None:
        self.dest = dest
        self.options = options
        self.ctx = ctx
        self.budget = budget
        self.issues: list[Issue] = list(plan.issues)
        self.skipped = plan.skipped
        self.done = 0
        self.bytes_done = 0
        self._items = plan.by_index()
        self._resolver = ConflictResolver(options.on_conflict, ctx)
        self._targets: dict[int, Path] = {}
        self._by_parts: dict[tuple[str, ...], Path] = {}
        self._dirs: list[tuple[Path, ArchiveEntry]] = []
        self._symlinks: list[tuple[Path, PlannedEntry]] = []
        self._hardlinks: list[tuple[Path, PlannedEntry]] = []
        self._prepare_names(plan, windows, case_insensitive)

    def _issue(self, severity: str, code: str, name: str, message: str) -> None:
        self.issues.append(Issue(severity, code, name, message))  # type: ignore[arg-type]

    def _prepare_names(self, plan: ExtractionPlan, windows: bool, case_insensitive: bool) -> None:
        clash = CaseClashResolver(case_insensitive)
        for planned in plan.planned:
            entry = planned.entry
            parts = planned.parts
            if windows:
                cleaned = tuple(sanitize_component(p, True) for p in parts)
                if cleaned != parts:
                    self._issue(
                        "info",
                        "renamed-invalid-name",
                        entry.name,
                        f"renamed to {'/'.join(cleaned)}",
                    )
                parts = cleaned
            resolved = clash.resolve(parts, is_dir=entry.type is EntryType.DIR)
            if resolved != parts:
                self._issue(
                    "info", "renamed-case-clash", entry.name, f"renamed to {'/'.join(resolved)}"
                )
            target = self.dest.joinpath(*resolved)
            self._assert_inside(target)
            self._targets[entry.index] = target
            self._by_parts[planned.parts] = target

    def _assert_inside(self, target: Path) -> None:
        base = os.path.normcase(os.path.abspath(self.dest))
        path = os.path.normcase(os.path.abspath(target))
        if os.path.commonpath([base, path]) != base or path == base:
            raise SafetyViolation(f"Refusing to write outside the destination: {target}")

    # -- helpers ---------------------------------------------------------------------------

    def _skip(self) -> None:
        self.skipped += 1
        self.ctx.entry_done()

    def _prepare_parent(self, target: Path, name: str) -> bool:
        """Create parent folders; refuse to write through a symlink or over a file."""
        current = self.dest
        for part in target.parent.relative_to(self.dest).parts:
            current = current / part
            lp = str(long_path(current))
            if _is_link_or_reparse(lp):
                self._issue("error", "symlink-traversal", name, "blocked: parent is a symlink")
                return False
            if os.path.lexists(lp) and not os.path.isdir(lp):
                self._issue("error", "parent-is-file", name, "blocked: a file is in the way")
                return False
        try:
            long_path(target.parent).mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            self._issue("error", "parent-is-file", name, "blocked: a file is in the way")
            return False
        except OSError as exc:
            raise translate_oserror(exc, f"cannot create {target.parent}") from exc
        return True

    def _resolve_conflict(self, target: Path, name: str) -> Path | None:
        """Return the path to write, or None to skip."""
        lp = long_path(target)
        if not os.path.lexists(lp):
            return target
        if os.path.isdir(lp) and not _is_link_or_reparse(str(lp)):
            self._issue("error", "dir-in-the-way", name, "blocked: a folder has this name")
            return None
        action = self._resolver.resolve(target)
        if action is ConflictAction.SKIP:
            self._issue("warning", "skipped-existing", name, "skipped: file already exists")
            return None
        if action is ConflictAction.RENAME:
            renamed = unique_path(target)
            self._issue("info", "renamed-existing", name, f"saved as {renamed.name}")
            return renamed
        try:
            if _is_link_or_reparse(str(lp)):
                os.unlink(lp)
            else:
                clear_readonly(target)
        except OSError as exc:
            raise translate_oserror(exc, f"cannot overwrite {target.name}") from exc
        return target

    # -- EntrySink -------------------------------------------------------------------------

    def open(self, entry: ArchiveEntry) -> BinaryIO | None:
        planned = self._items.get(entry.index)
        if planned is None:
            return None
        self.ctx.raise_if_cancelled()
        target = self._targets[entry.index]
        kind = planned.entry.type
        name = planned.entry.name
        if kind is EntryType.DIR:
            self._make_dir(target, planned.entry)
            return None
        if kind is EntryType.SYMLINK:
            self._symlinks.append((target, planned))
            return None
        if kind is EntryType.HARDLINK:
            self._hardlinks.append((target, planned))
            return None
        if not self._prepare_parent(target, name):
            self._skip()
            return None
        final = self._resolve_conflict(target, name)
        if final is None:
            self._skip()
            return None
        self.budget.begin(planned.entry)
        return cast(BinaryIO, _OutFile(final, self.budget))

    def close(self, entry: ArchiveEntry, stream: BinaryIO, ok: bool) -> None:
        out = cast(_OutFile, stream)
        failure: OSError | None = None
        try:
            out.close()
        except OSError as exc:
            failure = exc
        if not ok or failure is not None:
            remove_quietly(out.temp)
            if failure is not None:
                raise translate_oserror(failure, f"cannot write {out.final.name}") from failure
            return
        meta = self._items[entry.index].entry
        set_mtime(out.temp, meta.mtime)
        if self.options.restore_mode:
            apply_mode(long_path(out.temp), meta.mode)
        try:
            os.replace(long_path(out.temp), long_path(out.final))
        except OSError as exc:
            remove_quietly(out.temp)
            raise translate_oserror(exc, f"cannot write {out.final.name}") from exc
        self.done += 1
        self.bytes_done += out.written
        self.ctx.entry_done()

    # -- non-file entries ------------------------------------------------------------------

    def _make_dir(self, target: Path, entry: ArchiveEntry) -> None:
        if not self._prepare_parent(target, entry.name):
            self._skip()
            return
        lp = long_path(target)
        if _is_link_or_reparse(str(lp)):
            self._issue("error", "symlink-traversal", entry.name, "blocked: target is a symlink")
            self._skip()
            return
        try:
            lp.mkdir(exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            self._issue("error", "file-in-the-way", entry.name, "blocked: a file has this name")
            self._skip()
            return
        except OSError as exc:
            raise translate_oserror(exc, f"cannot create {target}") from exc
        self._dirs.append((target, entry))
        self.done += 1
        self.ctx.entry_done()

    def finish(self) -> None:
        """Create links after all files exist, then apply directory metadata deepest first."""
        for target, planned in self._symlinks:
            self._make_symlink(target, planned)
        for target, planned in self._hardlinks:
            self._make_hardlink(target, planned)
        for target, entry in sorted(self._dirs, key=lambda t: len(t[0].parts), reverse=True):
            if self.options.restore_mode:
                apply_mode(long_path(target), entry.mode)
            set_mtime(target, entry.mtime)

    def _make_symlink(self, target: Path, planned: PlannedEntry) -> None:
        entry = planned.entry
        if not self._prepare_parent(target, entry.name):
            self._skip()
            return
        final = self._resolve_conflict(target, entry.name)
        if final is None:
            self._skip()
            return
        if os.path.lexists(long_path(final)):
            remove_quietly(final)
        link_target = entry.link_target or ""
        is_dir = (final.parent / link_target.replace("\\", "/")).is_dir()
        if make_symlink(final, link_target, target_is_dir=is_dir):
            self.done += 1
            self.ctx.entry_done()
        else:
            self._issue(
                "warning",
                "symlink-unsupported",
                entry.name,
                "skipped: this system did not allow creating the symlink",
            )
            self._skip()

    def _make_hardlink(self, target: Path, planned: PlannedEntry) -> None:
        entry = planned.entry
        source = self._by_parts.get(planned.link_parts or ())
        if source is None or not os.path.isfile(long_path(source)):
            self._issue("error", "hardlink-missing", entry.name, "skipped: link target not found")
            self._skip()
            return
        if not self._prepare_parent(target, entry.name):
            self._skip()
            return
        final = self._resolve_conflict(target, entry.name)
        if final is None:
            self._skip()
            return
        if os.path.lexists(long_path(final)):
            remove_quietly(final)
        try:
            os.link(long_path(source), long_path(final))
        except OSError:
            try:
                shutil.copy2(long_path(source), long_path(final))
            except OSError as exc:
                raise translate_oserror(exc, f"cannot create {final.name}") from exc
        self.done += 1
        self.ctx.entry_done()


def extract_reader(
    reader: ArchiveReader,
    dest: Path | str,
    options: ExtractOptions | None = None,
    ctx: JobContext | None = None,
    *,
    case_insensitive: bool | None = None,
) -> OperationReport:
    """Run the pipeline for an already opened reader (``extract_archive`` calls this)."""
    options = options or ExtractOptions()
    ctx = ctx or JobContext()
    started = time.monotonic()
    destination = Path(os.path.abspath(dest))

    entries = reader.entries()
    selected = _select(entries, options)
    ensure_password(reader, selected, ctx)
    original = {e.index: e for e in reader.entries()}
    selected = _select(list(original.values()), options)

    plan = plan_extraction(selected, destination, options.policy)  # raises before any write
    ensure_writable_dir(destination)
    insensitive = is_case_insensitive(destination) if case_insensitive is None else case_insensitive
    total = plan.total_bytes
    max_total = options.policy.max_total_bytes
    if max_total is None:
        unknown = any(p.entry.size < 0 for p in plan.planned)
        max_total = free_space(destination) if unknown else total
    budget = ByteBudget(max_total, options.policy.max_entry_bytes)
    sink = _ExtractSink(
        destination,
        plan,
        options,
        ctx,
        budget,
        windows=options.policy.sanitize_windows(),
        case_insensitive=insensitive,
    )
    ctx.begin("extract", bytes_total=total, entries_total=len(plan.planned))
    reader.extract([original[p.entry.index] for p in plan.planned], sink, ctx)
    sink.finish()
    ctx.finish()
    return OperationReport.build(
        sink.issues,
        entries_done=sink.done,
        entries_skipped=sink.skipped,
        bytes_done=sink.bytes_done,
        output_paths=[destination],
        elapsed=time.monotonic() - started,
    )


def extract_archive(
    archive: Path | str,
    dest: Path | str,
    options: ExtractOptions | None = None,
    ctx: JobContext | None = None,
) -> OperationReport:
    """Extract ``archive`` into ``dest`` safely. See the module docstring for the pipeline."""
    options = options or ExtractOptions()
    _reject_resume(options)
    with open_input_reader(
        archive, password=options.password, name_encoding=options.name_encoding
    ) as reader:
        return extract_reader(reader, dest, options, ctx)
