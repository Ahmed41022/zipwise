"""ZIP backend: stdlib ``zipfile``, plus ``pyzipper`` for WinZip AES-256."""

from __future__ import annotations

import codecs
import dataclasses
import functools
import os
import stat
import struct
import time
import zipfile
import zlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, BinaryIO

from ..encoding import EncodingGuess, decode_name, detect_names
from ..errors import (
    BackendUnavailable,
    CorruptArchive,
    IOFailure,
    PasswordRequired,
    UnsupportedFormat,
    UsageError,
    WrongPassword,
    ZipwiseError,
)
from ..fsutil import copy_stream, drain_stream, normalised_mode, translate_oserror
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
from .base import ArchiveReader, ArchiveWriter, EntrySink

_FLAG_ENCRYPTED = 0x1
_FLAG_UTF8 = 0x800
_METHOD_AES = 99
_EXTRA_UNIX_TIME = 0x5455
_EXTRA_UNICODE_PATH = 0x7075
_MAX_LINK_BYTES = 64 * 1024
_DOS_MIN_YEAR, _DOS_MAX_YEAR = 1980, 2107


def _pyzipper() -> ModuleType:
    try:
        import pyzipper
    except ImportError as exc:
        raise BackendUnavailable(
            "Password-protected ZIP files need the 'pyzipper' package",
            hint="Install it with: pip install pyzipper",
        ) from exc
    return pyzipper


class _Utf8Info(zipfile.ZipInfo):
    """ZipInfo that always sets the UTF-8 name flag, even for ASCII names."""

    def _encodeFilenameFlags(self) -> tuple[bytes, int]:
        return self.filename.encode("utf-8"), self.flag_bits | _FLAG_UTF8


@functools.cache
def _aes_info_class() -> type:
    pz = _pyzipper()

    class _Utf8AESInfo(pz.zipfile_aes.AESZipInfo):  # type: ignore[misc,name-defined]
        def _encodeFilenameFlags(self) -> tuple[bytes, int]:
            return self.filename.encode("utf-8"), self.flag_bits | _FLAG_UTF8

    return _Utf8AESInfo


def _parse_extra(extra: bytes) -> dict[int, bytes]:
    fields: dict[int, bytes] = {}
    pos = 0
    while pos + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, pos)
        fields.setdefault(tag, extra[pos + 4 : pos + 4 + size])
        pos += 4 + size
    return fields


def _unix_time_extra(mtime: float) -> bytes:
    if not 0 <= int(mtime) <= 0xFFFFFFFF:
        return b""
    return struct.pack("<HHBI", _EXTRA_UNIX_TIME, 5, 1, int(mtime))


def _dos_time(mtime: float) -> tuple[int, int, int, int, int, int]:
    try:
        t = time.localtime(mtime)[:6]
    except (OverflowError, OSError, ValueError):
        t = (_DOS_MIN_YEAR, 1, 1, 0, 0, 0)
    if t[0] < _DOS_MIN_YEAR:
        return (_DOS_MIN_YEAR, 1, 1, 0, 0, 0)
    if t[0] > _DOS_MAX_YEAR:
        return (_DOS_MAX_YEAR, 12, 31, 23, 59, 58)
    return t  # type: ignore[return-value]


class ZipWriter(ArchiveWriter):
    format = FormatId.ZIP

    def __init__(
        self, dest: BinaryIO | Path, options: CreateOptions, ctx: JobContext | None = None
    ) -> None:
        super().__init__(dest, options, ctx)
        self._level = resolve_level(options.level)
        self._password = options.password.encode("utf-8") if options.password else None
        self._owned: BinaryIO | None = None
        stream: BinaryIO
        if isinstance(dest, Path):
            try:
                stream = self._owned = dest.open("wb")
            except OSError as exc:
                raise translate_oserror(exc, f"cannot create {dest.name}") from exc
        else:
            stream = dest
        self._zf: Any
        if self._password is not None:
            pz = _pyzipper()
            self._zf = pz.AESZipFile(
                stream,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=self._level,
                allowZip64=True,
                strict_timestamps=False,
                encryption=pz.WZ_AES,
                encryption_kwargs={"nbits": 256},
            )
            self._zf.setpassword(self._password)
            self._info_cls: type = _aes_info_class()
        else:
            self._zf = zipfile.ZipFile(
                stream,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=self._level,
                allowZip64=True,
                strict_timestamps=False,
            )
            self._info_cls = _Utf8Info

    def _new_info(self, name: str, mtime: float, attr: int) -> Any:
        info = self._info_cls(name, _dos_time(mtime))
        info.create_system = 3
        info.external_attr = attr
        info.extra = _unix_time_extra(mtime)
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
        attr = (stat.S_IFREG | mode) << 16
        if not mode & 0o200:
            attr |= 0x01  # DOS read-only
        info = self._new_info(arcname, st.st_mtime, attr)
        info.file_size = st.st_size
        if store or self._level == 0:
            info.compress_type = zipfile.ZIP_STORED
        else:
            info.compress_type = zipfile.ZIP_DEFLATED
            info._compresslevel = self._level
        with fin:
            try:
                with self._zf.open(info, "w", force_zip64=self.options.force_zip64) as fout:
                    copy_stream(fin, fout, self.ctx)
            except OSError as exc:
                raise translate_oserror(exc, f"cannot write {arcname}") from exc
        self.ctx.entry_done()

    def _write_plain(self, info: Any, data: bytes) -> None:
        """Write a small entry. Directories are never encrypted, so 7-Zip and others
        do not ask for a password to show an empty folder."""
        info.compress_type = zipfile.ZIP_STORED
        saved = (self._zf.encryption, self._zf.pwd) if self._password is not None else None
        if saved is not None and not data:
            self._zf.encryption, self._zf.pwd = None, None
        try:
            self._zf.writestr(info, data)
        finally:
            if saved is not None:
                self._zf.encryption, self._zf.pwd = saved

    def add_dir(self, arcname: str, mtime: float, mode: int | None) -> None:
        self.ctx.raise_if_cancelled()
        self.ctx.set_entry(arcname)
        perm = 0o755 if mode is None else mode & 0o777
        info = self._new_info(
            arcname.rstrip("/") + "/", mtime, ((stat.S_IFDIR | perm) << 16) | 0x10
        )
        self._write_plain(info, b"")
        self.ctx.entry_done()

    def add_symlink(self, arcname: str, target: str, mtime: float) -> None:
        self.ctx.raise_if_cancelled()
        self.ctx.set_entry(arcname)
        info = self._new_info(arcname, mtime, (stat.S_IFLNK | 0o777) << 16)
        self._write_plain(info, target.encode("utf-8"))
        self.ctx.entry_done()

    def close(self) -> None:
        try:
            self._zf.close()
        finally:
            if self._owned is not None:
                self._owned.close()

    def abort(self) -> None:
        # Without this the central directory would still be written on close.
        self._zf._didModify = False
        try:
            self._zf.close()
        finally:
            if self._owned is not None:
                self._owned.close()


class _Guard:
    """Wraps a ZipExtFile and turns library exceptions into Zipwise errors."""

    def __init__(self, inner: Any, entry: ArchiveEntry, reader: ZipReader) -> None:
        self._inner = inner
        self._entry = entry
        self._reader = reader

    def read(self, n: int = -1) -> bytes:
        try:
            data: bytes = self._inner.read(n)
        except Exception as exc:
            raise self._reader._translate(exc, self._entry) from exc
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


class ZipReader(ArchiveReader):
    format = FormatId.ZIP

    def __init__(
        self, path: Path, *, password: str | None = None, name_encoding: str | None = None
    ) -> None:
        super().__init__(path, password=password, name_encoding=name_encoding)
        if name_encoding is not None:
            try:
                codecs.lookup(name_encoding)
            except LookupError:
                raise UsageError(f"Unknown name encoding: {name_encoding}") from None
        self._entries: list[ArchiveEntry] | None = None
        self._guess: EncodingGuess | None = None
        self._zf: Any = self._open()
        self._infos: list[zipfile.ZipInfo] = self._zf.infolist()

    @property
    def password(self) -> str | None:
        return self._password

    @password.setter
    def password(self, value: str | None) -> None:
        self._password = value
        self._entries = None  # encrypted link targets can only be read with the password

    def _open(self) -> Any:
        try:
            zf: Any = zipfile.ZipFile(self.path, "r")
        except zipfile.BadZipFile as exc:
            raise CorruptArchive(
                f"Not a valid ZIP file: {self.path.name} ({exc})",
                hint="The archive may be truncated or damaged.",
            ) from exc
        except FileNotFoundError as exc:
            raise UsageError(f"File not found: {self.path}") from exc
        except OSError as exc:
            raise translate_oserror(exc, f"cannot read {self.path.name}") from exc
        if any(i.compress_type == _METHOD_AES for i in zf.infolist()):
            zf.close()
            try:
                zf = _pyzipper().AESZipFile(self.path, "r")
            except zipfile.BadZipFile as exc:
                raise CorruptArchive(f"Not a valid ZIP file: {self.path.name} ({exc})") from exc
        return zf

    def _pwd(self) -> bytes | None:
        return self._password.encode("utf-8") if self._password else None

    def _translate(self, exc: Exception, entry: ArchiveEntry) -> ZipwiseError:
        if isinstance(exc, ZipwiseError):
            return exc
        text = str(exc)
        if isinstance(exc, NotImplementedError):  # a RuntimeError subclass: check it first
            return UnsupportedFormat(f"{entry.name!r} uses an unsupported compression method")
        if isinstance(exc, RuntimeError):
            if "Bad password" in text:
                return WrongPassword(f"Wrong password for {entry.name!r}")
            if "password" in text:
                return PasswordRequired("This archive is password protected")
            return CorruptArchive(f"{entry.name!r}: {text}")
        if isinstance(exc, zipfile.BadZipFile):
            if entry.encrypted and self._password and "CRC" in text and not self._is_aes(entry):
                return WrongPassword(f"Wrong password for {entry.name!r}")
            return CorruptArchive(f"{entry.name!r} is damaged: {text}")
        if isinstance(exc, zlib.error | EOFError | struct.error | ValueError):
            return CorruptArchive(f"{entry.name!r} is damaged: {text}")
        if isinstance(exc, OSError):
            return translate_oserror(exc, f"cannot read {entry.name}")
        raise exc

    def _is_aes(self, entry: ArchiveEntry) -> bool:
        return getattr(self._infos[entry.index], "wz_aes_version", None) is not None

    def _open_entry(self, entry: ArchiveEntry) -> _Guard:
        try:
            inner = self._zf.open(self._infos[entry.index], "r", pwd=self._pwd())
        except Exception as exc:
            raise self._translate(exc, entry) from exc
        return _Guard(inner, entry, self)

    def _read_link(self, entry: ArchiveEntry) -> str | None:
        if entry.size > _MAX_LINK_BYTES or (entry.encrypted and not self._password):
            return None
        try:
            with self._open_entry(entry) as fh:
                return fh.read(_MAX_LINK_BYTES).decode("utf-8", errors="replace")
        except ZipwiseError:
            return None

    def _decode_names(self) -> list[tuple[bytes | None, str]]:
        raws: list[tuple[bytes | None, str | None]] = []
        for info in self._infos:
            if info.flag_bits & _FLAG_UTF8:
                raws.append((None, info.orig_filename))
                continue
            raw_bytes = info.orig_filename.encode("cp437")
            raws.append((raw_bytes, self._unicode_path(info, raw_bytes)))
        pending = [raw for raw, uni in raws if raw is not None and uni is None]
        if self.name_encoding is not None:
            self._guess = EncodingGuess(self.name_encoding, 1.0)
        elif pending:
            self._guess = detect_names(pending)
        else:
            self._guess = EncodingGuess("utf-8", 1.0)
        out: list[tuple[bytes | None, str]] = []
        for raw, text in raws:
            if text is not None:
                out.append((raw, text))
            else:
                assert raw is not None
                out.append((raw, decode_name(raw, self._guess.encoding)))
        return out

    @staticmethod
    def _unicode_path(info: zipfile.ZipInfo, raw: bytes) -> str | None:
        data = _parse_extra(info.extra).get(_EXTRA_UNICODE_PATH)
        if data is None or len(data) < 5 or data[0] != 1:
            return None
        if struct.unpack_from("<I", data, 1)[0] != zlib.crc32(raw):
            return None
        try:
            return data[5:].decode("utf-8")
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _mtime(info: zipfile.ZipInfo) -> datetime | None:
        ut = _parse_extra(info.extra).get(_EXTRA_UNIX_TIME)
        if ut is not None and len(ut) >= 5 and ut[0] & 1:
            return datetime.fromtimestamp(struct.unpack_from("<I", ut, 1)[0], tz=UTC)
        try:
            return datetime(*info.date_time)
        except ValueError:
            return None

    def entries(self) -> list[ArchiveEntry]:
        if self._entries is not None:
            return self._entries
        names = self._decode_names()
        result: list[ArchiveEntry] = []
        for index, info in enumerate(self._infos):
            raw, name = names[index]
            attr = info.external_attr >> 16
            unix = info.create_system == 3 and attr != 0
            kind = stat.S_IFMT(attr) if unix else 0
            if name.endswith("/") or info.external_attr & 0x10 or kind == stat.S_IFDIR:
                etype = EntryType.DIR
            elif kind == stat.S_IFLNK:
                etype = EntryType.SYMLINK
            elif kind in (0, stat.S_IFREG):
                etype = EntryType.FILE
            else:
                etype = EntryType.OTHER
            entry = ArchiveEntry(
                name=name.rstrip("/") if etype is EntryType.DIR else name,
                raw_name=raw,
                type=etype,
                size=info.file_size,
                compressed_size=info.compress_size,
                mtime=self._mtime(info),
                mode=stat.S_IMODE(attr) if unix else None,
                encrypted=bool(info.flag_bits & _FLAG_ENCRYPTED),
                crc=info.CRC,
                index=index,
            )
            result.append(entry)
        self._entries = result
        for i, entry in enumerate(result):
            if entry.type is EntryType.SYMLINK:
                target = self._read_link(entry)
                result[i] = dataclasses.replace(entry, link_target=target)
        return result

    def check_password(self) -> None:
        entry = next((e for e in self.entries() if e.encrypted and e.type is EntryType.FILE), None)
        if entry is None:
            return
        if not self._password:
            raise PasswordRequired("This archive is password protected")
        with self._open_entry(entry) as fh:
            fh.read(1)

    def extract(self, entries: Sequence[ArchiveEntry], sink: EntrySink, ctx: JobContext) -> None:
        self.pull_extract(entries, sink, ctx, self._open_entry)  # type: ignore[arg-type]

    def test(self, ctx: JobContext) -> list[Issue]:
        issues: list[Issue] = []
        for entry in self.entries():
            ctx.raise_if_cancelled()
            ctx.set_entry(entry.name)
            if entry.type is EntryType.FILE:
                try:
                    with self._open_entry(entry) as fh:
                        drain_stream(fh, ctx)  # type: ignore[arg-type]
                except (CorruptArchive, UnsupportedFormat, IOFailure) as exc:
                    issues.append(Issue("error", exc.code, entry.name, exc.message))
            ctx.entry_done()
        return issues

    def info(self) -> ArchiveInfo:
        entries = self.entries()
        guess = self._guess
        comment = self._zf.comment.decode("utf-8", errors="replace") if self._zf.comment else ""
        return ArchiveInfo(
            path=self.path,
            format=FormatId.ZIP,
            entries_count=len(entries),
            total_size=sum(e.size for e in entries),
            compressed_size=sum(e.compressed_size or 0 for e in entries),
            encrypted=any(e.encrypted for e in entries),
            name_encoding=guess.encoding if guess else None,
            encoding_confidence=guess.confidence if guess else None,
            comment=comment,
        )

    def close(self) -> None:
        self._zf.close()
