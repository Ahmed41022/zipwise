"""Backend interfaces. Backends read and write archives; they never touch the destination
file system, which is the job of the :class:`EntrySink` supplied by ``ops.extract``."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol

from ..fsutil import copy_stream
from ..jobs import JobContext
from ..model import ArchiveEntry, ArchiveInfo, CreateOptions, FormatId, Issue


class EntrySink(Protocol):
    """Receives entries from a reader. Implemented by ``ops.extract``."""

    def open(self, entry: ArchiveEntry) -> BinaryIO | None:
        """Return a stream for the entry's data, or None when there is nothing to copy
        (skipped entry, directory, link). Non-file entries are handled by the sink itself."""
        ...

    def close(self, entry: ArchiveEntry, stream: BinaryIO, ok: bool) -> None:
        """Called once per stream returned by :meth:`open`, with ``ok`` False on failure."""
        ...


class ArchiveReader(ABC):
    format: FormatId

    def __init__(
        self,
        path: Path,
        *,
        password: str | None = None,
        name_encoding: str | None = None,
        source: BinaryIO | None = None,
    ) -> None:
        """``source`` is an optional seekable binary file object to read instead of opening
        ``path`` (used for multi-part archives, see ``zipwise.split.MultiPartFile``). The
        reader never closes it: whoever supplied it registers a cleanup (:meth:`add_cleanup`)."""
        self.path = Path(path)
        self.origin = self.path  # the file the user named, even when ``path`` is a temp copy
        self.source = source
        self.multipart = False  # set by the opener when the input is a set of numbered parts
        self._password = password
        self.name_encoding = name_encoding
        self._cleanups: list[Callable[[], None]] = []

    @property
    def password(self) -> str | None:
        return self._password

    @password.setter
    def password(self, value: str | None) -> None:
        self._password = value

    @abstractmethod
    def entries(self) -> list[ArchiveEntry]: ...

    @abstractmethod
    def extract(
        self, entries: Sequence[ArchiveEntry], sink: EntrySink, ctx: JobContext
    ) -> None: ...

    @abstractmethod
    def test(self, ctx: JobContext) -> list[Issue]: ...

    @abstractmethod
    def info(self) -> ArchiveInfo: ...

    @abstractmethod
    def close(self) -> None: ...

    def issues(self) -> list[Issue]:
        """Problems found while reading (for example names that needed a fallback decoding).
        Meaningful after :meth:`entries` has been called."""
        return []

    def add_cleanup(self, action: Callable[[], None]) -> None:
        """Run ``action`` once when the reader is released (``with`` exit or run_cleanups)."""
        self._cleanups.append(action)

    def run_cleanups(self) -> None:
        pending, self._cleanups = self._cleanups, []
        for action in reversed(pending):
            action()

    def check_password(self) -> None:  # noqa: B027 - optional hook
        """Raise PasswordRequired / WrongPassword early when the archive is encrypted."""

    def pull_extract(
        self,
        entries: Sequence[ArchiveEntry],
        sink: EntrySink,
        ctx: JobContext,
        open_entry: Callable[[ArchiveEntry], BinaryIO],
    ) -> None:
        """Helper for pull-style formats: turns "open the entry's stream" into sink calls."""
        for entry in entries:
            ctx.raise_if_cancelled()
            ctx.set_entry(entry.name)
            out = sink.open(entry)
            if out is None:
                continue
            ok = False
            try:
                with open_entry(entry) as src:
                    copy_stream(src, out, ctx)
                ok = True
            finally:
                sink.close(entry, out, ok)

    def __enter__(self) -> ArchiveReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self.close()
        finally:
            self.run_cleanups()


class ArchiveWriter(ABC):
    format: FormatId

    def __init__(
        self, dest: BinaryIO | Path, options: CreateOptions, ctx: JobContext | None = None
    ) -> None:
        self.dest = dest
        self.options = options
        self.ctx = ctx or JobContext()

    @abstractmethod
    def add_file(self, src: Path, arcname: str, *, store: bool = False) -> None: ...

    @abstractmethod
    def add_dir(self, arcname: str, mtime: float, mode: int | None) -> None: ...

    @abstractmethod
    def add_symlink(self, arcname: str, target: str, mtime: float) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def abort(self) -> None:  # noqa: B027 - optional hook
        """Release resources after a failure without finalising the archive."""

    def __enter__(self) -> ArchiveWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()
