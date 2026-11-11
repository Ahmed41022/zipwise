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
