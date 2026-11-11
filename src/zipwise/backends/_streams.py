"""Compression streams shared by the TAR and single-file backends."""

from __future__ import annotations

import bz2
import gzip
import lzma
import struct
import zlib
from pathlib import Path
from typing import Any, BinaryIO

from ..errors import BackendUnavailable, CorruptArchive, UsageError, ZipwiseError
from ..fsutil import translate_oserror
from ..model import FormatId

GZ, BZ2, XZ, ZST, NONE = "gz", "bz2", "xz", "zst", "none"

_MAGIC = (
    (b"\x1f\x8b", GZ),
    (b"BZh", BZ2),
    (b"\xfd7zXZ\x00", XZ),
    (b"\x28\xb5\x2f\xfd", ZST),
)

CODEC_OF_FORMAT: dict[FormatId, str] = {
    FormatId.TAR: NONE,
    FormatId.TAR_GZ: GZ,
    FormatId.GZ: GZ,
    FormatId.TAR_BZ2: BZ2,
    FormatId.BZ2: BZ2,
    FormatId.TAR_XZ: XZ,
    FormatId.XZ: XZ,
    FormatId.TAR_ZST: ZST,
    FormatId.ZST: ZST,
}

# zstd levels for Zipwise's 0-9 scale ("normal" is 6)
_ZSTD_LEVELS = (1, 1, 2, 3, 5, 7, 9, 12, 15, 19)
_ZSTD_FEED = 1024  # small input slices keep the output of one call bounded for hostile data


def zstandard_module() -> Any:
    try:
        import zstandard
    except ImportError as exc:
        raise BackendUnavailable(
            "zstd archives need the 'zstandard' package",
            hint="Install it with: pip install zstandard",
        ) from exc
    return zstandard


def sniff_codec(head: bytes) -> str:
    for magic, codec in _MAGIC:
        if head.startswith(magic):
            return codec
    return NONE


def stream_errors() -> tuple[type[BaseException], ...]:
    """Exceptions that mean "the compressed data is damaged"."""
    errors: list[type[BaseException]] = [
        EOFError,
        zlib.error,
        lzma.LZMAError,
        gzip.BadGzipFile,
        struct.error,
        ValueError,
        OSError,
    ]
    try:
        import zstandard

        errors.append(zstandard.ZstdError)
    except ImportError:
        pass
    return tuple(errors)


def translate_stream_error(exc: BaseException, name: str) -> ZipwiseError:
    """Map a decompression or tar-parsing failure onto a Zipwise error."""
    if isinstance(exc, ZipwiseError):
        return exc
    if isinstance(exc, EOFError):
        return CorruptArchive(f"{name}: the data ends unexpectedly (truncated archive)")
    if isinstance(exc, OSError) and exc.errno is not None:  # BadGzipFile etc. carry no errno
        return translate_oserror(exc, f"cannot read {name}")
    return CorruptArchive(f"{name}: damaged data ({exc})")


class ZstdReader:
    """Strict zstd decompressor: a stream that ends inside a frame raises ``EOFError``."""

    def __init__(self, fh: BinaryIO) -> None:
        self._zstd = zstandard_module()
        self._fh = fh
        self._dobj = self._zstd.ZstdDecompressor().decompressobj()
        self._in_frame = False
        self._pending = b""
        self._buf = bytearray()
        self._done = False

    def _fill(self) -> None:
        chunk = self._pending[:_ZSTD_FEED]
        self._pending = self._pending[_ZSTD_FEED:]
        if not chunk:
            chunk = self._fh.read(_ZSTD_FEED)
        if not chunk:
            if self._in_frame:
                raise EOFError("Compressed file ended before the end-of-stream marker was reached")
            self._done = True
            return
        self._in_frame = True
        self._buf += self._dobj.decompress(chunk)
        if self._dobj.eof:
            self._pending = self._dobj.unused_data + self._pending
            self._dobj = self._zstd.ZstdDecompressor().decompressobj()
            self._in_frame = False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            while not self._done:
                self._fill()
            data, self._buf = bytes(self._buf), bytearray()
            return data
        while len(self._buf) < size and not self._done:
            self._fill()
        data = bytes(self._buf[:size])
        del self._buf[:size]
        return data

    def close(self) -> None:
        self._done = True


def open_decompressed(fh: BinaryIO, codec: str) -> Any:
    """Wrap an open binary file in a reader for ``codec`` (``none`` returns it unchanged)."""
    if codec == GZ:
        return gzip.GzipFile(fileobj=fh, mode="rb")
    if codec == BZ2:
        return bz2.BZ2File(fh, "rb")
    if codec == XZ:
        return lzma.LZMAFile(fh, "rb")
    if codec == ZST:
        return ZstdReader(fh)
    return fh


def open_source(path: Path) -> BinaryIO:
    try:
        return path.open("rb")
    except FileNotFoundError as exc:
        raise UsageError(f"File not found: {path}") from exc
    except OSError as exc:
        raise translate_oserror(exc, f"cannot read {path.name}") from exc


def open_compressor(dest: BinaryIO, codec: str, level: int, size: int | None = None) -> Any:
    """Writer for ``codec`` on top of ``dest``; closing it does not close ``dest``."""
    if codec == NONE:
        return dest
    if codec == GZ:
        return gzip.GzipFile(filename="", fileobj=dest, mode="wb", compresslevel=max(level, 0))
    if codec == BZ2:
        return bz2.BZ2File(dest, "wb", compresslevel=min(max(level, 1), 9))
    if codec == XZ:
        return lzma.LZMAFile(dest, "wb", preset=min(max(level, 0), 9))
    zstd = zstandard_module()
    comp = zstd.ZstdCompressor(level=_ZSTD_LEVELS[min(max(level, 0), 9)], write_checksum=True)
    kwargs: dict[str, Any] = {"closefd": False}
    if size is not None:
        kwargs["size"] = size
    return comp.stream_writer(dest, **kwargs)
