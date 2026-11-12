"""Locating the real 7-Zip and RAR programs, running them, and building the shared test tree."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from helpers.trees import Spec, random_bytes, text_bytes
from zipwise.backends.rar import find_tool


def _find_7zip() -> str | None:
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMW6432")):
            candidate = Path(root or r"C:\Program Files") / "7-Zip" / "7z.exe"
            if candidate.is_file():
                return str(candidate)
    return None


SEVENZIP = _find_7zip()
RAR = shutil.which("rar")
UNRAR_TOOL = find_tool()


def _rar_writes_rar4() -> bool:
    """RAR 7 dropped ``-ma4``; older versions still create RAR 4 archives."""
    if RAR is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "f.txt").write_text("x")
        done = subprocess.run(
            [RAR, "a", "-idq", "-ma4", "f.rar", "f.txt"],
            cwd=tmp,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=60,
        )
    return done.returncode == 0


# Plain markers; ``conftest.py`` turns them into skips when the program is missing.
needs_7zip = pytest.mark.needs_7zip
needs_rar = pytest.mark.needs_rar
needs_unrar = pytest.mark.needs_unrar
needs_rar4 = pytest.mark.skipif(
    not _rar_writes_rar4(), reason="this rar program cannot create RAR 4 archives (-ma4)"
)
needs_crypto = pytest.mark.skipif(
    importlib.util.find_spec("cryptography") is None,
    reason="rarfile needs the 'cryptography' package for encrypted RAR headers",
)

MISSING_TOOL_REASONS: dict[str, tuple[bool, str]] = {
    "needs_7zip": (SEVENZIP is None, "7-Zip (7z, 7zz or 7za) not found"),
    "needs_rar": (RAR is None, "the rar program (RAR creation) not found"),
    "needs_unrar": (UNRAR_TOOL is None, "no unrar-compatible tool on PATH"),
}

PASSWORD = "s3cret-päss"
ASCII_PASSWORD = "s3cret-pass"  # ZipCrypto only takes ASCII


class ToolFailed(AssertionError):
    pass


def run_tool(exe: str, args: list[str], cwd: Path) -> str:
    """Run an external program; any non-zero exit is a test setup failure."""
    done = subprocess.run(
        [exe, *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=120,
    )
    out = done.stdout.decode("utf-8", errors="replace") + done.stderr.decode(
        "utf-8", errors="replace"
    )
    if done.returncode != 0:
        raise ToolFailed(f"{Path(exe).name} {' '.join(args)} exited with {done.returncode}\n{out}")
    return out


def sevenzip(*args: str, cwd: Path) -> str:
    assert SEVENZIP is not None
    return run_tool(SEVENZIP, ["-bd", "-y", *args], cwd)


def rar(*args: str, cwd: Path) -> str:
    assert RAR is not None
    return run_tool(RAR, ["-idq", "-y", *args], cwd)


def exe_like(size: int = 200_000) -> bytes:
    """Bytes that resemble x86 code: many CALL/JMP opcodes with relative offsets, so that the
    BCJ filters have something to convert."""
    out = bytearray(b"MZ\x90\x00")
    rng_bytes = random_bytes(11, size)
    i = 0
    while len(out) < size:
        chunk = rng_bytes[i : i + 3]
        i += 3
        out += b"\xe8" + chunk + b"\x00" + b"\x55\x8b\xec\x83"
    return bytes(out[:size])


def mixed_big() -> bytes:
    """Over 1 MiB: text, random bytes, a zero run, text again."""
    return (
        text_bytes(500_000, 21)
        + random_bytes(22, 450_000)
        + bytes(120_000)
        + text_bytes(60_000, 23)
    )


TREE_NAME = "tree"

INTEROP_SPEC: Spec = {
    "top.txt": b"top level\n",
    "nested/a/b/c/deep.txt": text_bytes(3000, 31),
    "nested/a/other.bin": random_bytes(32, 5000),
    "unicode/résumé.txt": b"accents",
    "unicode/ملف عربي.txt": "مرحبا".encode(),
    "unicode/文件/数据.txt": b"cjk",
    "unicode/папка/файл.txt": b"cyrillic",
    "empty/": None,
    "parent/child/": None,
    "blank.txt": b"",
    "big/mixed.bin": mixed_big(),
    "bin/prog.exe": exe_like(),
}

SINGLE_NAME = "single.txt"
