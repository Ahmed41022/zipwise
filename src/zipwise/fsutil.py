"""File-system helpers: atomic output files, long paths, metadata, source walking."""

from __future__ import annotations

import contextlib
import errno
import ntpath
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, cast

from .errors import IOFailure, UsageError
from .jobs import JobContext
from .model import EntryType

IS_WINDOWS = os.name == "nt"
CHUNK_SIZE = 1024 * 1024
LONG_PATH_THRESHOLD = 248


def long_path(path: Path | str, *, windows: bool | None = None) -> Path:
    """Return an absolute path, with the extended-length prefix on Windows when it is long."""
    use_windows = IS_WINDOWS if windows is None else windows
    if not use_windows:
        return Path(os.path.abspath(path))
    text = ntpath.abspath(str(path))
    if text.startswith("\\\\?\\") or len(text) < LONG_PATH_THRESHOLD:
        return Path(text)
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def translate_oserror(exc: OSError, what: str = "") -> IOFailure:
    """Turn an OSError into an IOFailure with a useful hint."""
    detail = exc.strerror or str(exc)
    prefix = f"{what}: " if what else ""
    if exc.errno == errno.ENOSPC:
        return IOFailure(
            f"{prefix}not enough free disk space",
            hint="Free some space and retry.",
            code="disk-full",
        )
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EROFS, errno.EPERM):
        return IOFailure(
            f"{prefix}permission denied ({detail})",
            hint="Check that the location is writable.",
            code="permission-denied",
        )
    return IOFailure(f"{prefix}{detail}")


def copy_stream(
    src: BinaryIO,
    dst: BinaryIO,
    ctx: JobContext,
    *,
    chunk_size: int = CHUNK_SIZE,
    on_chunk: Callable[[int], None] | None = None,
) -> int:
    """Copy ``src`` to ``dst`` in chunks, checking for cancellation and reporting progress."""
    total = 0
    while True:
        ctx.raise_if_cancelled()
        chunk = src.read(chunk_size)
        if not chunk:
            return total
        dst.write(chunk)
        total += len(chunk)
        if on_chunk is not None:
            on_chunk(len(chunk))
        ctx.advance(len(chunk))


class _NullWriter:
    def write(self, data: bytes) -> int:
        return len(data)


def drain_stream(src: BinaryIO, ctx: JobContext, *, chunk_size: int = CHUNK_SIZE) -> int:
    """Read ``src`` to the end, discarding the data (used to verify checksums)."""
    return copy_stream(src, cast(BinaryIO, _NullWriter()), ctx, chunk_size=chunk_size)


class AtomicFile:
    """Writes to ``<final><suffix>`` and renames onto ``final`` only on :meth:`commit`.

    Used as a context manager, the temporary file is removed if the block raises.
    """

    def __init__(self, final: Path, suffix: str = ".partial") -> None:
        self.final = Path(final)
        self.temp = self.final.with_name(self.final.name + suffix)
        self._stream: BinaryIO | None = None
        self._done = False

    def open(self) -> BinaryIO:
        try:
            self._stream = long_path(self.temp).open("wb")
        except OSError as exc:
            raise translate_oserror(exc, f"cannot create {self.temp.name}") from exc
        return self._stream

    @property
    def stream(self) -> BinaryIO:
        if self._stream is None:
            raise RuntimeError("AtomicFile is not open")
        return self._stream

    def commit(self) -> None:
        if self._done:
            return
        stream = self._stream
        if stream is not None and not stream.closed:
            stream.close()
        try:
            clear_readonly(self.final)
            os.replace(long_path(self.temp), long_path(self.final))
        except OSError as exc:
            self.discard()
            raise translate_oserror(exc, f"cannot write {self.final.name}") from exc
        self._done = True

    def discard(self) -> None:
        if self._done:
            return
        self._done = True
        stream = self._stream
        if stream is not None and not stream.closed:
            with contextlib.suppress(OSError):
                stream.close()
        remove_quietly(self.temp)

    def __enter__(self) -> AtomicFile:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.discard()


def remove_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        long_path(path).unlink()


def clear_readonly(path: Path) -> None:
    """Make an existing file writable so it can be replaced (needed on Windows)."""
    try:
        target = long_path(path)
        if target.exists() and not target.is_symlink():
            os.chmod(target, target.stat().st_mode | stat.S_IWRITE)
    except OSError:
        pass


def to_timestamp(value: datetime) -> float:
    """Naive datetimes are local time (DOS style); aware ones are exact."""
    return value.timestamp()


def set_mtime(path: Path, mtime: datetime | float | None) -> None:
    if mtime is None:
        return
    try:
        ts = to_timestamp(mtime) if isinstance(mtime, datetime) else float(mtime)
        os.utime(long_path(path), (ts, ts))
    except (OSError, OverflowError, ValueError):
        pass


def apply_mode(path: Path, mode: int | None) -> None:
    """Restore POSIX permission bits (masked to 0o777; setuid, setgid and sticky are dropped).
    A no-op on Windows."""
    if mode is None or IS_WINDOWS:
        return
    with contextlib.suppress(OSError):
        os.chmod(path, mode & 0o777)


def free_space(path: Path) -> int:
    """Free bytes on the volume holding ``path`` (or its nearest existing parent)."""
    probe = Path(os.path.abspath(path))
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def is_case_insensitive(directory: Path) -> bool:
    """Probe the file system holding ``directory`` instead of trusting the OS name."""
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".zwProbe", suffix="aB", dir=directory)
    os.close(fd)
    try:
        swapped = os.path.join(os.path.dirname(name), os.path.basename(name).swapcase())
        return os.path.exists(swapped)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


def ensure_writable_dir(directory: Path) -> None:
    """Create ``directory`` if needed and prove that files can be created in it."""
    try:
        long_path(directory).mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".zwwrite", dir=long_path(directory))
        os.close(fd)
        os.unlink(name)
    except (FileExistsError, NotADirectoryError) as exc:
        raise UsageError(f"Destination is not a directory: {directory}") from exc
    except OSError as exc:
        raise translate_oserror(exc, f"cannot write to {directory}") from exc


def make_symlink(link: Path, target: str, *, target_is_dir: bool = False) -> bool:
    """Create a symlink; False when the OS refuses (e.g. Windows without privilege)."""
    native = target.replace("/", os.sep) if IS_WINDOWS else target
    try:
        os.symlink(native, long_path(link), target_is_directory=target_is_dir)
    except (OSError, NotImplementedError):
        return False
    return True


def can_create_symlinks(directory: Path) -> bool:
    """Probe whether the current user may create symlinks in ``directory``."""
    probe = Path(tempfile.mkdtemp(prefix="zwlink", dir=directory))
    try:
        return make_symlink(probe / "l", "t")
    finally:
        shutil.rmtree(probe, ignore_errors=True)


@dataclass(frozen=True)
class SourceItem:
    path: Path
    arcname: str
    type: EntryType
    size: int
    mtime: float
    mode: int
    link_target: str | None = None


def normalised_mode(st: os.stat_result, is_dir: bool) -> int:
    if not IS_WINDOWS:
        return stat.S_IMODE(st.st_mode)
    if is_dir:
        return 0o755
    return 0o644 if st.st_mode & stat.S_IWRITE else 0o444


def _item_for(path: Path, arcname: str, follow: bool) -> SourceItem:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) and not follow:
        target = os.readlink(path).replace("\\", "/")
        return SourceItem(path, arcname, EntryType.SYMLINK, 0, st.st_mtime, 0o777, target)
    if follow:
        st = os.stat(path)
    if stat.S_ISDIR(st.st_mode):
        return SourceItem(path, arcname, EntryType.DIR, 0, st.st_mtime, normalised_mode(st, True))
    if stat.S_ISREG(st.st_mode):
        return SourceItem(
            path, arcname, EntryType.FILE, st.st_size, st.st_mtime, normalised_mode(st, False)
        )
    return SourceItem(path, arcname, EntryType.OTHER, 0, st.st_mtime, 0)


def walk_sources(
    sources: Sequence[Path],
    *,
    follow_symlinks: bool = False,
    root_dir: Path | None = None,
    exclude: Sequence[Path] = (),
) -> Iterator[SourceItem]:
    """Yield every file, directory (including empty ones) and symlink below ``sources``.

    Names are POSIX style and relative to ``root_dir`` when given, otherwise to the
    parent of each source, so a directory keeps its own name inside the archive.
    """
    skip = {os.path.normcase(os.path.abspath(p)) for p in exclude}
    active: set[str] = set()

    def arc(p: Path, base: Path) -> str:
        return p.relative_to(base).as_posix()

    def walk(directory: Path, base: Path) -> Iterator[SourceItem]:
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            raise translate_oserror(exc, f"cannot read {directory}") from exc
        for name in names:
            child = directory / name
            if os.path.normcase(os.path.abspath(child)) in skip:
                continue
            yield from visit(child, base)

    def visit(path: Path, base: Path) -> Iterator[SourceItem]:
        item = _item_for(path, arc(path, base), follow_symlinks)
        if item.type is EntryType.DIR:
            real = os.path.normcase(os.path.realpath(path))
            if real in active:
                return  # symlink loop
            active.add(real)
            if item.arcname != ".":
                yield item
            yield from walk(path, base)
            active.discard(real)
        else:
            yield item

    for source in sources:
        src = Path(source)
        if not os.path.lexists(src):
            raise UsageError(f"Input not found: {src}")
        absolute = Path(os.path.abspath(src))
        if root_dir is not None:
            base = Path(os.path.abspath(root_dir))
            try:
                absolute.relative_to(base)
            except ValueError:
                raise UsageError(f"{src} is not inside the root directory {root_dir}") from None
        else:
            base = absolute.parent
        if os.path.normcase(str(absolute)) in skip:
            continue
        yield from visit(absolute, base)
