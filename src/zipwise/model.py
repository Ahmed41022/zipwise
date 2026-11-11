"""Data types shared by every layer. All dataclasses are frozen."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .errors import ExitCode

if TYPE_CHECKING:
    from .safety import SafetyPolicy


class FormatId(Enum):
    ZIP = "zip"
    SEVENZ = "7z"
    TAR = "tar"
    TAR_GZ = "tar.gz"
    TAR_BZ2 = "tar.bz2"
    TAR_XZ = "tar.xz"
    TAR_ZST = "tar.zst"
    GZ = "gz"
    BZ2 = "bz2"
    XZ = "xz"
    ZST = "zst"
    RAR = "rar"


class EntryType(Enum):
    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    OTHER = "other"


class ConflictAction(Enum):
    SKIP = "skip"
    RENAME = "rename"
    OVERWRITE = "overwrite"
    ASK = "ask"


Level = Literal["fast", "normal", "best"] | int
Severity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class ArchiveEntry:
    name: str
    raw_name: bytes | None = None
    type: EntryType = EntryType.FILE
    size: int = 0
    compressed_size: int | None = None
    mtime: datetime | None = None
    mode: int | None = None
    link_target: str | None = None
    encrypted: bool = False
    crc: int | None = None
    index: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "type": self.type.value,
            "size": self.size,
            "compressed_size": self.compressed_size,
            "mtime": self.mtime.isoformat() if self.mtime else None,
            "mode": self.mode,
            "link_target": self.link_target,
            "encrypted": self.encrypted,
            "crc": self.crc,
            "index": self.index,
        }


@dataclass(frozen=True)
class ArchiveInfo:
    path: Path
    format: FormatId
    entries_count: int
    total_size: int
    compressed_size: int
    encrypted: bool
    name_encoding: str | None = None
    encoding_confidence: float | None = None
    multipart: bool = False
    comment: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "format": self.format.value,
            "entries_count": self.entries_count,
            "total_size": self.total_size,
            "compressed_size": self.compressed_size,
            "encrypted": self.encrypted,
            "name_encoding": self.name_encoding,
            "encoding_confidence": self.encoding_confidence,
            "multipart": self.multipart,
            "comment": self.comment,
        }


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    entry: str | None
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "entry": self.entry,
            "message": self.message,
        }


@dataclass(frozen=True)
class OperationReport:
    ok: bool
    issues: tuple[Issue, ...] = ()
    entries_done: int = 0
    entries_skipped: int = 0
    bytes_done: int = 0
    output_paths: tuple[Path, ...] = ()
    elapsed: float = 0.0

    @classmethod
    def build(
        cls,
        issues: list[Issue] | tuple[Issue, ...],
        *,
        entries_done: int = 0,
        entries_skipped: int = 0,
        bytes_done: int = 0,
        output_paths: list[Path] | tuple[Path, ...] = (),
        elapsed: float = 0.0,
    ) -> OperationReport:
        return cls(
            ok=not any(i.severity == "error" for i in issues),
            issues=tuple(issues),
            entries_done=entries_done,
            entries_skipped=entries_skipped,
            bytes_done=bytes_done,
            output_paths=tuple(output_paths),
            elapsed=elapsed,
        )

    @property
    def exit_code(self) -> ExitCode:
        """0 when clean, 1 when anything was skipped, blocked or flagged."""
        if any(i.severity in ("warning", "error") for i in self.issues):
            return ExitCode.WARNINGS
        return ExitCode.OK

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
            "entries_done": self.entries_done,
            "entries_skipped": self.entries_skipped,
            "bytes_done": self.bytes_done,
            "output_paths": [str(p) for p in self.output_paths],
            "elapsed": round(self.elapsed, 6),
        }


@dataclass(frozen=True)
class CreateOptions:
    format: FormatId = FormatId.ZIP
    level: Level = "normal"
    password: str | None = None
    encrypt_names: bool = False
    smart_store: bool = True
    verify: bool = True
    split_size: int | None = None
    follow_symlinks: bool = False
    root_dir: Path | None = None
    force_zip64: bool = False


def _default_policy() -> SafetyPolicy:
    from .safety import SafetyPolicy

    return SafetyPolicy()


@dataclass(frozen=True)
class ExtractOptions:
    password: str | None = None
    name_encoding: str | None = None
    policy: SafetyPolicy = field(default_factory=_default_policy)
    on_conflict: ConflictAction = ConflictAction.ASK
    only: list[str] | None = None
    resume: bool = False
    restore_mode: bool = True
    strip_components: int = 0
