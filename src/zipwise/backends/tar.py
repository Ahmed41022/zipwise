"""TAR backend (plain, gz, bz2, xz, zst): streaming read and write on top of ``tarfile``.

Reading uses tarfile's stream mode, so a compressed archive is never unpacked as a whole and
members are copied out through the sink by this module, never by ``tarfile.extract``.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import tarfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

from ..errors import CorruptArchive, UsageError, ZipwiseError
from ..fsutil import drain_stream, normalised_mode, translate_oserror
from ..jobs import JobContext
from ..model import ArchiveEntry, ArchiveInfo, CreateOptions, EntryType, FormatId, Issue
from ..smart import resolve_level
from . import _streams as streams
from .base import ArchiveReader, ArchiveWriter, EntrySink

_FORMAT_OF_CODEC = {
    streams.NONE: FormatId.TAR,
    streams.GZ: FormatId.TAR_GZ,
    streams.BZ2: FormatId.TAR_BZ2,
    streams.XZ: FormatId.TAR_XZ,
    streams.ZST: FormatId.TAR_ZST,
}
_TAR_FORMATS = frozenset(_FORMAT_OF_CODEC.values())
_READ_ERRORS: tuple[type[BaseException], ...] = (tarfile.TarError, *streams.stream_errors())


def _translate(exc: BaseException, name: str) -> ZipwiseError:
    if isinstance(exc, tarfile.TarError):
        return CorruptArchive(f"{name}: {exc}", hint="The archive may be truncated or damaged.")
    return streams.translate_stream_error(exc, name)


def _timestamp(value: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


class _Source:
    """An open tar stream: file, decompressor and ``TarFile`` in stream mode."""

    def __init__(
        self, path: Path, codec: str, encoding: str, source: BinaryIO | None = None
    ) -> None:
        self.fh: BinaryIO = streams.open_input(path, source)
        self.stream: Any = None
        self.tar: tarfile.TarFile | None = None
        self.pos = -1
        self._name = path.name
        try:
            self.stream = streams.open_decompressed(self.fh, codec)
            self.tar = tarfile.open(  # noqa: SIM115 - closed in close()
                fileobj=self.stream, mode="r|", encoding=encoding, errors="replace"
            )
        except _READ_ERRORS as exc:
            self.close()
            raise _translate(exc, path.name) from exc
        except BaseException:
            self.close()
            raise

    def next(self) -> tarfile.TarInfo | None:
        assert self.tar is not None
        try:
            info = self.tar.next()
        except _READ_ERRORS as exc:
            raise _translate(exc, self._name) from exc
        self.tar.members = []  # type: ignore[attr-defined]  # tarfile would otherwise keep every header in memory
        if info is not None:
            self.pos += 1
        return info

    def drain(self, ctx: JobContext) -> None:
        """Read what is left so that trailing checksums and truncation are noticed."""
        try:
            drain_stream(self.stream, ctx)
        except _READ_ERRORS as exc:
            raise _translate(exc, self._name) from exc

    def close(self) -> None:
        for obj in (self.tar, self.stream if self.stream is not self.fh else None, self.fh):
            if obj is not None:
                with contextlib.suppress(Exception):  # closing must not mask the real error
                    obj.close()
        self.tar = None


class _Guard:
    """Member data stream that turns decompression failures into Zipwise errors."""

    def __init__(self, inner: Any, name: str) -> None:
        self._inner = inner
        self._name = name

    def read(self, n: int = -1) -> bytes:
        try:
            data: bytes = self._inner.read(n)
        except _READ_ERRORS as exc:
            raise _translate(exc, self._name) from exc
        return data

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> _Guard:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class TarReader(ArchiveReader):
    format = FormatId.TAR

    def __init__(
        self,
        path: Path,
        *,
        password: str | None = None,
        name_encoding: str | None = None,
        source: BinaryIO | None = None,
    ) -> None:
        super().__init__(path, password=password, name_encoding=name_encoding, source=source)
        if name_encoding is not None:
            try:
                codecs.lookup(name_encoding)
            except LookupError:
                raise UsageError(f"Unknown name encoding: {name_encoding}") from None
        with streams.open_input(self.path, self.source) as fh:
            self._codec = streams.sniff_codec(fh.read(8))
        self.format = _FORMAT_OF_CODEC[self._codec]
        self._entries: list[ArchiveEntry] | None = None
        self._damage: CorruptArchive | None = None
        self._current: _Source | None = None

    def _open(self) -> _Source:
        return _Source(self.path, self._codec, self.name_encoding or "utf-8", self.source)

    @staticmethod
    def _entry(info: tarfile.TarInfo, index: int) -> ArchiveEntry:
        link: str | None = None
        size = 0
        if info.isdir():
            kind = EntryType.DIR
        elif info.issym():
            kind, link = EntryType.SYMLINK, info.linkname
        elif info.islnk():
            kind, link = EntryType.HARDLINK, info.linkname
        elif info.isreg():
            kind, size = EntryType.FILE, info.size
        else:
            kind = EntryType.OTHER
        return ArchiveEntry(
            name=info.name,
            type=kind,
            size=size,
            mtime=_timestamp(info.mtime),
            mode=info.mode & 0o7777,
            link_target=link,
            index=index,
        )

    def _scan(self) -> tuple[list[ArchiveEntry], CorruptArchive | None]:
        """Every member header, plus the damage that stopped the scan early (if any)."""
        result: list[ArchiveEntry] = []
        src = self._open()
        try:
            while (info := src.next()) is not None:
                result.append(self._entry(info, src.pos))
            src.drain(JobContext())
        except CorruptArchive as exc:
            return result, exc
        finally:
            src.close()
        return result, None

    def entries(self) -> list[ArchiveEntry]:
        if self._entries is None:
            result, damage = self._scan()
            if damage is not None:
                raise damage
            self._entries = result
        return self._entries

    def extractable_entries(self) -> list[ArchiveEntry]:
        if self._entries is None:
            result, self._damage = self._scan()
            if self._damage is not None:
                return result
            self._entries = result
        return self.entries()

    # -- extraction ------------------------------------------------------------------------

    def _open_entry(self, entry: ArchiveEntry) -> _Guard:
        src = self._current
        if src is None or entry.index <= src.pos:
            if src is not None:
                src.close()
            src = self._current = self._open()
        info = None
        while src.pos < entry.index:
            info = src.next()
            if info is None:
                raise CorruptArchive(f"{entry.name!r} is missing from the archive")
        assert info is not None and src.tar is not None
        try:
            handle = src.tar.extractfile(info)
        except _READ_ERRORS as exc:
            raise _translate(exc, entry.name) from exc
        if handle is None:
            raise CorruptArchive(f"{entry.name!r} has no data in the archive")
        return _Guard(handle, entry.name)

    def extract(self, entries: Sequence[ArchiveEntry], sink: EntrySink, ctx: JobContext) -> None:
        try:
            self.pull_extract(entries, sink, ctx, self._open_entry)  # type: ignore[arg-type]
            if self._damage is not None:  # the damage lies after the last selected entry
                raise self._damage
            src = self._current
            if src is not None and len(entries) == len(self.entries()):
                src.drain(ctx)
        finally:
            if self._current is not None:
                self._current.close()
                self._current = None

    def test(self, ctx: JobContext) -> list[Issue]:
        issues: list[Issue] = []
        src = self._open()
        name = self.path.name
        try:
            while (info := src.next()) is not None:
                ctx.raise_if_cancelled()
                name = info.name
                ctx.set_entry(name)
                if info.isreg() and src.tar is not None:
                    handle = src.tar.extractfile(info)
                    if handle is not None:
                        with _Guard(handle, name) as data:
                            drain_stream(data, ctx)  # type: ignore[arg-type]
                ctx.entry_done()
            src.drain(ctx)
        except CorruptArchive as exc:
            issues.append(Issue("error", exc.code, name, exc.message))
        finally:
            src.close()
        return issues

    def info(self) -> ArchiveInfo:
        entries = self.entries()
        packed = streams.input_size(self.path, self.source)
        return ArchiveInfo(
            path=self.path,
            format=self.format,
            entries_count=len(entries),
            total_size=sum(e.size for e in entries if e.type is EntryType.FILE),
            compressed_size=packed,
            encrypted=False,
            name_encoding=self.name_encoding or "utf-8",
            encoding_confidence=None,
        )

    def close(self) -> None:
        if self._current is not None:
            self._current.close()
            self._current = None
        self.run_cleanups()


class _CountingReader:
    """Feeds a source file to tarfile while honouring cancellation and progress."""

    def __init__(self, fh: BinaryIO, ctx: JobContext) -> None:
        self._fh = fh
        self._ctx = ctx

    def read(self, n: int = -1) -> bytes:
        self._ctx.raise_if_cancelled()
        data = self._fh.read(n)
        self._ctx.advance(len(data))
        return data


class TarWriter(ArchiveWriter):
    """Streams a PAX-format tar (long and Unicode names work), optionally compressed."""

    format = FormatId.TAR

    def __init__(
        self, dest: BinaryIO | Path, options: CreateOptions, ctx: JobContext | None = None
    ) -> None:
        super().__init__(dest, options, ctx)
        if options.format not in _TAR_FORMATS:
            raise UsageError(f"{options.format.value} is not a TAR format")
        self.format = options.format
        codec = streams.CODEC_OF_FORMAT[options.format]
        level = resolve_level(options.level)
        self._owned: BinaryIO | None = None
        stream: BinaryIO
        if isinstance(dest, Path):
            try:
                stream = self._owned = dest.open("wb")
            except OSError as exc:
                raise translate_oserror(exc, f"cannot create {dest.name}") from exc
        else:
            stream = dest
        self._comp: Any = None
        self._stream = stream
        try:
            self._comp = streams.open_compressor(stream, codec, level)
            self._tar = tarfile.TarFile(
                fileobj=self._comp, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8"
            )
        except BaseException:
            self.abort()
            raise

    def _info(self, arcname: str, mtime: float, mode: int) -> tarfile.TarInfo:
        info = tarfile.TarInfo(arcname)
        info.mtime = int(mtime)
        info.mode = mode & 0o7777
        return info

    def add_file(self, src: Path, arcname: str, *, store: bool = False) -> None:
        self.ctx.raise_if_cancelled()
        self.ctx.set_entry(arcname)
        try:
            st = os.stat(src)
            fin = open(src, "rb")  # noqa: SIM115 - closed in the with-block below
        except OSError as exc:
            raise translate_oserror(exc, f"cannot read {src}") from exc
        info = self._info(arcname, st.st_mtime, normalised_mode(st, False))
        info.size = st.st_size
        with fin:
            try:
                self._tar.addfile(info, _CountingReader(fin, self.ctx))  # type: ignore[arg-type]
            except OSError as exc:
                raise translate_oserror(exc, f"cannot write {arcname}") from exc
        self.ctx.entry_done()

    def add_dir(self, arcname: str, mtime: float, mode: int | None) -> None:
        self.ctx.raise_if_cancelled()
        self.ctx.set_entry(arcname)
        info = self._info(arcname.rstrip("/"), mtime, 0o755 if mode is None else mode)
        info.type = tarfile.DIRTYPE
        self._add_header(info, arcname)
        self.ctx.entry_done()

    def add_symlink(self, arcname: str, target: str, mtime: float) -> None:
        self.ctx.raise_if_cancelled()
        self.ctx.set_entry(arcname)
        info = self._info(arcname, mtime, 0o777)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        self._add_header(info, arcname)
        self.ctx.entry_done()

    def _add_header(self, info: tarfile.TarInfo, arcname: str) -> None:
        try:
            self._tar.addfile(info)
        except OSError as exc:
            raise translate_oserror(exc, f"cannot write {arcname}") from exc

    def close(self) -> None:
        try:
            try:
                self._tar.close()
                if self._comp is not self._stream:
                    self._comp.close()
            except OSError as exc:
                raise translate_oserror(exc, "cannot finish the archive") from exc
        finally:
            if self._owned is not None:
                self._owned.close()

    def abort(self) -> None:
        try:
            if self._comp is not None and self._comp is not self._stream:
                with contextlib.suppress(Exception):  # the output is discarded anyway
                    self._comp.close()
        finally:
            if self._owned is not None:
                self._owned.close()
