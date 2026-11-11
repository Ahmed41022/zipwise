"""Maps parsed arguments to options, calls ``ops`` / ``split`` and returns ``Result`` objects."""

from __future__ import annotations

import argparse
import codecs
import dataclasses
import getpass
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..errors import ExitCode, UsageError, ZipwiseError
from ..fsutil import translate_oserror
from ..jobs import CancelToken, JobContext
from ..model import (
    ConflictAction,
    CreateOptions,
    ExtractOptions,
    OperationReport,
)
from ..ops import (
    archive_info,
    create_archive,
    extract_archive,
    list_archive,
    test_archive,
)
from ..settings import load_settings
from ..split import (
    find_parts,
    is_part_path,
    join_parts,
    manifest_name,
    recipient_notes,
    split_file,
)
from .parser import PASSWORD_PROMPT, format_from_name, level_arg, suffix_of
from .render import (
    ProgressBar,
    Result,
    arrow,
    format_listing,
    human_size,
    listing_footer,
    percent,
    plural,
)

_LEGACY_SKIP = ("ascii", "utf-8", "utf8")


@dataclass
class RunEnv:
    """Everything a command needs to know about the process it runs in."""

    cancel: CancelToken
    progress: ProgressBar | None = None
    stdin_tty: bool = False
    quiet: bool = False
    json: bool = False


# -- job context and prompts ----------------------------------------------------------------


def make_context(env: RunEnv) -> JobContext:
    """Single place where a ``JobContext`` is created (tests replace it to inject a cancel)."""
    return JobContext(
        env.cancel,
        on_progress=env.progress,
        resolve_conflict=_conflict_prompt(env),
        ask_password=_password_prompt(env),
    )


def _password_prompt(env: RunEnv) -> Callable[[], str | None]:
    def ask() -> str | None:
        if not env.stdin_tty:
            return None
        if env.progress is not None:
            env.progress.clear()
        try:
            return getpass.getpass("Password: ") or None
        except EOFError:
            return None

    return ask


def _conflict_prompt(env: RunEnv) -> Callable[[Path], tuple[ConflictAction, bool]]:
    choices = {"s": ConflictAction.SKIP, "r": ConflictAction.RENAME, "o": ConflictAction.OVERWRITE}

    def ask(path: Path) -> tuple[ConflictAction, bool]:
        if env.progress is not None:
            env.progress.clear()
        while True:
            sys.stderr.write(
                f"{path} already exists: [s]kip / [r]ename / [o]verwrite "
                "(add [A]ll, e.g. 'sa', to apply to every file): "
            )
            sys.stderr.flush()
            try:
                answer = input().strip()
            except EOFError:
                return ConflictAction.SKIP, True
            if not answer or answer[0].lower() not in choices:
                continue
            remember = answer[0].isupper() or answer[1:].lower().startswith("a")
            return choices[answer[0].lower()], remember

    return ask


def _read_password(args: argparse.Namespace, env: RunEnv, *, confirm: bool) -> str | None:
    value = getattr(args, "password", None)
    pfile = getattr(args, "password_file", None)
    if pfile and value is not None:
        raise UsageError("Use either --password or --password-file, not both")
    if pfile:
        try:
            text = Path(pfile).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageError(f"Cannot read the password file: {pfile}") from exc
        first = text.splitlines()[0] if text.splitlines() else ""
        if not first:
            raise UsageError(f"The password file is empty: {pfile}")
        return first
    if value == PASSWORD_PROMPT:
        if not env.stdin_tty:
            raise UsageError(
                "Cannot ask for a password: input is not a terminal",
                hint="Use --password-file FILE instead.",
            )
        if env.progress is not None:
            env.progress.clear()
        first = getpass.getpass("Password: ")
        if confirm and getpass.getpass("Repeat password: ") != first:
            raise UsageError("The two passwords do not match")
        if not first:
            raise UsageError("The password is empty")
        return first
    if value == "":
        raise UsageError("The password is empty")
    return value


# -- helpers ----------------------------------------------------------------------------------


def _existing(path: str) -> Path:
    p = Path(path)
    if not os.path.lexists(p):
        raise UsageError(f"No such file or folder: {path}")
    return p


def _check_encoding(name: str | None) -> str | None:
    if name is None:
        return None
    try:
        codecs.lookup(name)
    except LookupError:
        raise UsageError(f"Unknown text encoding: {name}") from None
    return name


def archive_stem(name: str) -> str:
    """``photos.tar.gz`` -> ``photos``; ``a.zip.001`` -> ``a``; unknown suffix: drop one."""
    text = re.sub(r"\.\d{3,}$", "", name)
    suffix = suffix_of(text)
    stem = text[: -len(suffix)] if suffix else Path(text).stem
    return stem or text


def _issue_data(report: OperationReport) -> dict[str, object]:
    return dict(report.to_dict())


def _report_result(command: str, report: OperationReport, **extra: object) -> Result:
    data = _issue_data(report)
    data.update(extra)
    return Result(
        command,
        exit_code=int(report.exit_code),
        ok=report.ok,
        issues=list(report.issues),
        data=data,
    )


def _size_on_disk(paths: list[Path]) -> int:
    total = 0
    for p in paths:
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return total


# -- create -----------------------------------------------------------------------------------


def run_create(args: argparse.Namespace, env: RunEnv) -> Result:
    settings = load_settings()
    target = Path(args.archive)
    fmt = args.format or format_from_name(target.name)
    if fmt is None:
        raise UsageError(
            f"Cannot tell the format from {target.name!r}",
            hint="Name the archive with a known suffix (.zip, .7z, .tar.gz, ...) or pass -f.",
        )
    for src in args.sources:
        _existing(src)
    first_part = target.with_name(target.name + ".001")
    clash = [p for p in (target, first_part) if os.path.lexists(p)]
    if clash and not args.force:
        raise UsageError(f"{clash[0]} already exists", hint="Use --force to overwrite it.")
    password = _read_password(args, env, confirm=True)
    if args.encrypt_names and password is None:
        raise UsageError("--encrypt-names needs a password")
    level = args.level
    if level is None:
        try:
            level = level_arg(settings.default_level)
        except argparse.ArgumentTypeError:
            level = "normal"
    options = CreateOptions(
        format=fmt,
        level=level,
        password=password,
        encrypt_names=args.encrypt_names,
        smart_store=settings.smart_store and not args.no_smart_store,
        verify=settings.verify_after_create and not args.no_verify,
        split_size=args.split,
        follow_symlinks=args.follow_symlinks,
    )
    report = create_archive(args.sources, target, options, make_context(env))
    outputs = list(report.output_paths)
    parts = [p for p in outputs if is_part_path(p)] if args.split else []
    archive_bytes = _size_on_disk(parts or outputs[:1])
    result = _report_result(
        "create",
        report,
        archive=str(target),
        format=fmt.value,
        verified=options.verify,
        encrypted=password is not None,
        input_bytes=report.bytes_done,
        archive_bytes=archive_bytes,
        parts=[str(p) for p in parts],
    )
    stats = [plural(report.entries_done, "entry")]
    if report.bytes_done > 0:
        stats.append(
            f"{human_size(report.bytes_done)} {arrow(sys.stdout)} {human_size(archive_bytes)}, "
            f"{percent(archive_bytes, report.bytes_done)}"
        )
    where = f"{target} in {plural(len(parts), 'part')}" if parts else str(target)
    tail = " and verified it." if options.verify else "."
    result.summary = f"Created {where} ({', '.join(stats)}){tail}"
    if parts:
        result.details = [str(p) for p in outputs]
    return result


# -- extract ----------------------------------------------------------------------------------


def _smart_destination(archive: Path, names: list[str], encoding: str | None, strip: int) -> Path:
    entries = list_archive(archive, name_encoding=encoding)
    chosen = [e.name.replace("\\", "/").strip("/") for e in entries]
    if names:
        wanted = [n.replace("\\", "/").strip("/") for n in names]
        picked = [c for c in chosen if any(c == w or c.startswith(w + "/") for w in wanted)]
        chosen = picked or chosen
    tops = {tuple(c.split("/")[strip:][:1]) for c in chosen if c}
    tops.discard(())
    if len(tops) > 1:
        return Path.cwd() / archive_stem(archive.name)
    return Path.cwd()


def run_extract(args: argparse.Namespace, env: RunEnv) -> Result:
    settings = load_settings()
    archive = _existing(args.archive)
    encoding = _check_encoding(args.encoding)
    password = _read_password(args, env, confirm=False)
    choice = args.on_conflict or settings.on_conflict
    try:
        action = ConflictAction(choice)
    except ValueError:
        action = ConflictAction.ASK
    if action is ConflictAction.ASK and not env.stdin_tty:
        action = ConflictAction.SKIP

    policy = settings.to_policy()
    if args.max_ratio is not None:
        policy = dataclasses.replace(policy, max_ratio=args.max_ratio)
    if args.max_size is not None:
        policy = dataclasses.replace(policy, max_total_bytes=args.max_size)
    if args.max_entries is not None:
        policy = dataclasses.replace(policy, max_entries=args.max_entries)
    if args.no_symlinks:
        policy = dataclasses.replace(policy, allow_symlinks=False)

    if args.dest:
        dest = Path(args.dest)
    elif args.here:
        dest = Path.cwd()
    else:
        dest = _smart_destination(archive, args.names, encoding, args.strip_components)
    options = ExtractOptions(
        password=password,
        name_encoding=encoding,
        policy=policy,
        on_conflict=action,
        only=list(args.names) or None,
        resume=args.resume,
        restore_mode=not args.no_restore_mode,
        strip_components=args.strip_components,
    )
    report = extract_archive(archive, dest, options, make_context(env))
    result = _report_result("extract", report, archive=str(archive), destination=str(dest))
    text = f"Extracted {plural(report.entries_done, 'entry')} to {dest}"
    if report.entries_skipped:
        text += f" ({report.entries_skipped} skipped)"
    result.summary = text + "."
    return result


# -- list / test / info -----------------------------------------------------------------------


def run_list(args: argparse.Namespace, env: RunEnv) -> Result:
    archive = _existing(args.archive)
    encoding = _check_encoding(args.encoding)
    ctx = make_context(env)
    entries = list_archive(archive, name_encoding=encoding, ctx=ctx)
    detected: str | None = encoding
    confidence: float | None = None
    try:
        info = archive_info(archive, name_encoding=encoding, ctx=ctx)
        detected, confidence = info.name_encoding, info.encoding_confidence
    except ZipwiseError:
        pass
    result = Result("list")
    result.lines = format_listing(entries, long=args.long)
    result.lines.append(listing_footer(entries))
    if encoding is None and detected and detected.lower() not in _LEGACY_SKIP:
        conf = f" (confidence {round(confidence * 100)}%)" if confidence is not None else ""
        result.notes.append(
            f"note: names were decoded as {detected}{conf}; use --encoding ENC if they look wrong"
        )
    files = [e for e in entries if e.type.value == "file"]
    result.data = {
        "archive": str(archive),
        "entries_count": len(entries),
        "total_size": sum(max(e.size, 0) for e in files),
        "name_encoding": detected,
        "encoding_confidence": confidence,
        "entries": [e.to_dict() for e in entries],
    }
    return result


def run_test(args: argparse.Namespace, env: RunEnv) -> Result:
    archive = _existing(args.archive)
    password = _read_password(args, env, confirm=False)
    report = test_archive(archive, password=password, ctx=make_context(env))
    total = report.entries_done + report.entries_skipped
    result = _report_result("test", report, archive=str(archive), entries_count=total)
    if not report.ok:
        bad = report.entries_skipped
        result.exit_code = int(ExitCode.CORRUPT)
        message = f"{plural(bad, 'damaged entry')} found" if bad else "damage found"
        result.error = {"code": "test-failed", "message": f"{message} in {archive}", "hint": None}
    else:
        result.summary = f"OK ({plural(total, 'entry')})"
    return result


def run_info(args: argparse.Namespace, env: RunEnv) -> Result:
    archive = _existing(args.archive)
    info = archive_info(
        archive, name_encoding=_check_encoding(args.encoding), ctx=make_context(env)
    )
    ratio = percent(info.compressed_size, info.total_size)
    names = info.name_encoding or "-"
    if info.name_encoding and info.encoding_confidence is not None:
        names += f" (confidence {round(info.encoding_confidence * 100)}%)"
    result = Result("info", data=dict(info.to_dict()))
    result.lines = [
        f"Archive:    {info.path}",
        f"Format:     {info.format.value}",
        f"Entries:    {info.entries_count}",
        f"Size:       {human_size(info.total_size)} ({info.total_size} bytes)",
        f"Packed:     {human_size(info.compressed_size)} ({info.compressed_size} bytes)",
        f"Ratio:      {ratio}",
        f"Encrypted:  {'yes' if info.encrypted else 'no'}",
        f"Names:      {names}",
        f"Multi-part: {'yes' if info.multipart else 'no'}",
    ]
    if info.comment:
        result.lines.append(f"Comment:    {info.comment}")
    result.issues = list(info.issues)
    return result


# -- split / join -----------------------------------------------------------------------------


def run_split(args: argparse.Namespace, env: RunEnv) -> Result:
    source = _existing(args.file)
    if not source.is_file():
        raise UsageError(f"Not a file: {source}")
    out_dir = Path(args.out_dir) if args.out_dir else source.parent
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise translate_oserror(exc, f"cannot create {out_dir}") from exc
    base = source.name
    parts = split_file(
        source,
        args.size,
        make_context(env),
        keep_source=not args.delete_source,
        out_dir=out_dir,
    )
    notes = recipient_notes(parts, base, directory=out_dir)
    manifest = Path(os.path.abspath(out_dir)) / manifest_name(base)
    sizes = [p.stat().st_size for p in parts]
    report = OperationReport.build(
        [], entries_done=len(parts), bytes_done=sum(sizes), output_paths=[*parts, manifest, notes]
    )
    result = _report_result(
        "split",
        report,
        source=str(source),
        part_size=args.size,
        parts=[{"path": str(p), "size": s} for p, s in zip(parts, sizes, strict=True)],
        manifest=str(manifest),
        notes=str(notes),
    )
    result.details = [f"{p}  ({human_size(s)})" for p, s in zip(parts, sizes, strict=True)]
    result.summary = f"Split {source.name} into {plural(len(parts), 'part')} in {out_dir}."
    return result


def run_join(args: argparse.Namespace, env: RunEnv) -> Result:
    first = _existing(args.part)
    count = len(find_parts(first).parts)
    verified = find_parts(first).manifest_path is not None
    output = join_parts(first, args.output, make_context(env))
    report = OperationReport.build(
        [], entries_done=count, bytes_done=output.stat().st_size, output_paths=[output]
    )
    result = _report_result(
        "join", report, output=str(output), parts_count=count, verified=verified
    )
    note = " and checked them against the manifest" if verified else ""
    result.summary = f"Joined {plural(count, 'part')} into {output}{note}."
    return result


HANDLERS: dict[str, Callable[[argparse.Namespace, RunEnv], Result]] = {
    "create": run_create,
    "extract": run_extract,
    "list": run_list,
    "test": run_test,
    "info": run_info,
    "split": run_split,
    "join": run_join,
}
