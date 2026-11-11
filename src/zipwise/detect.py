"""Content-based format detection (magic bytes), never trusting the file extension alone."""

from __future__ import annotations

import bz2
import gzip
import lzma
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

from .errors import IOFailure, UnsupportedFormat, UsageError
from .model import FormatId

_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_SEVENZ_MAGIC = b"7z\xbc\xaf\x27\x1c"
_RAR_MAGICS = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")
_GZ_MAGIC = b"\x1f\x8b"
_BZ2_MAGIC = b"BZh"
_XZ_MAGIC = b"\xfd7zXZ\x00"
_ZST_MAGIC = b"\x28\xb5\x2f\xfd"

_PART_RE = re.compile(r"\.\d{3}$")
HEAD_SIZE = 4096
_TAR_BLOCK = 512

_SINGLE_TO_TAR = {
    FormatId.GZ: FormatId.TAR_GZ,
    FormatId.BZ2: FormatId.TAR_BZ2,
    FormatId.XZ: FormatId.TAR_XZ,
    FormatId.ZST: FormatId.TAR_ZST,
}


@dataclass(frozen=True)
class Detection:
    format: FormatId
    multipart: bool = False


def looks_like_tar_header(block: bytes) -> bool:
    """True for a POSIX/GNU ``ustar`` header or a v7 header with a valid checksum."""
    if len(block) < _TAR_BLOCK:
        return False
    if block[257:262] == b"ustar":
        return True
    raw = block[148:156].strip(b"\0 ")
    if not raw or not block[0]:
        return False
    try:
        stored = int(raw, 8)
    except ValueError:
        return False
    computed = sum(block[:148]) + 8 * 32 + sum(block[156:_TAR_BLOCK])
    return stored == computed


def _first_block(fmt: FormatId, data: bytes) -> bytes:
    """Decompress just enough of ``data`` to obtain the first 512 bytes."""
    try:
        if fmt is FormatId.GZ:
            return zlib.decompressobj(31).decompress(data, _TAR_BLOCK)
        if fmt is FormatId.BZ2:
            return bz2.BZ2Decompressor().decompress(data, _TAR_BLOCK)
        if fmt is FormatId.XZ:
            return lzma.LZMADecompressor().decompress(data, _TAR_BLOCK)
        if fmt is FormatId.ZST:
            import zstandard

            return zstandard.ZstdDecompressor().decompressobj().decompress(data)[:_TAR_BLOCK]
    except Exception:
        return b""
    return b""


def _first_block_from_file(path: Path, fmt: FormatId) -> bytes:
    """Like ``_first_block`` but reads from the file: bz2 and zstd emit nothing until a whole
    block arrived, which can be far larger than ``HEAD_SIZE``."""
    try:
        with path.open("rb") as fh:
            if fmt is FormatId.GZ:
                return gzip.GzipFile(fileobj=fh).read(_TAR_BLOCK)
            if fmt is FormatId.BZ2:
                return bz2.BZ2File(fh).read(_TAR_BLOCK)
            if fmt is FormatId.XZ:
                return lzma.LZMAFile(fh).read(_TAR_BLOCK)
            if fmt is FormatId.ZST:
                import zstandard

                return zstandard.ZstdDecompressor().stream_reader(fh).read(_TAR_BLOCK)
    except Exception:
        return b""
    return b""


def sniff(head: bytes) -> FormatId | None:
    """Classify a file from its first bytes. Compressed streams holding a TAR report ``TAR_*``."""
    if head.startswith(_ZIP_MAGICS):
        return FormatId.ZIP
    if head.startswith(_SEVENZ_MAGIC):
        return FormatId.SEVENZ
    if head.startswith(_RAR_MAGICS):
        return FormatId.RAR
    single: FormatId | None = None
    if head.startswith(_GZ_MAGIC):
        single = FormatId.GZ
    elif head.startswith(_BZ2_MAGIC):
        single = FormatId.BZ2
    elif head.startswith(_XZ_MAGIC):
        single = FormatId.XZ
    elif head.startswith(_ZST_MAGIC):
        single = FormatId.ZST
    if single is not None:
        if looks_like_tar_header(_first_block(single, head)):
            return _SINGLE_TO_TAR[single]
        return single
    if looks_like_tar_header(head[:_TAR_BLOCK]):
        return FormatId.TAR
    return None


def is_part_name(path: Path) -> bool:
    return bool(_PART_RE.search(path.name))


def detect(path: Path) -> Detection:
    """Detect the format of ``path``; ``name.ext.001`` is the first volume of a split set."""
    path = Path(path)
    if not path.exists():
        raise UsageError(f"File not found: {path}")
    if path.is_dir():
        raise UsageError(f"Not a file: {path}")
    try:
        with path.open("rb") as fh:
            head = fh.read(HEAD_SIZE)
    except OSError as exc:
        raise IOFailure(f"Cannot read {path}: {exc.strerror or exc}") from exc
    fmt = sniff(head)
    if fmt in _SINGLE_TO_TAR and looks_like_tar_header(_first_block_from_file(path, fmt)):
        fmt = _SINGLE_TO_TAR[fmt]
    if fmt is None:
        raise UnsupportedFormat(
            f"Unrecognised archive format: {path.name}",
            hint="The file does not start with a known archive signature.",
        )
    return Detection(fmt, multipart=is_part_name(path))


def detect_format(path: Path) -> FormatId:
    return detect(path).format
