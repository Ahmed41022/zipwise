"""RAR backend: read-only, through ``rarfile`` plus an external extraction tool.

Zipwise never creates RAR archives and does not bundle the proprietary tool. Listing only
parses headers; extracting and testing need ``unrar``, ``unar``, ``7z`` or ``bsdtar`` on PATH.
"""

from __future__ import annotations

import shutil
import stat
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

import rarfile

from ..errors import (
    BackendUnavailable,
    CorruptArchive,
    PasswordRequired,
    UnsupportedFormat,
    UsageError,
    WrongPassword,
    ZipwiseError,
)
from ..fsutil import drain_stream, translate_oserror
from ..jobs import JobContext
from ..model import ArchiveEntry, ArchiveInfo, EntryType, FormatId, Issue
from .base import ArchiveReader, EntrySink

TOOLS = ("unrar", "unar", "7z", "7zz", "bsdtar")
INSTALL_HINT = (
    "Install an extraction tool. Windows: winget install RARLab.WinRAR "
    "(or: winget install 7zip.7zip). Linux: sudo apt install unrar"
)
_RAR_OS_UNIX = 3
_SYMLINK_REDIRECTS = (1, 2)  # RAR5 "unix symlink" and "windows symlink"


def find_tool() -> str | None:
    """Name of the first extraction tool found on PATH, or None."""
    for tool in TOOLS:
        if shutil.which(tool):
            return tool
    return None


def require_tool() -> str:
    """Pick a tool for rarfile, or raise ``BackendUnavailable`` with install instructions."""
    available = {tool for tool in TOOLS if shutil.which(tool)}
    if not available:
        raise BackendUnavailable(
            "Reading RAR archives needs an external tool (unrar, unar, 7z or bsdtar), "
            "and none was found on PATH",
            hint=INSTALL_HINT,
        )
    try:
        rarfile.tool_setup(
            unrar="unrar" in available,
            unar="unar" in available,
            bsdtar="bsdtar" in available,
            sevenzip="7z" in available,
            sevenzip2="7zz" in available,
            force=True,
        )
    except (rarfile.RarCannotExec, OSError) as exc:
        raise BackendUnavailable(
            "A tool for reading RAR archives was found but could not be run", hint=INSTALL_HINT
        ) from exc
    return sorted(available, key=TOOLS.index)[0]


class _Guard:
    def __init__(self, inner: Any, entry: ArchiveEntry, reader: RarReader) -> None:
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


class RarReader(ArchiveReader):
    format = FormatId.RAR

    def __init__(
        self, path: Path, *, password: str | None = None, name_encoding: str | None = None
    ) -> None:
        super().__init__(path, password=password, name_encoding=name_encoding)
        self._entries: list[ArchiveEntry] | None = None
        self._rf: Any = self._open()

    def _open(self) -> Any:
        try:
            return rarfile.RarFile(str(self.path), errors="strict")
        except FileNotFoundError as exc:
            raise UsageError(f"File not found: {self.path}") from exc
        except rarfile.PasswordRequired as exc:
            raise PasswordRequired("This archive is password protected") from exc
        except rarfile.NoCrypto as exc:
            raise BackendUnavailable(
                "This RAR archive has encrypted headers", hint="Install: pip install cryptography"
            ) from exc
        except rarfile.NeedFirstVolume as exc:
            raise UsageError(
                "This is a later volume of a multi-volume RAR set",
                hint="Open the first volume (.part1.rar or .rar) instead.",
            ) from exc
        except (rarfile.BadRarFile, rarfile.NotRarFile) as exc:
            raise CorruptArchive(
                f"Not a valid RAR file: {self.path.name} ({exc})",
                hint="The archive may be truncated or damaged.",
            ) from exc
        except OSError as exc:
            raise translate_oserror(exc, f"cannot read {self.path.name}") from exc

    def _translate(self, exc: Exception, entry: ArchiveEntry) -> ZipwiseError:
        if isinstance(exc, ZipwiseError):
            return exc
        if isinstance(exc, rarfile.PasswordRequired):
            return PasswordRequired("This archive is password protected")
        if isinstance(exc, rarfile.RarWrongPassword):
            return WrongPassword(f"Wrong password for {entry.name!r}")
        if isinstance(exc, rarfile.RarCannotExec):
            return BackendUnavailable("The RAR extraction tool could not be run", hint=INSTALL_HINT)
        if isinstance(exc, rarfile.BadRarFile | rarfile.RarCRCError | rarfile.RarExecError):
            if entry.encrypted and self._password and isinstance(exc, rarfile.BadRarFile):
                return WrongPassword(f"Wrong password, or damaged data in {entry.name!r}")
            return CorruptArchive(f"{entry.name!r} is damaged: {exc}")
        if isinstance(exc, rarfile.Error):
            return UnsupportedFormat(f"{entry.name!r} cannot be read: {exc}")
        if isinstance(exc, OSError):
            return translate_oserror(exc, f"cannot read {entry.name}")
        raise exc

    @staticmethod
    def _mtime(info: Any) -> datetime | None:
        if isinstance(info.mtime, datetime):
            return info.mtime
        try:
            return datetime(*info.date_time)
        except (TypeError, ValueError):
            return None

    def entries(self) -> list[ArchiveEntry]:
        if self._entries is not None:
            return self._entries
        result: list[ArchiveEntry] = []
        for index, info in enumerate(self._rf.infolist()):
            link: str | None = None
            redirect = getattr(info, "file_redir", None)
            if info.is_dir():
                kind = EntryType.DIR
            elif redirect and redirect[0] in _SYMLINK_REDIRECTS:
                kind, link = EntryType.SYMLINK, redirect[2]
            elif redirect or info.is_symlink():
                kind = EntryType.OTHER
            else:
                kind = EntryType.FILE
            mode = None
            if info.host_os == _RAR_OS_UNIX and isinstance(info.mode, int):
                mode = stat.S_IMODE(info.mode)
            result.append(
                ArchiveEntry(
                    name=info.filename.rstrip("/"),
                    type=kind,
                    size=info.file_size or 0,
                    compressed_size=info.compress_size,
                    mtime=self._mtime(info),
                    mode=mode,
                    link_target=link,
                    encrypted=bool(info.needs_password()),
                    crc=info.CRC,
                    index=index,
                )
            )
        self._entries = result
        return result

    def _open_entry(self, entry: ArchiveEntry) -> _Guard:
        require_tool()
        info = self._rf.infolist()[entry.index]
        try:
            inner = self._rf.open(info, pwd=self._password or None)
        except Exception as exc:
            raise self._translate(exc, entry) from exc
        return _Guard(inner, entry, self)

    def check_password(self) -> None:
        entry = next((e for e in self.entries() if e.encrypted and e.type is EntryType.FILE), None)
        if entry is None:
            return
        if not self._password:
            raise PasswordRequired("This archive is password protected")
        with self._open_entry(entry) as fh:
            fh.read(1)

    def extract(self, entries: Sequence[ArchiveEntry], sink: EntrySink, ctx: JobContext) -> None:
        require_tool()
        self.pull_extract(entries, sink, ctx, self._open_entry)  # type: ignore[arg-type]

    def test(self, ctx: JobContext) -> list[Issue]:
        require_tool()
        issues: list[Issue] = []
        for entry in self.entries():
            ctx.raise_if_cancelled()
            ctx.set_entry(entry.name)
            if entry.type is EntryType.FILE:
                try:
                    with self._open_entry(entry) as fh:
                        drain_stream(fh, ctx)  # type: ignore[arg-type]
                except (CorruptArchive, UnsupportedFormat) as exc:
                    issues.append(Issue("error", exc.code, entry.name, exc.message))
            ctx.entry_done()
        return issues

    def info(self) -> ArchiveInfo:
        entries = self.entries()
        return ArchiveInfo(
            path=self.path,
            format=FormatId.RAR,
            entries_count=len(entries),
            total_size=sum(e.size for e in entries if e.type is EntryType.FILE),
            compressed_size=sum(e.compressed_size or 0 for e in entries),
            encrypted=any(e.encrypted for e in entries),
            comment=self._rf.comment or "",
        )

    def close(self) -> None:
        self._rf.close()
