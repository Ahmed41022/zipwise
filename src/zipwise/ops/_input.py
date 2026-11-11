"""Shared helpers for operations that open an existing archive."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, cast

from ..backends.base import ArchiveReader
from ..detect import HEAD_SIZE, Detection, detect, sniff, sniff_stream
from ..errors import CorruptArchive, PasswordRequired, UnsupportedFormat
from ..jobs import JobContext
from ..model import ArchiveEntry, EntryType
from ..registry import get_reader, reader_accepts_source
from ..split import MultiPartFile, PartSet, find_parts, is_part_path, materialise_parts


def _find_part_set(path: Path) -> PartSet | None:
    """The part set ``path`` belongs to, or None when it is an ordinary file."""
    if not is_part_path(path):
        return None
    try:
        return find_parts(path)
    except CorruptArchive as exc:
        # ``data.2024`` style names: a real archive that only looks like a numbered part
        if exc.code == "part-first-missing" and _looks_like_archive(path):
            return None
        raise


def _looks_like_archive(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return sniff(fh.read(HEAD_SIZE)) is not None
    except OSError:
        return False


def _open_over_parts(
    first: Path,
    joined: MultiPartFile,
    detection: Detection,
    password: str | None,
    name_encoding: str | None,
    ctx: JobContext | None,
) -> ArchiveReader:
    if reader_accepts_source(detection.format):
        reader = get_reader(
            first,
            password=password,
            name_encoding=name_encoding,
            detection=detection,
            source=cast(BinaryIO, joined),
        )
        reader.add_cleanup(joined.close)
        return reader
    joined.close()
    copy, cleanup = materialise_parts(first, ctx)
    try:
        reader = get_reader(
            copy, password=password, name_encoding=name_encoding, detection=detection
        )
    except BaseException:
        cleanup()
        raise
    reader.add_cleanup(cleanup)
    return reader


def open_input_reader(
    archive: Path | str,
    *,
    password: str | None = None,
    name_encoding: str | None = None,
    ctx: JobContext | None = None,
) -> ArchiveReader:
    """Open ``archive`` for reading.

    Any part of a numbered set (``x.zip.001``, ``x.7z.003``) opens the whole set through a
    :class:`~zipwise.split.MultiPartFile`. Backends receive it as the ``source`` keyword of
    their reader constructor; a backend without that parameter gets a temporary joined copy
    instead (removed when the reader is released).
    """
    path = Path(archive)
    parts = _find_part_set(path)
    if parts is None:
        return get_reader(
            path, password=password, name_encoding=name_encoding, detection=detect(path)
        )
    first = parts.first
    joined = MultiPartFile(parts.parts)
    try:
        fmt = sniff_stream(cast(BinaryIO, joined))  # the first part may be tiny: read the set
        if fmt is None:
            raise UnsupportedFormat(
                f"Unrecognised archive format: {first.name}",
                hint="The file does not start with a known archive signature.",
            )
        joined.seek(0)
        detection = Detection(fmt, multipart=True)
        reader = _open_over_parts(first, joined, detection, password, name_encoding, ctx)
    except BaseException:
        joined.close()
        raise
    reader.origin = first
    reader.multipart = True
    return reader


def progress_total(entries: list[ArchiveEntry]) -> int:
    """Byte total for progress events: the sum of file sizes, or 0 ("unknown", shown as an
    indeterminate bar) when any file's size is unknown (negative, as in a .bz2 or .xz)."""
    sizes = [e.size for e in entries if e.type is EntryType.FILE]
    if any(size < 0 for size in sizes):
        return 0
    return sum(sizes)


def ensure_password(reader: ArchiveReader, entries: list[ArchiveEntry], ctx: JobContext) -> None:
    """Obtain a password when any entry that needs data is encrypted, and verify it."""
    needed = any(e.encrypted and e.type in (EntryType.FILE, EntryType.SYMLINK) for e in entries)
    if not needed:
        return
    if not reader.password:
        supplied = ctx.ask_password() if ctx.ask_password is not None else None
        if not supplied:
            raise PasswordRequired(
                "This archive is password protected",
                hint="Provide the password with --password or in the password dialog.",
            )
        reader.password = supplied
    reader.check_password()
