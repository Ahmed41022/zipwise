"""Shared helpers for operations that open an existing archive."""

from __future__ import annotations

from pathlib import Path

from ..backends.base import ArchiveReader
from ..detect import detect
from ..errors import PasswordRequired, UsageError
from ..jobs import JobContext
from ..model import ArchiveEntry, EntryType
from ..registry import get_reader


def open_input_reader(
    archive: Path | str, *, password: str | None = None, name_encoding: str | None = None
) -> ArchiveReader:
    """Open ``archive`` for reading.

    Extension point for multi-part input: a later milestone wraps ``name.ext.001`` in a
    virtual file here so every operation can read split archives directly.
    """
    path = Path(archive)
    detection = detect(path)
    if detection.multipart:
        raise UsageError(
            "Multi-part archives are not available yet",
            hint="Join the parts into one file first.",
        )
    return get_reader(path, password=password, name_encoding=name_encoding, detection=detection)


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
