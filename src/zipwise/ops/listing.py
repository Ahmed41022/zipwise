"""List the entries of an archive."""

from __future__ import annotations

from pathlib import Path

from ..jobs import JobContext
from ..model import ArchiveEntry
from ._input import open_input_reader


def list_archive(
    archive: Path | str,
    *,
    password: str | None = None,
    name_encoding: str | None = None,
    ctx: JobContext | None = None,
) -> list[ArchiveEntry]:
    """Return every entry. Listing never needs a password: file names are not encrypted."""
    if ctx is not None:
        ctx.raise_if_cancelled()
    with open_input_reader(archive, password=password, name_encoding=name_encoding) as reader:
        return reader.entries()
