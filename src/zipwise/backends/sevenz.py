"""7z backend on top of ``py7zr`` (solid LZMA2, optional AES-256 and header encryption).

Everything that touches py7zr lives here. Extraction streams through py7zr's writer-factory
API straight into the sink: nothing is unpacked to a temporary folder or held in memory.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import stat
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import py7zr
from py7zr.exceptions import Bad7zFile, UnsupportedCompressionMethodError
from py7zr.helpers import ArchiveTimestamp
from py7zr.io import MemIO, Py7zIO, WriterFactory
from py7zr.member import (
    FILE_ATTRIBUTE_UNIX_EXTENSION,
    FILE_ATTRIBUTE_UNIX_SHIFT,
    MemberType,
)
from py7zr.properties import FILTER_CRYPTO_AES256_SHA256, FILTER_LZMA2

from ..errors import (
    PASSWORD_REQUIRED_HINT,
    CorruptArchive,
    IOFailure,
    PasswordRequired,
    UnsupportedFormat,
    UsageError,
    WrongPassword,
    ZipwiseError,
)
from ..fsutil import CHUNK_SIZE, normalised_mode, translate_oserror
from ..jobs import JobContext
from ..model import (
    ArchiveEntry,
    ArchiveInfo,
    CreateOptions,
    EntryType,
    FormatId,
    Issue,
)
from ..smart import resolve_level
from . import _streams as streams
from .base import ArchiveReader, ArchiveWriter, EntrySink

_AES_METHOD = b"\x06\xf1\x07\x01"
_MAX_LINK_BYTES = 64 * 1024
_FILE_ATTRIBUTE_READONLY = 0x01


class _Probed(Exception):
    """Raised by a probe stream to stop py7zr as soon as the first bytes decrypted."""


class _Discard(Py7zIO):
    """Stream for data nobody asked for."""

    def __init__(self, ctx: JobContext | None = None) -> None:
        self._ctx = ctx
        self._size = 0

    def write(self, s: bytes | bytearray) -> int:
        if self._ctx is not None:
            self._ctx.raise_if_cancelled()
            self._ctx.advance(len(s))
        self._size += len(s)
        return len(s)

    def read(self, size: int | None = None) -> bytes:
        return b""

    def seek(self, offset: int, whence: int = 0) -> int:
        return 0

    def seekable(self) -> bool:
        return False

    def flush(self) -> None:
        return None

    def size(self) -> int:
        return self._size


class _CollectIO(_Discard):
    """Keeps a small payload (a symlink target) in memory."""

    def __init__(self) -> None:
        super().__init__()
        self.data = bytearray()

    def write(self, s: bytes | bytearray) -> int:
        if len(self.data) < _MAX_LINK_BYTES:
            self.data += s[: _MAX_LINK_BYTES - len(self.data)]
        return super().write(s)


class _ProbeIO(_Discard):
    def write(self, s: bytes | bytearray) -> int:
        raise _Probed


class _IndexFactory(WriterFactory):
    """Hands py7zr one stream per file entry. Entries are registered under their index, so
    py7zr's own name handling (which rejects ``..`` and ``x:y``) is never involved."""

    def __init__(self, make: Callable[[int], Py7zIO]) -> None:
        self._make = make

    def create(self, filename: str) -> Py7zIO:
        return self._make(int(filename))


class _CountingIO(_Discard):
    """Discards data, counting the entry as done once it was decoded completely."""

    def __init__(self, ctx: JobContext) -> None:
        super().__init__(ctx)
        self._counted = ctx

    def close(self) -> None:
        self._counted.entry_done()


class _SinkIO(_Discard):
    """Forwards decompressed bytes into the stream the sink gave us for one entry."""

    def __init__(
        self, sink: EntrySink, ctx: JobContext, entry: ArchiveEntry, out: BinaryIO
    ) -> None:
        super().__init__(ctx)
        self._sink = sink
        self._entry = entry
        self._out = out
        self.finished = False

    def write(self, s: bytes | bytearray) -> int:
        n = super().write(s)
        self._out.write(s)
        return n

    def close(self) -> None:
        self.finish(True)

    def finish(self, ok: bool) -> None:
        if not self.finished:
            self.finished = True
            self._sink.close(self._entry, self._out, ok)


class _SinkSession:
    """State of one extraction: which entry is being decoded, and which streams are open."""

    def __init__(self, by_index: dict[int, ArchiveEntry], sink: EntrySink, ctx: JobContext) -> None:
        self.by_index = by_index
        self.sink = sink
        self.ctx = ctx
        self.streams: list[_SinkIO] = []
        self.current: str | None = None
        self.key_proven = False
        self._previous: ArchiveEntry | None = None

    def create(self, index: int) -> Py7zIO:
        entry = self.by_index[index]
        self.ctx.raise_if_cancelled()
        self.ctx.set_entry(entry.name)
        self.current = entry.name
        # py7zr asks for the next stream only after the previous one passed its CRC check
        if self._previous is not None and self._previous.encrypted:
            self.key_proven = True
        self._previous = entry
        out = self.sink.open(entry)
        if out is None:
            return _Discard(self.ctx)
        stream = _SinkIO(self.sink, self.ctx, entry, out)
        self.streams.append(stream)
        return stream

    def abandon(self) -> None:
        """Tell the sink about streams that never completed, so partial files are removed."""
        for stream in self.streams:
            stream.finish(False)


def _timestamp(value: ArchiveTimestamp | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value.totimestamp(), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


class SevenZipReader(ArchiveReader):
    format = FormatId.SEVENZ

    def __init__(
        self,
        path: Path,
        *,
        password: str | None = None,
        name_encoding: str | None = None,
        source: BinaryIO | None = None,
    ) -> None:
        super().__init__(path, password=password, name_encoding=name_encoding, source=source)
        self._fh: BinaryIO | None = None
        self._sz: Any = None
        self._entries: list[ArchiveEntry] | None = None
        self._open()

    @property
    def password(self) -> str | None:
        return self._password

    @password.setter
    def password(self, value: str | None) -> None:
        if value != self._password:
            self._password = value
            self._open()  # py7zr takes the password when the archive is opened

    # -- opening ---------------------------------------------------------------------------

    def _open(self) -> None:
        self._release()
        self._entries = None
        fh = streams.open_input(self.path, self.source)  # a shared source restarts at 0
        try:
            # A file object (not a path) also stops py7zr from extracting folders in threads.
            self._sz = py7zr.SevenZipFile(fh, "r", password=self._password or None)
        except py7zr.exceptions.PasswordRequired as exc:
            fh.close()
            raise PasswordRequired(
                "This archive is password protected", hint=PASSWORD_REQUIRED_HINT
            ) from exc
        except Bad7zFile as exc:
            fh.close()
            raise CorruptArchive(
                f"Not a valid 7z file: {self.path.name} ({exc})",
                hint="The archive may be truncated or damaged.",
            ) from exc
        except OSError as exc:
            fh.close()
            raise translate_oserror(exc, f"cannot read {self.path.name}") from exc
        except Exception as exc:
            fh.close()
            if self._password:  # an encrypted header decrypts to garbage with a wrong password
                raise WrongPassword(
                    "Wrong password, or the archive is damaged",
                    hint="Check the password. If it is right, the archive is damaged.",
                ) from exc
            raise CorruptArchive(f"Cannot read {self.path.name}: {exc}") from exc
        self._fh = fh

    # -- listing ---------------------------------------------------------------------------

    @staticmethod
    def _is_encrypted(f: Any) -> bool:
        folder = f.folder
        if folder is None:
            return False
        return any(c.get("method") == _AES_METHOD for c in folder.coders)

    def entries(self) -> list[ArchiveEntry]:
        if self._entries is not None:
            return self._entries
        result: list[ArchiveEntry] = []
        for f in self._sz.files:
            if f.is_directory:
                kind = EntryType.DIR
            elif f.is_symlink:
                kind = EntryType.SYMLINK
            elif f.is_socket or f.is_junction:
                kind = EntryType.OTHER
            else:
                kind = EntryType.FILE
            result.append(
                ArchiveEntry(
                    name=f.filename.rstrip("/") if kind is EntryType.DIR else f.filename,
                    type=kind,
                    size=(f.uncompressed or 0) if kind is EntryType.FILE else 0,
                    compressed_size=f.compressed,
                    mtime=_timestamp(f.lastwritetime),
                    mode=f.posix_mode,
                    encrypted=self._is_encrypted(f),
                    crc=f.crc32,
                    index=f.id,
                )
            )
        self._entries = result
        self._fill_link_targets(result)
        return result

    def _run(self, indexes: Collection[int], make: Callable[[int], Py7zIO]) -> None:
        """Decode the file entries ``indexes`` through streams from ``make``, in archive order.

        py7zr's public extract() derives output names from the entry names and refuses some of
        them, so the worker is driven directly with the entry index as the "name"."""
        sz = self._sz
        sz.reset()
        factory = _IndexFactory(make)
        for f in sz.files:
            if f.id in indexes:
                sz.worker.register_filelike(f.id, MemIO(str(f.id), factory))
            else:
                sz.worker.register_filelike(f.id, None)
        sz.worker.extract(sz.fp, None, parallel=False)

    def _fill_link_targets(self, result: list[ArchiveEntry]) -> None:
        links = [e for e in result if e.type is EntryType.SYMLINK]
        if not links:
            return
        found: dict[int, _CollectIO] = {}

        def make(index: int) -> Py7zIO:
            found[index] = _CollectIO()
            return found[index]

        # Encrypted data without a password, or damage: the targets stay unknown and the
        # safety layer blocks such links. A later password change reloads the entries.
        with contextlib.suppress(Exception):
            self._run({e.index for e in links}, make)
        for entry in links:
            stream = found.get(entry.index)
            if stream is not None:
                target = bytes(stream.data).decode("utf-8", errors="replace")
                result[entry.index] = _with_target(entry, target)

    # -- errors ----------------------------------------------------------------------------

    def _encrypted(self) -> bool:
        return any(e.encrypted for e in self.entries())

    def _translate(
        self, exc: BaseException, name: str | None = None, *, key_proven: bool = False
    ) -> ZipwiseError:
        """``key_proven``: an encrypted entry was already decoded with a matching CRC in this
        pass, so the password is right and a later failure can only be damage."""
        if isinstance(exc, ZipwiseError):
            return exc
        what = f"{name!r}" if name else self.path.name
        if isinstance(exc, py7zr.exceptions.PasswordRequired):
            return PasswordRequired("This archive is password protected")
        if isinstance(exc, UnsupportedCompressionMethodError):
            return UnsupportedFormat(f"{what} uses an unsupported compression method")
        if isinstance(exc, OSError) and exc.errno is not None:
            return translate_oserror(exc, f"cannot read {what}")
        if self._password and self._encrypted() and not key_proven:
            # A wrong key decrypts to garbage, which only shows up as a decoding or CRC error,
            # exactly like damaged data: 7z gives no cheap way to tell the two apart.
            return WrongPassword(
                f"Wrong password, or damaged data in {what}",
                hint="Check the password. If it is right, the archive is damaged.",
            )
        return CorruptArchive(f"{what} is damaged: {exc}")

    # -- extraction ------------------------------------------------------------------------

    def extract(self, entries: Sequence[ArchiveEntry], sink: EntrySink, ctx: JobContext) -> None:
        files: dict[int, ArchiveEntry] = {}
        for entry in entries:
            ctx.raise_if_cancelled()
            if entry.type is EntryType.FILE:
                files[entry.index] = entry
                continue
            ctx.set_entry(entry.name)
            stream = sink.open(entry)  # directories and links are created by the sink itself
            if stream is not None:
                sink.close(entry, stream, True)
        if not files:
            return
        session = _SinkSession(files, sink, ctx)
        try:
            self._run(files, session.create)
        except BaseException as exc:
            session.abandon()
            if isinstance(exc, ZipwiseError | KeyboardInterrupt | SystemExit):
                raise
            raise self._translate(exc, session.current, key_proven=session.key_proven) from exc

    def check_password(self) -> None:
        probe = [e for e in self.entries() if e.encrypted and e.type is EntryType.FILE and e.size]
        if not probe:
            return
        if not self._password:
            raise PasswordRequired("This archive is password protected")
        first = probe[0]
        try:
            self._run({first.index}, lambda _i: _ProbeIO())
        except _Probed:
            return
        except Exception as exc:
            raise self._translate(exc, first.name) from exc

    def test(self, ctx: JobContext) -> list[Issue]:
        entries = self.entries()
        files = {e.index: e for e in entries if e.type is EntryType.FILE}
        for entry in entries:
            if entry.type is not EntryType.FILE:
                ctx.entry_done()
        current: list[ArchiveEntry] = []
        proven: list[bool] = [False]

        def make(index: int) -> Py7zIO:
            ctx.raise_if_cancelled()
            if current and current[0].encrypted:  # the previous entry passed its CRC check
                proven[0] = True
            current[:] = [files[index]]
            ctx.set_entry(files[index].name)
            return _CountingIO(ctx)

        try:
            if files:
                self._run(files, make)
        except ZipwiseError:
            raise
        except Exception as exc:
            name = current[0].name if current else None
            error = self._translate(exc, name, key_proven=proven[0])
            if isinstance(error, WrongPassword | PasswordRequired | IOFailure):
                raise error from exc
            return [Issue("error", error.code, name, error.message)]
        return []

    def info(self) -> ArchiveInfo:
        entries = self.entries()
        try:
            packed = sum(self._sz.header.main_streams.packinfo.packsizes)
        except (AttributeError, TypeError):
            packed = streams.input_size(self.path, self.source)
        return ArchiveInfo(
            path=self.path,
            format=FormatId.SEVENZ,
            entries_count=len(entries),
            total_size=sum(e.size for e in entries if e.type is EntryType.FILE),
            compressed_size=packed,
            encrypted=any(e.encrypted for e in entries),
        )

    def close(self) -> None:
        try:
            self._release()
        finally:
            self.run_cleanups()

    def _release(self) -> None:
        """Close py7zr and the read handle; a shared ``source`` itself stays open."""
        sz, fh = self._sz, self._fh
        self._sz = self._fh = None
        try:
            if sz is not None:
                sz.close()
        finally:
            if fh is not None:
                fh.close()


def _with_target(entry: ArchiveEntry, target: str) -> ArchiveEntry:
    return dataclasses.replace(entry, link_target=target)


# -- writing -------------------------------------------------------------------------------


def _attributes(kind: MemberType, mode: int, *, readonly: bool = False) -> int:
    """Windows attribute bits plus the Unix type and permission bits in the high word."""
    unix = kind.unix_file_type_bits | (mode & 0o7777)
    attrs = kind.win32_file_attributes | FILE_ATTRIBUTE_UNIX_EXTENSION
    attrs |= unix << FILE_ATTRIBUTE_UNIX_SHIFT
    if readonly:
        attrs |= _FILE_ATTRIBUTE_READONLY
    return attrs


class _SourceReader(io.BufferedIOBase):
    """Feeds a source file to py7zr while honouring cancellation and progress."""

    def __init__(self, fh: BinaryIO, ctx: JobContext) -> None:
        self._fh = fh
        self._ctx = ctx

    def read(self, size: int | None = -1) -> bytes:
        self._ctx.raise_if_cancelled()
        data = self._fh.read(CHUNK_SIZE if size is None or size < 0 else size)
        self._ctx.advance(len(data))
        return data

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._fh.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._fh.seek(offset, whence)


class SevenZipWriter(ArchiveWriter):
    """Writes a solid LZMA2 archive. The per-file ``store`` flag is ignored (7z is solid)."""

    format = FormatId.SEVENZ

    def __init__(
        self, dest: BinaryIO | Path, options: CreateOptions, ctx: JobContext | None = None
    ) -> None:
        super().__init__(dest, options, ctx)
        password = options.password or None
        if options.encrypt_names and password is None:
            raise UsageError("Encrypting file names needs a password")
        filters: list[dict[str, int]] = [
            {"id": FILTER_LZMA2, "preset": resolve_level(options.level)}
        ]
        if password is not None:
            filters.append({"id": FILTER_CRYPTO_AES256_SHA256})
        self._owned: BinaryIO | None = None
        stream: BinaryIO
        if isinstance(dest, Path):
            try:
                stream = self._owned = dest.open("wb")
            except OSError as exc:
                raise translate_oserror(exc, f"cannot create {dest.name}") from exc
        else:
            stream = dest
        try:
            self._sz = py7zr.SevenZipFile(
                stream,
                "w",
                filters=filters,
                password=password,
                header_encryption=bool(options.encrypt_names),
            )
        except BaseException:
            if self._owned is not None:
                self._owned.close()
            raise

    def _stamp(self, info: dict[str, Any], mtime: float, attributes: int) -> None:
        ts = ArchiveTimestamp.from_datetime(mtime)
        info["creationtime"] = info["lastwritetime"] = info["lastaccesstime"] = ts
        info["attributes"] = attributes

    def _writef(self, data: BinaryIO, arcname: str) -> dict[str, Any]:
        try:
            self._sz.writef(data, arcname)
        except ValueError as exc:
            raise UsageError(f"Cannot store {arcname!r}: {exc}") from exc
        except OSError as exc:
            raise translate_oserror(exc, f"cannot write {arcname}") from exc
        info: dict[str, Any] = self._sz.header.files_info.files[-1]
        return info

    def add_file(self, src: Path, arcname: str, *, store: bool = False) -> None:
        self.ctx.raise_if_cancelled()
        self.ctx.set_entry(arcname)
        try:
            st = os.stat(src)
            fin = open(src, "rb")  # noqa: SIM115 - closed in the with-block below
        except OSError as exc:
            raise translate_oserror(exc, f"cannot read {src}") from exc
        mode = normalised_mode(st, False)
        with fin:
            info = self._writef(_SourceReader(fin, self.ctx), arcname)  # type: ignore[arg-type]
        self._stamp(
            info, st.st_mtime, _attributes(MemberType.FILE, mode, readonly=not mode & 0o200)
        )
        self.ctx.entry_done()

    def add_dir(self, arcname: str, mtime: float, mode: int | None) -> None:
        self.ctx.raise_if_cancelled()
        self.ctx.set_entry(arcname)
        sz = self._sz
        folder = sz.header.initialize()
        info: Any = {
            "origin": None,
            "filename": arcname.rstrip("/"),
            "emptystream": True,
        }
        self._stamp(info, mtime, _attributes(MemberType.DIRECTORY, 0o755 if mode is None else mode))
        sz.header.files_info.files.append(info)
        sz.header.files_info.emptyfiles.append(True)
        sz.files.append(info)
        sz.worker.archive(sz.fp, sz.files, folder, deref=False)
        self.ctx.entry_done()

    def add_symlink(self, arcname: str, target: str, mtime: float) -> None:
        self.ctx.raise_if_cancelled()
        self.ctx.set_entry(arcname)
        info = self._writef(io.BytesIO(target.encode("utf-8")), arcname)  # type: ignore[arg-type]
        self._stamp(info, mtime, _attributes(MemberType.SYMLINK, stat.S_IMODE(0o777)))
        self.ctx.entry_done()

    def close(self) -> None:
        try:
            self._sz.close()
        except OSError as exc:
            raise translate_oserror(exc, "cannot finish the archive") from exc
        finally:
            if self._owned is not None:
                self._owned.close()

    def abort(self) -> None:
        # Finalising would write a header for a half-built archive; just drop the output.
        if self._owned is not None:
            self._owned.close()
