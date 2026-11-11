"""Summary information about an archive."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ..jobs import JobContext
from ..model import ArchiveInfo
from ._input import open_input_reader


def archive_info(
    archive: Path | str,
    *,
    password: str | None = None,
    name_encoding: str | None = None,
    ctx: JobContext | None = None,
) -> ArchiveInfo:
    if ctx is not None:
        ctx.raise_if_cancelled()
    with open_input_reader(
        archive, password=password, name_encoding=name_encoding, ctx=ctx
    ) as reader:
        info = reader.info()
        return dataclasses.replace(info, multipart=reader.multipart)
