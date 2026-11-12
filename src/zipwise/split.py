"""Compress to fit: size presets, splitting into numbered parts, joining, a checksum manifest,
recipient notes and a seekable file object that reads a part set without joining it."""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, cast

from .errors import CorruptArchive, IOFailure, UsageError
from .fsutil import (
    CHUNK_SIZE,
    AtomicFile,
    clear_readonly,
    copy_stream,
    drain_stream,
    free_space,
    long_path,
    remove_quietly,
    translate_oserror,
)
from .jobs import JobContext

PRESETS: dict[str, int] = {
    "email": 25 * 1000**2,
    "whatsapp": 2 * 1000**3,
    "telegram": 2 * 1000**3,
    "telegram-premium": 4 * 1000**3,
    "fat32": 4 * 1024**3 - 1,
}

MANIFEST_SUFFIX = ".parts.json"
NOTES_NAME = "HOW-TO-OPEN.txt"
TEMP_SUFFIX = ".zwpart"
_MANIFEST_VERSION = 1
_MIN_DIGITS = 3

_PART_RE = re.compile(r"^(?P<base>.+)\.(?P<num>\d{3,})$")
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?|\.\d+)\s*([a-z]*)$")
_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "m": 1000**2,
    "mb": 1000**2,
    "g": 1000**3,
    "gb": 1000**3,
    "t": 1000**4,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def parse_size(value: str | int) -> int:
    """Parse ``25M``, ``700MiB``, ``1.5G``, a preset name (``email``) or plain bytes.

    Decimal units (K, M, G, KB, MB, GB) are powers of 1000; ``KiB``/``MiB``/``GiB`` are
    powers of 1024. Raises :class:`UsageError` for zero, negative or unparsable values.
    """
    if isinstance(value, bool):
        raise UsageError(f"Invalid size: {value!r}")
    if isinstance(value, int):
        if value < 1:
            raise UsageError(f"Size must be at least 1 byte, got {value}")
        return value
    text = value.strip().lower()
    preset = text.replace("_", "-").replace(" ", "-")
    if preset in PRESETS:
        return PRESETS[preset]
    match = _SIZE_RE.match(text)
    if match is None or match.group(2) not in _UNITS:
        raise UsageError(
            f"Invalid size: {value!r}",
            hint="Use a number with an optional unit, such as 25M, 700MiB, 1.5G, "
            f"or a preset: {', '.join(PRESETS)}.",
        )
    try:
        amount = Decimal(match.group(1)) * _UNITS[match.group(2)]
    except InvalidOperation:  # pragma: no cover - the regex only lets numbers through
        raise UsageError(f"Invalid size: {value!r}") from None
    size = int(amount)
    if size < 1:
        raise UsageError(f"Size must be at least 1 byte, got {value!r}")
    return size


# --- part naming and discovery --------------------------------------------------------------


def part_digits(count: int) -> int:
    """Width of the part number: at least 3 digits, more when there are over 999 parts."""
    return max(_MIN_DIGITS, len(str(count)))


def part_name(base: str, index: int, digits: int) -> str:
    """Name of part ``index`` (1-based) of ``base``."""
    return f"{base}.{index:0{digits}d}"


def manifest_name(base: str) -> str:
    return base + MANIFEST_SUFFIX


def is_part_path(path: Path | str) -> bool:
    """True for ``name.ext.001``-style names (three or more digits)."""
    return _PART_RE.match(Path(path).name) is not None


@dataclass(frozen=True)
class ManifestPart:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    original_name: str
    total_size: int
    sha256: str
    part_size: int
    parts: tuple[ManifestPart, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "format": "zipwise-parts",
            "version": _MANIFEST_VERSION,
            "original_name": self.original_name,
            "total_size": self.total_size,
            "sha256": self.sha256,
            "part_size": self.part_size,
            "parts": [{"name": p.name, "size": p.size, "sha256": p.sha256} for p in self.parts],
        }


def read_manifest(path: Path) -> Manifest:
    """Load a ``.parts.json`` file. Raises ``CorruptArchive`` when it is unreadable."""
    try:
        data = json.loads(long_path(path).read_text(encoding="utf-8"))
        if data.get("format") != "zipwise-parts":
            raise ValueError("not a Zipwise parts manifest")
        parts = tuple(
            ManifestPart(str(p["name"]), int(p["size"]), str(p["sha256"])) for p in data["parts"]
        )
        return Manifest(
            str(data["original_name"]),
            int(data["total_size"]),
            str(data["sha256"]),
            int(data["part_size"]),
            parts,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CorruptArchive(
            f"The parts manifest {path.name} is unreadable ({exc})",
            hint="Delete the manifest to join without checksum verification.",
            code="bad-manifest",
        ) from exc


@dataclass(frozen=True)
class PartSet:
    """The numbered parts of one split file, in order."""

    base: str
    directory: Path
    parts: tuple[Path, ...]
    digits: int
    manifest_path: Path | None = None
    manifest: Manifest | None = None
    stray: tuple[Path, ...] = ()

    @property
    def first(self) -> Path:
        return self.parts[0]

    @property
    def total_size(self) -> int:
        return sum(os.stat(long_path(p)).st_size for p in self.parts)


def _same_set(num: str, width: int) -> bool:
    if len(num) == width:
        return True
    # 7-Zip style overflow: 001..999 followed by 1000, 1001 (no zero padding)
    return width == _MIN_DIGITS and len(num) > width and not num.startswith("0")


def find_parts(any_part: Path | str) -> PartSet:
    """Locate every part of the set that ``any_part`` belongs to.

    Raises ``UsageError`` when the name is not a part name and ``CorruptArchive`` (code
    ``part-first-missing``, ``part-missing`` or ``part-size``) when a part is missing,
    numbering has a gap, or the manifest disagrees with what is on disk.
    """
    path = Path(os.path.abspath(any_part))
    match = _PART_RE.match(path.name)
    if match is None:
        raise UsageError(
            f"{path.name} is not a numbered part",
            hint="Parts are named like archive.zip.001, archive.zip.002, ...",
        )
    if not path.is_file():
        raise UsageError(f"File not found: {path}")
    base = match.group("base")
    given = match.group("num")
    width = len(given) if given.startswith("0") or len(given) == _MIN_DIGITS else _MIN_DIGITS
    directory = path.parent
    folded = os.path.normcase(base)
    numbers: dict[int, Path] = {}
    stray: list[Path] = []
    for entry in sorted(os.listdir(directory)):
        m = _PART_RE.match(entry)
        if m is None or os.path.normcase(m.group("base")) != folded:
            continue
        candidate = directory / entry
        if _same_set(m.group("num"), width):
            numbers[int(m.group("num"))] = candidate
        else:
            stray.append(candidate)
    if 1 not in numbers:
        raise CorruptArchive(
            f"The first part {part_name(base, 1, width)} is missing",
            hint="Put all parts of the file in the same folder.",
            code="part-first-missing",
        )
    found = sorted(numbers)
    for expected, actual in enumerate(found, start=1):
        if actual != expected:
            others = ", ".join(f".{n:0{width}d}" for n in found)
            raise CorruptArchive(
                f"Part {part_name(base, expected, width)} is missing (found {others})",
                hint="Put all parts of the file in the same folder.",
                code="part-missing",
            )
    parts = tuple(numbers[n] for n in found)
    manifest_path = directory / manifest_name(base)
    manifest: Manifest | None = None
    if manifest_path.is_file():
        with contextlib.suppress(CorruptArchive):
            manifest = read_manifest(manifest_path)
    result = PartSet(
        base, directory, parts, width, manifest_path if manifest_path.is_file() else None,
        manifest, tuple(stray),
    )  # fmt: skip
    if manifest is not None:
        _check_against_manifest(result, manifest)
    return result


def existing_set(dest: Path | str) -> PartSet | None:
    """The numbered set written for the archive name ``dest`` (``dest.001`` and on), or None
    when there is none or it cannot be identified (a gap, a manifest that disagrees)."""
    path = Path(os.path.abspath(dest))
    first = path.with_name(part_name(path.name, 1, _MIN_DIGITS))
    try:
        return find_parts(first) if first.is_file() else None
    except (CorruptArchive, UsageError, OSError):
        return None


def remove_set(old: PartSet, keep: Sequence[Path | str] = ()) -> list[Path]:
    """Delete the parts of ``old``, its manifest and, when it was written for this set,
    ``HOW-TO-OPEN.txt``. Files in ``keep`` and anything else in the folder stay."""
    kept = {os.path.normcase(os.path.abspath(p)) for p in keep}
    victims = list(old.parts)
    if old.manifest_path is not None:
        victims.append(old.manifest_path)
    notes = old.directory / NOTES_NAME
    if _notes_are_for(notes, old.base):
        victims.append(notes)
    removed: list[Path] = []
    for path in victims:
        if os.path.normcase(os.path.abspath(path)) not in kept and path.is_file():
            remove_quietly(path)
            removed.append(path)
    return removed


def _notes_are_for(notes: Path, base: str) -> bool:
    try:
        text = notes.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return f'"{base}" was split into' in text


def _check_against_manifest(parts: PartSet, manifest: Manifest) -> None:
    on_disk = {p.name: p for p in parts.parts}
    for listed in manifest.parts:
        found = on_disk.get(listed.name)
        if found is None:
            raise CorruptArchive(
                f"Part {listed.name} is missing (listed in the manifest)",
                hint="Put all parts of the file in the same folder.",
                code="part-missing",
            )
        size = os.stat(long_path(found)).st_size
        if size != listed.size:
            raise CorruptArchive(
                f"Part {listed.name} has the wrong size ({size} bytes, expected {listed.size})",
                hint="Download or copy the part again.",
                code="part-size",
            )
    extra = set(on_disk) - {p.name for p in manifest.parts}
    if extra:
        raise CorruptArchive(
            f"Part {sorted(extra)[0]} is not listed in the manifest",
            hint="Remove stray files or delete the manifest.",
            code="part-stray",
        )


# --- split ----------------------------------------------------------------------------------


class _HashingReader:
    """Reads at most ``limit`` bytes from ``fh`` and feeds every chunk to the hashes."""

    def __init__(self, fh: BinaryIO, limit: int, *hashes: Any) -> None:
        self._fh = fh
        self._left = limit
        self._hashes = hashes

    def read(self, n: int = -1) -> bytes:
        want = self._left if n < 0 else min(n, self._left)
        if want <= 0:
            return b""
        data = self._fh.read(want)
        self._left -= len(data)
        for h in self._hashes:
            h.update(data)
        return data


def _ensure_space(directory: Path, needed: int, what: str) -> None:
    if free_space(directory) < needed:
        raise IOFailure(
            f"Not enough free disk space to {what}: {needed} bytes are needed",
            hint="Free some space or write to another drive.",
            code="disk-full",
        )


def write_manifest(directory: Path, base: str, manifest: Manifest) -> Path:
    """Write ``base.parts.json`` atomically."""
    final = directory / manifest_name(base)
    atomic = AtomicFile(final, ".partial")
    try:
        stream = atomic.open()
        stream.write(json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False).encode("utf-8"))
        atomic.commit()
    except OSError as exc:
        atomic.discard()
        raise translate_oserror(exc, f"cannot write {final.name}") from exc
    except BaseException:
        atomic.discard()
        raise
    return final


def split_file(
    src: Path | str,
    part_size: int,
    ctx: JobContext | None = None,
    *,
    keep_source: bool = True,
    name: str | None = None,
    out_dir: Path | str | None = None,
    manifest: bool = True,
    check_space: bool = True,
) -> list[Path]:
    """Cut ``src`` into ``name.001``, ``name.002``, ... of ``part_size`` bytes each.

    Parts are written as ``.zwpart`` files and renamed together at the end, so a cancelled
    or failed split leaves nothing behind. A file smaller than one part (or an empty file)
    gives a single part. ``name`` and ``out_dir`` default to the source's own. A checksum
    manifest is written next to the parts unless ``manifest`` is False. With
    ``keep_source=False`` the source is removed after everything succeeded.
    """
    ctx = ctx or JobContext()
    if part_size < 1:
        raise UsageError(f"Part size must be at least 1 byte, got {part_size}")
    source = Path(os.path.abspath(src))
    try:
        size = os.stat(long_path(source)).st_size
    except OSError as exc:
        raise UsageError(f"Cannot read {source}: {exc.strerror or exc}") from exc
    base = name or source.name
    directory = Path(os.path.abspath(out_dir)) if out_dir is not None else source.parent
    count = max(1, -(-size // part_size))
    digits = part_digits(count)
    finals = [directory / part_name(base, i, digits) for i in range(1, count + 1)]
    temps = [f.with_name(f.name + TEMP_SUFFIX) for f in finals]
    if check_space:
        _ensure_space(directory, size, "split the file")

    ctx.begin("split", bytes_total=size, entries_total=count)
    whole = hashlib.sha256()
    listed: list[ManifestPart] = []
    written_temps: list[Path] = []
    renamed: list[Path] = []
    try:
        with long_path(source).open("rb") as fin:
            for index, (final, temp) in enumerate(zip(finals, temps, strict=True)):
                ctx.raise_if_cancelled()
                ctx.set_entry(final.name)
                limit = min(part_size, size - index * part_size)
                digest = hashlib.sha256()
                written_temps.append(temp)
                try:
                    with long_path(temp).open("wb") as fout:
                        copy_stream(
                            cast(BinaryIO, _HashingReader(fin, limit, digest, whole)),
                            fout,
                            ctx,
                        )
                except OSError as exc:
                    raise translate_oserror(exc, f"cannot write {final.name}") from exc
                written = os.stat(long_path(temp)).st_size
                if written != limit:
                    raise IOFailure(f"{source.name} changed while it was being split")
                listed.append(ManifestPart(final.name, written, digest.hexdigest()))
                ctx.entry_done()
        ctx.raise_if_cancelled()
        stale = _stale_parts(directory, base, {f.name for f in finals}, source)
        for final, temp in zip(finals, temps, strict=True):
            clear_readonly(final)
            try:
                os.replace(long_path(temp), long_path(final))
            except OSError as exc:
                raise translate_oserror(exc, f"cannot write {final.name}") from exc
            renamed.append(final)
        for old in stale:
            remove_quietly(old)
        if manifest:
            write_manifest(
                directory,
                base,
                Manifest(base, size, whole.hexdigest(), part_size, tuple(listed)),
            )
    except BaseException:
        for leftover in (*renamed, *written_temps):
            remove_quietly(leftover)
        raise
    ctx.finish()
    if not keep_source:
        remove_quietly(source)
    return finals


def _stale_parts(directory: Path, base: str, keep: set[str], source: Path) -> list[Path]:
    """Parts of an older split of the same file that the new set does not overwrite."""
    stale: list[Path] = []
    folded = os.path.normcase(base)
    for entry in os.listdir(directory):
        m = _PART_RE.match(entry)
        if m is None or os.path.normcase(m.group("base")) != folded or entry in keep:
            continue
        path = directory / entry
        if os.path.normcase(os.path.abspath(path)) != os.path.normcase(str(source)):
            stale.append(path)
    return stale


# --- join and verify ------------------------------------------------------------------------


def _bad_part(listed: ManifestPart) -> CorruptArchive:
    return CorruptArchive(
        f"Part {listed.name} is corrupted (its checksum does not match the manifest)",
        hint="Download or copy that part again.",
        code="part-corrupt",
    )


def _load_manifest(parts: PartSet) -> Manifest | None:
    if parts.manifest_path is not None and parts.manifest is None:
        return read_manifest(parts.manifest_path)  # raises: a damaged manifest is reported
    return parts.manifest


def join_parts(
    any_part: Path | str,
    dest: Path | str | None = None,
    ctx: JobContext | None = None,
    *,
    check_space: bool = True,
) -> Path:
    """Concatenate the parts into ``dest`` (a file, or a folder; default: next to the parts).

    When the manifest is present every part and the whole file are verified; a mismatch raises
    ``CorruptArchive`` naming the bad part and nothing is written. Output is atomic.
    """
    ctx = ctx or JobContext()
    parts = find_parts(any_part)
    manifest = _load_manifest(parts)
    base = manifest.original_name if manifest is not None else parts.base
    if dest is None:
        target = parts.directory / base
    else:
        target = Path(os.path.abspath(dest))
        if target.is_dir():
            target = target / base
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise translate_oserror(exc, f"cannot create {target.parent}") from exc
    if any(os.path.normcase(str(target)) == os.path.normcase(str(p)) for p in parts.parts):
        raise UsageError("The output file would overwrite one of the parts")
    total = parts.total_size
    if check_space:
        _ensure_space(target.parent, total, "join the parts")

    ctx.begin("join", bytes_total=total, entries_total=len(parts.parts))
    atomic = AtomicFile(target, ".partial")
    whole = hashlib.sha256()
    try:
        out = atomic.open()
        for index, part in enumerate(parts.parts):
            ctx.raise_if_cancelled()
            ctx.set_entry(part.name)
            digest = hashlib.sha256()
            try:
                with long_path(part).open("rb") as fin:
                    reader = _HashingReader(fin, os.stat(long_path(part)).st_size, digest, whole)
                    copy_stream(cast(BinaryIO, reader), out, ctx)
            except OSError as exc:
                raise translate_oserror(exc, f"cannot read {part.name}") from exc
            if manifest is not None and digest.hexdigest() != manifest.parts[index].sha256:
                raise _bad_part(manifest.parts[index])
            ctx.entry_done()
        if manifest is not None and whole.hexdigest() != manifest.sha256:
            raise CorruptArchive(
                "The joined file does not match the manifest checksum",
                hint="Parts may be from different versions of the file.",
                code="part-corrupt",
            )
        ctx.raise_if_cancelled()
        atomic.commit()
    except BaseException:
        atomic.discard()
        raise
    ctx.finish()
    return target


def verify_parts(any_part: Path | str, ctx: JobContext | None = None) -> bool:
    """Check every part against the manifest. Returns False when there is no manifest."""
    ctx = ctx or JobContext()
    parts = find_parts(any_part)
    manifest = _load_manifest(parts)
    if manifest is None:
        return False
    ctx.begin("join", bytes_total=parts.total_size, entries_total=len(parts.parts))
    for listed, part in zip(manifest.parts, parts.parts, strict=True):
        ctx.set_entry(part.name)
        digest = hashlib.sha256()
        with long_path(part).open("rb") as fin:
            drain_stream(cast(BinaryIO, _HashingReader(fin, listed.size, digest)), ctx)
        if digest.hexdigest() != listed.sha256:
            raise _bad_part(listed)
        ctx.entry_done()
    return True


def materialise_parts(
    any_part: Path | str, ctx: JobContext | None = None
) -> tuple[Path, Callable[[], None]]:
    """Join the parts into a temporary file named like the original (for readers that cannot
    take a file object). Returns the file and a function that deletes it."""
    parts = find_parts(any_part)
    folder = Path(tempfile.mkdtemp(prefix="zipwise-join-"))

    def cleanup() -> None:
        shutil.rmtree(folder, ignore_errors=True)

    try:
        joined = join_parts(parts.first, folder / parts.base, ctx, check_space=False)
    except BaseException:
        cleanup()
        raise
    return joined, cleanup


# --- recipient notes ------------------------------------------------------------------------


def recipient_notes(
    parts: Sequence[Path | str],
    original_name: str | None = None,
    *,
    directory: Path | str | None = None,
) -> Path:
    """Write ``HOW-TO-OPEN.txt`` (English, then Arabic) next to the parts and return its path."""
    names = [Path(p).name for p in parts]
    if not names:
        raise UsageError("No parts to describe")
    match = _PART_RE.match(names[0])
    base = original_name or (match.group("base") if match else names[0])
    folder = Path(directory) if directory is not None else Path(parts[0]).parent
    digits = len(match.group("num")) if match else _MIN_DIGITS
    first, last, count = names[0], names[-1], len(names)
    wild_win = f"{base}." + "?" * digits
    wild_nix = f"{base}." + "[0-9]" * digits
    text = "\n".join(
        [
            "HOW TO OPEN THESE FILES",
            "=======================",
            "",
            f'"{base}" was split into {count} part(s) so that it is easier to send:',
            f"    {first}" + (f"  ...  {last}" if count > 1 else ""),
            "Keep ALL the parts in the same folder. Do not rename them.",
            "",
            "Option 1 - 7-Zip or WinRAR (free, Windows)",
            f'    Right-click "{first}" and choose Open archive (7-Zip: Open archive).',
            "    The other parts are picked up automatically.",
            "",
            "Option 2 - Zipwise",
            f'    zipwise join "{first}"',
            f'    (or open "{first}" in Zipwise directly)',
            "",
            "Option 3 - no extra program needed",
            "    Windows (Command Prompt, in this folder):",
            f'        copy /b "{wild_win}" "{base}"',
            "    Linux / macOS (terminal, in this folder):",
            f'        cat "{wild_nix}" > "{base}"',
            "",
            f'The result is the file "{base}".',
            "",
            "",
            "طريقة فتح هذه الملفات",
            "====================",
            "",
            f"تم تقسيم الملف «{base}» إلى {count} جزء لتسهيل إرساله:",
            f"    {first}" + (f"  ...  {last}" if count > 1 else ""),
            "احتفظ بجميع الأجزاء في نفس المجلد ولا تغيّر أسماءها.",
            "",
            "الطريقة الأولى - برنامج 7-Zip أو WinRAR",
            f"    افتح الجزء الأول «{first}» بالبرنامج، وسيتم التقاط بقية الأجزاء تلقائيا.",
            "",
            "الطريقة الثانية - برنامج Zipwise",
            f'    zipwise join "{first}"',
            "",
            "الطريقة الثالثة - بدون أي برنامج إضافي",
            "    ويندوز (موجه الأوامر داخل هذا المجلد):",
            f'        copy /b "{wild_win}" "{base}"',
            "    لينكس / ماك (الطرفية داخل هذا المجلد):",
            f'        cat "{wild_nix}" > "{base}"',
            "",
            f"الملف الناتج هو «{base}».",
            "",
        ]
    )
    target = folder / NOTES_NAME
    try:
        target.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise translate_oserror(exc, f"cannot write {NOTES_NAME}") from exc
    return target


# --- reading parts as one file --------------------------------------------------------------


class MultiPartFile(io.RawIOBase):
    """Read-only, seekable binary file over an ordered list of parts.

    Lets ``list`` / ``test`` / ``extract`` work on ``name.zip.001`` without joining it. Only
    the part under the read position is kept open.
    """

    def __init__(self, parts: Sequence[Path | str]) -> None:
        super().__init__()
        if not parts:
            raise UsageError("A multi-part file needs at least one part")
        self._paths = [Path(p) for p in parts]
        try:
            sizes = [os.stat(long_path(p)).st_size for p in self._paths]
        except OSError as exc:
            raise translate_oserror(exc, "cannot read the parts") from exc
        self._starts = [0]
        for size in sizes:
            self._starts.append(self._starts[-1] + size)
        self._size = self._starts[-1]
        self._pos = 0
        self._current = -1
        self._fh: BinaryIO | None = None
        self.name = str(self._paths[0])

    @property
    def size(self) -> int:
        return self._size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        self._check_open()
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._check_open()
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError(f"invalid whence ({whence})")
        if target < 0:
            raise ValueError("negative seek position")
        self._pos = target
        return target

    def _check_open(self) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")

    def _open_part(self, index: int) -> BinaryIO:
        if self._current != index or self._fh is None:
            self._drop()
            try:
                self._fh = long_path(self._paths[index]).open("rb")
            except OSError as exc:
                raise translate_oserror(exc, f"cannot read {self._paths[index].name}") from exc
            self._current = index
        return self._fh

    def _drop(self) -> None:
        if self._fh is not None:
            self._fh.close()
        self._fh = None
        self._current = -1

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        self._check_open()
        view = memoryview(buffer).cast("B")
        filled = 0
        while filled < len(view) and self._pos < self._size:
            index = bisect.bisect_right(self._starts, self._pos) - 1
            into_part = self._pos - self._starts[index]
            room = self._starts[index + 1] - self._pos
            fh = self._open_part(index)
            fh.seek(into_part)
            data = fh.read(min(room, len(view) - filled, CHUNK_SIZE))
            if not data:
                raise IOFailure(f"{self._paths[index].name} is shorter than it was")
            view[filled : filled + len(data)] = data
            filled += len(data)
            self._pos += len(data)
        return filled

    def readall(self) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = self.read(CHUNK_SIZE)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def close(self) -> None:
        if not self.closed:
            self._drop()
        super().close()

    def __enter__(self) -> MultiPartFile:
        self._check_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
