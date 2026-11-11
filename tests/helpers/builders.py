"""Low-level ZIP writer so that names, flags and headers can be anything (malicious or legacy)."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

S_IFLNK = 0o120000
S_IFREG = 0o100000
S_IFIFO = 0o010000
S_IFCHR = 0o020000


@dataclass
class RawEntry:
    name: str | bytes
    data: bytes = b""
    method: int = 8
    flags: int | None = None  # None: UTF-8 flag for str names, no flag for bytes names
    external_attr: int = (S_IFREG | 0o644) << 16
    create_system: int = 3
    declared_size: int | None = None
    declared_crc: int | None = None
    date_time: tuple[int, int, int, int, int, int] = (2024, 3, 5, 12, 30, 40)
    extra: bytes = b""
    compressed: bytes | None = None
    level: int = 9


def _deflate(data: bytes, level: int) -> bytes:
    comp = zlib.compressobj(level, zlib.DEFLATED, -15)
    return comp.compress(data) + comp.flush()


def _dos(dt: tuple[int, int, int, int, int, int]) -> tuple[int, int]:
    return (dt[3] << 11) | (dt[4] << 5) | (dt[5] // 2), ((dt[0] - 1980) << 9) | (dt[1] << 5) | dt[2]


def zipcrypto_encrypt(data: bytes, password: bytes, crc: int) -> bytes:
    """Traditional PKWARE encryption (Zipwise only reads it, so tests need a producer)."""
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
        table.append(c)
    keys = [0x12345678, 0x23456789, 0x34567890]

    def update(b: int) -> None:
        keys[0] = table[(keys[0] ^ b) & 0xFF] ^ (keys[0] >> 8)
        keys[1] = ((keys[1] + (keys[0] & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        keys[2] = table[(keys[2] ^ (keys[1] >> 24)) & 0xFF] ^ (keys[2] >> 8)

    def stream_byte() -> int:
        t = (keys[2] | 2) & 0xFFFF
        return ((t * (t ^ 1)) >> 8) & 0xFF

    for b in password:
        update(b)
    header = bytes(range(11)) + bytes([(crc >> 24) & 0xFF])
    out = bytearray()
    for plain in header + data:
        out.append(plain ^ stream_byte())
        update(plain)
    return bytes(out)


def write_raw_zip(path: Path, entries: list[RawEntry], comment: bytes = b"") -> Path:
    body = bytearray()
    central = bytearray()
    for e in entries:
        raw = e.name.encode("utf-8") if isinstance(e.name, str) else e.name
        flags = e.flags if e.flags is not None else (0x800 if isinstance(e.name, str) else 0)
        payload = e.compressed
        if payload is None:
            payload = _deflate(e.data, e.level) if e.method == 8 else e.data
        crc = e.declared_crc if e.declared_crc is not None else zlib.crc32(e.data)
        usize = e.declared_size if e.declared_size is not None else len(e.data)
        dtime, ddate = _dos(e.date_time)
        offset = len(body)
        body += struct.pack(
            "<4sHHHHHIIIHH",
            b"PK\x03\x04",
            20,
            flags,
            e.method,
            dtime,
            ddate,
            crc,
            len(payload),
            usize,
            len(raw),
            len(e.extra),
        )
        body += raw + e.extra + payload
        central += struct.pack(
            "<4sHHHHHHIIIHHHHHII",
            b"PK\x01\x02",
            (e.create_system << 8) | 20,
            20,
            flags,
            e.method,
            dtime,
            ddate,
            crc,
            len(payload),
            usize,
            len(raw),
            len(e.extra),
            0,
            0,
            0,
            e.external_attr,
            offset,
        )
        central += raw + e.extra
    end = struct.pack(
        "<4sHHHHIIH",
        b"PK\x05\x06",
        0,
        0,
        len(entries),
        len(entries),
        len(central),
        len(body),
        len(comment),
    )
    path.write_bytes(bytes(body) + bytes(central) + end + comment)
    return path


def file_entry(name: str | bytes, data: bytes = b"data", **kw: object) -> RawEntry:
    return RawEntry(name, data, **kw)  # type: ignore[arg-type]


def dir_entry(name: str) -> RawEntry:
    return RawEntry(name.rstrip("/") + "/", b"", method=0, external_attr=(0o040755 << 16) | 0x10)


def symlink_entry(name: str, target: str) -> RawEntry:
    return RawEntry(name, target.encode(), method=0, external_attr=(S_IFLNK | 0o777) << 16)


def special_entry(name: str, kind: int = S_IFIFO) -> RawEntry:
    return RawEntry(name, b"", method=0, external_attr=(kind | 0o644) << 16)


def unicode_path_extra(raw: bytes, text: str) -> bytes:
    payload = b"\x01" + struct.pack("<I", zlib.crc32(raw)) + text.encode("utf-8")
    return struct.pack("<HH", 0x7075, len(payload)) + payload


# --- TAR, 7z and RAR builders (added for the TAR family, 7z and RAR backends) ---------------

import bz2  # noqa: E402
import gzip  # noqa: E402
import io  # noqa: E402
import lzma  # noqa: E402
import tarfile  # noqa: E402

CODECS = ("none", "gz", "bz2", "xz", "zst")


@dataclass
class RawTarMember:
    """One tar member with a free-form name and type (malicious or special archives)."""

    name: str
    data: bytes = b""
    type: bytes = tarfile.REGTYPE
    linkname: str = ""
    mode: int = 0o644
    mtime: int = 1_700_000_000
    size: int | None = None  # None: len(data)


def compress_bytes(raw: bytes, codec: str) -> bytes:
    """Compress ``raw`` with one of ``CODECS``."""
    if codec == "none":
        return raw
    if codec == "gz":
        return gzip.compress(raw, 6, mtime=0)
    if codec == "bz2":
        return bz2.compress(raw)
    if codec == "xz":
        return lzma.compress(raw)
    if codec == "zst":
        import zstandard

        return zstandard.ZstdCompressor(write_checksum=True).compress(raw)
    raise ValueError(codec)


def tar_bytes(members: list[RawTarMember], *, fmt: int = tarfile.GNU_FORMAT) -> bytes:
    buf = io.BytesIO()
    with tarfile.TarFile(fileobj=buf, mode="w", format=fmt) as tar:
        for m in members:
            info = tarfile.TarInfo(m.name)
            info.type = m.type
            info.mode = m.mode
            info.mtime = m.mtime
            info.linkname = m.linkname
            data = m.data if m.type in (tarfile.REGTYPE, tarfile.AREGTYPE) else b""
            info.size = len(data) if m.size is None else m.size
            if data or m.type == tarfile.REGTYPE:
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return buf.getvalue()


def write_raw_tar(
    path: Path, members: list[RawTarMember], codec: str = "none", *, fmt: int = tarfile.GNU_FORMAT
) -> Path:
    path.write_bytes(compress_bytes(tar_bytes(members, fmt=fmt), codec))
    return path


def tar_file(name: str, data: bytes = b"data") -> RawTarMember:
    return RawTarMember(name, data)


def tar_dir(name: str) -> RawTarMember:
    return RawTarMember(name, type=tarfile.DIRTYPE, mode=0o755)


def tar_symlink(name: str, target: str) -> RawTarMember:
    return RawTarMember(name, type=tarfile.SYMTYPE, linkname=target, mode=0o777)


def tar_hardlink(name: str, target: str) -> RawTarMember:
    return RawTarMember(name, type=tarfile.LNKTYPE, linkname=target)


def tar_special(name: str, kind: bytes = tarfile.FIFOTYPE) -> RawTarMember:
    return RawTarMember(name, type=kind)


def write_raw_7z(
    path: Path,
    items: list[tuple[str, str, bytes | str]],
    *,
    password: str | None = None,
    encrypt_names: bool = False,
) -> Path:
    """Write a 7z whose names are not validated (py7zr's public writer refuses ``..``).

    Each item is ``("file", name, data)``, ``("symlink", name, target)`` or ``("dir", name, b"")``.
    """
    from zipwise.backends.sevenz import SevenZipWriter
    from zipwise.model import CreateOptions, FormatId

    options = CreateOptions(
        format=FormatId.SEVENZ, level="fast", password=password, encrypt_names=encrypt_names
    )
    writer = SevenZipWriter(path, options)
    stamp = 1_700_000_000.0
    for kind, name, payload in items:
        if kind == "dir":
            writer.add_dir(name, stamp, 0o755)
            continue
        data = payload.encode() if isinstance(payload, str) else payload
        writer._sz._writef(io.BytesIO(data), name)  # type: ignore[arg-type]
        info = writer._sz.header.files_info.files[-1]
        from zipwise.backends.sevenz import MemberType, _attributes

        member = MemberType.SYMLINK if kind == "symlink" else MemberType.FILE
        writer._stamp(info, stamp, _attributes(member, 0o777 if kind == "symlink" else 0o644))
    writer.close()
    return path


def _rar4_block(htype: int, flags: int, body: bytes) -> bytes:
    header = struct.pack("<BHH", htype, flags, 7 + len(body)) + body
    return struct.pack("<H", zlib.crc32(header) & 0xFFFF) + header


def _rar4_file(name: str, data: bytes, *, is_dir: bool = False) -> bytes:
    raw = name.encode("utf-8")
    stamp = ((2024 - 1980) << 25) | (3 << 21) | (5 << 16) | (12 << 11) | (30 << 5) | 20
    body = struct.pack(
        "<IIBIIBBHI",
        len(data),
        len(data),
        2,  # host OS: Windows
        zlib.crc32(data) & 0xFFFFFFFF,
        stamp,
        20,
        0x30,  # method: stored
        len(raw),
        0x10 if is_dir else 0x20,
    )
    flags = 0x8000 | (0xE0 if is_dir else 0)
    return _rar4_block(0x74, flags, body + raw) + data


def write_stored_rar4(path: Path, files: dict[str, bytes | None]) -> Path:
    """A minimal RAR 4.x archive that stores its files uncompressed. A ``None`` value or a name
    ending in ``/`` makes a directory entry."""
    out = bytearray(b"Rar!\x1a\x07\x00")
    out += _rar4_block(0x73, 0, struct.pack("<HI", 0, 0))
    for name, data in files.items():
        is_dir = data is None or name.endswith("/")
        out += _rar4_file(name.rstrip("/"), b"" if data is None else data, is_dir=is_dir)
    out += _rar4_block(0x7B, 0x4000, b"")
    path.write_bytes(bytes(out))
    return path
