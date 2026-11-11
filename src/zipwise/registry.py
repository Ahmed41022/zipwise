"""FormatId -> backend mapping with lazy imports and a capability table.

Adding a format means one backend module plus one row here.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .backends.base import ArchiveReader, ArchiveWriter
from .detect import Detection, detect
from .errors import BackendUnavailable, UnsupportedFormat
from .jobs import JobContext
from .model import CreateOptions, FormatId


@dataclass(frozen=True)
class Capability:
    module: str
    reader: str | None
    writer: str | None
    can_encrypt: bool = False
    can_store_per_file: bool = False
    multi_entry: bool = True
    needs_tool: str | None = None
    package: str | None = None  # PyPI name shown in the install hint


_TAR = "zipwise.backends.tar"
_SINGLE = "zipwise.backends.single"

CAPABILITIES: dict[FormatId, Capability] = {
    FormatId.ZIP: Capability(
        "zipwise.backends.zip",
        "ZipReader",
        "ZipWriter",
        can_encrypt=True,
        can_store_per_file=True,
        package="pyzipper",
    ),
    FormatId.SEVENZ: Capability(
        "zipwise.backends.sevenz",
        "SevenZipReader",
        "SevenZipWriter",
        can_encrypt=True,
        package="py7zr",
    ),
    FormatId.TAR: Capability(_TAR, "TarReader", "TarWriter"),
    FormatId.TAR_GZ: Capability(_TAR, "TarReader", "TarWriter"),
    FormatId.TAR_BZ2: Capability(_TAR, "TarReader", "TarWriter"),
    FormatId.TAR_XZ: Capability(_TAR, "TarReader", "TarWriter"),
    FormatId.TAR_ZST: Capability(_TAR, "TarReader", "TarWriter", package="zstandard"),
    FormatId.GZ: Capability(_SINGLE, "SingleFileReader", "SingleFileWriter", multi_entry=False),
    FormatId.BZ2: Capability(_SINGLE, "SingleFileReader", "SingleFileWriter", multi_entry=False),
    FormatId.XZ: Capability(_SINGLE, "SingleFileReader", "SingleFileWriter", multi_entry=False),
    FormatId.ZST: Capability(
        _SINGLE, "SingleFileReader", "SingleFileWriter", multi_entry=False, package="zstandard"
    ),
    FormatId.RAR: Capability(
        "zipwise.backends.rar", "RarReader", None, needs_tool="unrar", package="rarfile"
    ),
}


def capability(fmt: FormatId) -> Capability:
    return CAPABILITIES[fmt]


def _load(fmt: FormatId, attr: str | None) -> type:
    cap = CAPABILITIES[fmt]
    if attr is None:
        raise UnsupportedFormat(f"{fmt.value.upper()} archives can be read but not created")
    try:
        module = importlib.import_module(cap.module)
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("zipwise."):
            raise BackendUnavailable(
                f"Support for {fmt.value} is not available in this version of Zipwise"
            ) from exc
        package = cap.package or (exc.name or "a required package")
        raise BackendUnavailable(
            f"Support for {fmt.value} needs the optional package {package!r}",
            hint=f"Install it with: pip install {package}",
        ) from exc
    cls: type = getattr(module, attr)
    return cls


def get_reader_class(fmt: FormatId) -> type[ArchiveReader]:
    cap = CAPABILITIES[fmt]
    cls = _load(fmt, cap.reader)
    assert issubclass(cls, ArchiveReader)
    return cls


def get_writer_class(fmt: FormatId) -> type[ArchiveWriter]:
    cap = CAPABILITIES[fmt]
    cls = _load(fmt, cap.writer)
    assert issubclass(cls, ArchiveWriter)
    return cls


def reader_accepts_source(fmt: FormatId) -> bool:
    """True when the backend's reader takes the optional ``source`` file object."""
    cls = get_reader_class(fmt)
    return "source" in inspect.signature(cls.__init__).parameters


def get_reader(
    path: Path,
    *,
    password: str | None = None,
    name_encoding: str | None = None,
    detection: Detection | None = None,
    source: BinaryIO | None = None,
) -> ArchiveReader:
    """Detect the format of ``path`` and open it with the matching backend.

    With ``source`` (a seekable binary file object holding the archive) the backend reads from
    it instead of opening ``path``; the backend must declare a ``source`` keyword parameter
    (check with :func:`reader_accepts_source`).
    """
    det = detection or detect(path)
    cls = get_reader_class(det.format)
    if source is None:
        return cls(Path(path), password=password, name_encoding=name_encoding)
    return cls(Path(path), password=password, name_encoding=name_encoding, source=source)


def get_writer(
    dest: BinaryIO | Path, options: CreateOptions, ctx: JobContext | None = None
) -> ArchiveWriter:
    cls = get_writer_class(options.format)
    return cls(dest, options, ctx)
