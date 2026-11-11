"""Single-file compressed streams: ``.gz``, ``.bz2``, ``.xz`` and ``.zst``.

These hold exactly one file. Its uncompressed size is not known up front, so the entry
reports ``size = -1`` and the extraction byte budget bounds the real output instead.
"""

from __future__ import annotations

import contextlib
import os
import re
import struct
import zlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

from ..errors import CorruptArchive, UsageError
from ..fsutil import CHUNK_SIZE, copy_stream, drain_stream, translate_oserror
from ..jobs import JobContext
from ..model import ArchiveEntry, ArchiveInfo, CreateOptions, EntryType, FormatId, Issue
from ..smart import resolve_level
from . import _streams as streams
from .base import ArchiveReader, ArchiveWriter, EntrySink

_FORMAT_OF_CODEC = {
    streams.GZ: FormatId.GZ,
    streams.BZ2: FormatId.BZ2,
    streams.XZ: FormatId.XZ,
    streams.ZST: FormatId.ZST,
}
_SUFFIXES = {
    streams.GZ: (".gz",),
    streams.BZ2: (".bz2", ".bz"),
    streams.XZ: (".xz",),
    streams.ZST: (".zst", ".zstd"),
}
_PART_RE = re.compile(r"\.\d{3}$")
_FLAG_HCRC, _FLAG_EXTRA, _FLAG_NAME, _FLAG_COMMENT = 0x02, 0x04, 0x08, 0x10
_MAX_NAME = 4096


def output_name(archive_name: str, codec: str) -> str:
    """``report.txt.gz`` -> ``report.txt``; unknown suffixes get ``.out`` appended."""
    name = _PART_RE.sub("", archive_name)
    for suffix in _SUFFIXES[codec]:
        if name.lower().endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return f"{name}.out" if name else "data.out"


def _clean_member_name(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    base = text.replace("\\", "/").rsplit("/", 1)[-1]
    return None if base in ("", ".", "..") else base


def read_gzip_header(fh: BinaryIO) -> tuple[str | None, int]:
    """Return ``(original name, mtime)`` from a gzip header; ``(None, 0)`` when unreadable."""
    head = fh.read(10)
    if len(head) < 10 or head[:3] != b"\x1f\x8b\x08":
        return None, 0
    flags = head[3]
    mtime = struct.unpack("<I", head[4:8])[0]
    if flags & _FLAG_EXTRA:
        length = fh.read(2)
        if len(length) < 2:
            return None, mtime
        fh.read(struct.unpack("<H", length)[0])
    name: str | None = None
    if flags & _FLAG_NAME:
        raw = bytearray()
        while len(raw) < _MAX_NAME:
            byte = fh.read(1)
            if not byte or byte == b"\0":
                break
            raw += byte
        name = _clean_member_name(bytes(raw))
    return name, mtime


class _Stream:
    """Decompressed data of the single member, with decompression errors translated."""

    def __init__(self, fh: BinaryIO, inner: Any, name: str) -> None:
        self._fh = fh
        self._inner = inner
        self._name = name

    def read(self, n: int = -1) -> bytes:
        try:
            data: bytes = self._inner.read(n)
        except streams.stream_errors() as exc:
            raise streams.translate_stream_error(exc, self._name) from exc
        return data

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._inner.close()
        self._fh.close()

    def __enter__(self) -> _Stream:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class SingleFileReader(ArchiveReader):
    format = FormatId.GZ

    def __init__(
        self,
        path: Path,
        *,
        password: str | None = None,
        name_encoding: str | None = None,
        source: BinaryIO | None = None,
    ) -> None:
        super().__init__(path, password=password, name_encoding=name_encoding, source=source)
        with streams.open_input(self.path, self.source) as fh:
            head = fh.read(8)
            self._codec = streams.sniff_codec(head)
            if self._codec not in _FORMAT_OF_CODEC:
                raise CorruptArchive(f"{self.path.name} is not a gz, bz2, xz or zst file")
            self.format = _FORMAT_OF_CODEC[self._codec]
            member_name: str | None = None
            self._mtime = 0
            if self._codec == streams.GZ:
                fh.seek(0)
                member_name, self._mtime = read_gzip_header(fh)
        self._name = member_name or output_name(self.origin.name, self._codec)
        self._packed = streams.input_size(self.path, self.source)

    def entries(self) -> list[ArchiveEntry]:
        mtime = datetime.fromtimestamp(self._mtime, tz=UTC) if self._mtime else None
        return [
            ArchiveEntry(
                name=self._name,
                type=EntryType.FILE,
                size=-1,
                compressed_size=self._packed,
                mtime=mtime,
                index=0,
            )
        ]

    def _open_entry(self, entry: ArchiveEntry) -> _Stream:
        fh = streams.open_input(self.path, self.source)
        try:
            inner = streams.open_decompressed(fh, self._codec)
        except BaseException:
            fh.close()
            raise
        return _Stream(fh, inner, self.path.name)

    def extract(self, entries: Sequence[ArchiveEntry], sink: EntrySink, ctx: JobContext) -> None:
        self.pull_extract(entries, sink, ctx, self._open_entry)  # type: ignore[arg-type]

    def test(self, ctx: JobContext) -> list[Issue]:
        entry = self.entries()[0]
        ctx.set_entry(entry.name)
        try:
            with self._open_entry(entry) as data:
                drain_stream(data, ctx)  # type: ignore[arg-type]
        except CorruptArchive as exc:
            return [Issue("error", exc.code, entry.name, exc.message)]
        ctx.entry_done()
        return []

    def info(self) -> ArchiveInfo:
        total: int | None = None
        if self._codec == streams.GZ and self._packed >= 18:
            with streams.open_input(self.path, self.source) as fh:
                fh.seek(-4, os.SEEK_END)
                total = struct.unpack("<I", fh.read(4))[0]  # modulo 2**32, as gzip stores it
        return ArchiveInfo(
            path=self.path,
            format=self.format,
            entries_count=1,
            total_size=total or 0,
            compressed_size=self._packed,
            encrypted=False,
            total_size_known=total is not None,
        )

    def close(self) -> None:
        self.run_cleanups()


class SingleFileWriter(ArchiveWriter):
    format = FormatId.GZ

    def __init__(
        self, dest: BinaryIO | Path, options: CreateOptions, ctx: JobContext | None = None
    ) -> None:
        super().__init__(dest, options, ctx)
        if options.format not in _FORMAT_OF_CODEC.values():
            raise UsageError(f"{options.format.value} is not a single-file format")
        self.format = options.format
        self._codec = streams.CODEC_OF_FORMAT[options.format]
        self._level = resolve_level(options.level)
        self._owned: BinaryIO | None = None
        self._added = False
        self._stream: BinaryIO
        if isinstance(dest, Path):
            try:
                self._stream = self._owned = dest.open("wb")
            except OSError as exc:
                raise translate_oserror(exc, f"cannot create {dest.name}") from exc
        else:
            self._stream = dest

    def _single_only(self) -> UsageError:
        return UsageError(
            f"A .{self.format.value} file holds exactly one file",
            hint="Pick a single file, or use a TAR, ZIP or 7z archive for several.",
        )

    def add_file(self, src: Path, arcname: str, *, store: bool = False) -> None:
        if self._added:
            raise self._single_only()
        self._added = True
        self.ctx.raise_if_cancelled()
        self.ctx.set_entry(arcname)
        try:
            st = os.stat(src)
            fin = open(src, "rb")  # noqa: SIM115 - closed in the with-block below
        except OSError as exc:
            raise translate_oserror(exc, f"cannot read {src}") from exc
        with fin:
            try:
                if self._codec == streams.GZ:
                    self._write_gzip(fin, arcname, st.st_mtime)
                else:
                    comp = streams.open_compressor(
                        self._stream, self._codec, self._level, size=st.st_size
                    )
                    copy_stream(fin, comp, self.ctx)
                    comp.close()
            except OSError as exc:
                raise translate_oserror(exc, f"cannot write {arcname}") from exc
        self.ctx.entry_done()

    def _write_gzip(self, fin: BinaryIO, arcname: str, mtime: float) -> None:
        """Written by hand so the original name is stored as UTF-8, not just latin-1."""
        name = arcname.replace("\\", "/").rsplit("/", 1)[-1].encode("utf-8")
        xfl = 2 if self._level == 9 else 4 if self._level <= 1 else 0
        stamp = int(mtime) if 0 <= int(mtime) <= 0xFFFFFFFF else 0
        header = b"\x1f\x8b\x08" + bytes([_FLAG_NAME if name else 0])
        header += struct.pack("<I", stamp) + bytes([xfl, 255]) + (name + b"\0" if name else b"")
        self._stream.write(header)
        comp = zlib.compressobj(self._level, zlib.DEFLATED, -15)
        crc = size = 0
        while True:
            self.ctx.raise_if_cancelled()
            chunk = fin.read(CHUNK_SIZE)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
            size += len(chunk)
            self._stream.write(comp.compress(chunk))
            self.ctx.advance(len(chunk))
        self._stream.write(comp.flush())
        self._stream.write(struct.pack("<II", crc & 0xFFFFFFFF, size & 0xFFFFFFFF))

    def add_dir(self, arcname: str, mtime: float, mode: int | None) -> None:
        raise self._single_only()

    def add_symlink(self, arcname: str, target: str, mtime: float) -> None:
        raise self._single_only()

    def close(self) -> None:
        try:
            if not self._added:
                raise UsageError("Nothing to compress: no input file was added")
        finally:
            if self._owned is not None:
                self._owned.close()

    def abort(self) -> None:
        if self._owned is not None:
            self._owned.close()
