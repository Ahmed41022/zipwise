from __future__ import annotations

import bz2
import gzip
import io
import lzma
import tarfile
import zipfile
from pathlib import Path

import pytest
import zstandard

from zipwise.detect import Detection, detect, detect_format, looks_like_tar_header, sniff
from zipwise.errors import UnsupportedFormat, UsageError
from zipwise.model import FormatId


def make_tar_bytes(name: str = "hello.txt", payload: bytes = b"hello") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def make_v7_tar_bytes() -> bytes:
    block = bytearray(make_tar_bytes()[:512])
    block[257:265] = b"\0" * 8  # no ustar magic: the oldest tar dialect
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return bytes(block) + b"hello" + b"\0" * (512 - 5) + b"\0" * 1024


def make_zip_bytes(empty: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if not empty:
            zf.writestr("a.txt", "a")
    return buf.getvalue()


TAR = make_tar_bytes(payload=b"x" * 3000)
SAMPLES: dict[str, tuple[bytes, FormatId]] = {
    "zip": (make_zip_bytes(), FormatId.ZIP),
    "zip-empty": (make_zip_bytes(empty=True), FormatId.ZIP),
    "zip-spanned-marker": (b"PK\x07\x08" + b"\0" * 40, FormatId.ZIP),
    "7z": (b"7z\xbc\xaf\x27\x1c\x00\x04" + b"\0" * 40, FormatId.SEVENZ),
    "rar4": (b"Rar!\x1a\x07\x00" + b"\0" * 40, FormatId.RAR),
    "rar5": (b"Rar!\x1a\x07\x01\x00" + b"\0" * 40, FormatId.RAR),
    "gz": (gzip.compress(b"plain text " * 50), FormatId.GZ),
    "bz2": (bz2.compress(b"plain text " * 50), FormatId.BZ2),
    "xz": (lzma.compress(b"plain text " * 50), FormatId.XZ),
    "zst": (zstandard.ZstdCompressor().compress(b"plain text " * 50), FormatId.ZST),
    "tar": (TAR, FormatId.TAR),
    "tar-v7": (make_v7_tar_bytes(), FormatId.TAR),
    "tar.gz": (gzip.compress(TAR), FormatId.TAR_GZ),
    "tar.bz2": (bz2.compress(TAR), FormatId.TAR_BZ2),
    "tar.xz": (lzma.compress(TAR), FormatId.TAR_XZ),
    "tar.zst": (zstandard.ZstdCompressor().compress(TAR), FormatId.TAR_ZST),
}


@pytest.mark.parametrize("key", sorted(SAMPLES))
def test_detection_table(tmp_path: Path, key: str) -> None:
    data, expected = SAMPLES[key]
    path = tmp_path / "sample.bin"  # the extension must not matter
    path.write_bytes(data)
    assert detect(path) == Detection(expected, multipart=False)
    assert detect_format(path) is expected
    assert sniff(data[:4096]) is expected


@pytest.mark.parametrize("key", ["zip", "7z", "tar.gz", "tar.zst", "gz"])
@pytest.mark.parametrize("suffix", [".001", ".002", ".999"])
def test_multipart_volumes_are_flagged(tmp_path: Path, key: str, suffix: str) -> None:
    data, expected = SAMPLES[key]
    path = tmp_path / f"archive.bin{suffix}"
    path.write_bytes(data)
    detection = detect(path)
    assert detection.multipart is True
    assert detection.format is expected


@pytest.mark.parametrize("name", ["a.zip", "a.zip.01", "a.zip.0001", "a.zip.abc", "a.001.zip"])
def test_non_volume_names_are_not_multipart(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(SAMPLES["zip"][0])
    assert detect(path).multipart is False


@pytest.mark.parametrize(
    "data",
    [b"", b"just some text\n", b"\x00" * 600, b"PK", b"MZ\x90\x00" + b"\0" * 100, b"\x1f"],
    ids=["empty", "text", "zeros", "short-pk", "exe", "one-byte"],
)
def test_unknown_content_is_unsupported(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "x.zip"
    path.write_bytes(data)
    with pytest.raises(UnsupportedFormat):
        detect(path)


def test_extension_lies_content_wins(tmp_path: Path) -> None:
    path = tmp_path / "photo.jpg"
    path.write_bytes(SAMPLES["zip"][0])
    assert detect_format(path) is FormatId.ZIP


def test_zip_named_gz_is_still_zip(tmp_path: Path) -> None:
    path = tmp_path / "archive.tar.gz"
    path.write_bytes(SAMPLES["zip"][0])
    assert detect_format(path) is FormatId.ZIP


def test_missing_file_is_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        detect(tmp_path / "nope.zip")


def test_directory_is_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        detect(tmp_path)


def test_corrupt_gzip_stream_is_reported_as_plain_gz() -> None:
    data = gzip.compress(TAR)[:10] + b"\xff" * 30
    assert sniff(data) is FormatId.GZ


def test_truncated_compressed_tar_header_still_detected() -> None:
    assert sniff(gzip.compress(TAR)[:60]) in (FormatId.TAR_GZ, FormatId.GZ)


def test_tar_header_checksum_must_match() -> None:
    block = bytearray(make_v7_tar_bytes()[:512])
    assert looks_like_tar_header(bytes(block))
    block[0] ^= 0xFF
    assert not looks_like_tar_header(bytes(block))


def test_short_block_is_not_tar() -> None:
    assert not looks_like_tar_header(b"ustar" * 10)
