"""Declarative small test trees. Everything is built at test time; nothing is stored in the repo."""

from __future__ import annotations

import hashlib
import os
import random
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Link:
    """A symlink in a tree spec."""

    target: str


Spec = Mapping[str, "bytes | str | Link | None"]

BASE_MTIME = 1_700_000_000


def random_bytes(seed: int, size: int) -> bytes:
    return random.Random(seed).randbytes(size)


def text_bytes(size: int, seed: int = 1) -> bytes:
    words = ["alpha", "beta", "gamma", "delta", "zip", "wise", "archive", "safe"]
    rng = random.Random(seed)
    out: list[str] = []
    length = 0
    while length < size:
        w = rng.choice(words)
        out.append(w)
        length += len(w) + 1
    return " ".join(out).encode()[:size]


SHAPES: dict[str, Spec] = {
    "simple": {"a.txt": b"hello\n", "b.txt": b"world\n"},
    "nested": {
        "top.txt": b"top",
        "d1/one.txt": text_bytes(2000, 2),
        "d1/d2/two.txt": text_bytes(3000, 3),
        "d1/d2/d3/three.bin": random_bytes(3, 4096),
    },
    "unicode": {
        "résumé.txt": b"accents",
        "ملف عربي.txt": "مرحبا".encode(),
        "文件/数据.txt": b"cjk",
        "папка/файл.txt": b"cyrillic",
    },
    "empty_dirs": {
        "empty/": None,
        "parent/child/": None,
        "parent/file.txt": b"x",
        "blank.txt": b"",
    },
    "mixed": {
        "empty.dat": b"",
        "text.txt": text_bytes(100_000, 5),
        "random.bin": random_bytes(7, 50_000),
        "photo.jpg": random_bytes(8, 20_000),
        "sub/archive.zip": random_bytes(9, 10_000),
        "sub/deep/notes.md": text_bytes(500, 10),
        "void/": None,
    },
    "many_small": {f"f{i % 5}/file{i:03}.txt": f"item {i}\n".encode() * 3 for i in range(30)},
}


def make_tree(root: Path, spec: Spec) -> Path:
    """Create ``spec`` below ``root``. A trailing ``/`` marks an (empty) directory.

    File and directory mtimes are set to distinct, fixed values (directories last).
    """
    root.mkdir(parents=True, exist_ok=True)
    for name, content in spec.items():
        path = root / name.rstrip("/")
        if isinstance(content, Link):
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(content.target, path)
            continue
        if name.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode() if isinstance(content, str) else (content or b"")
        path.write_bytes(data)
    for i, path in enumerate(sorted(p for p in root.rglob("*") if not p.is_symlink())):
        stamp = BASE_MTIME + i * 37
        os.utime(path, (stamp, stamp))
    os.utime(root, (BASE_MTIME - 5, BASE_MTIME - 5))
    return root


def snapshot(root: Path) -> dict[str, tuple]:
    """Relative path -> (kind, sha256 or link target, mtime, mode)."""
    result: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            result[rel] = ("link", os.readlink(path).replace("\\", "/"), None, None)
        elif stat.S_ISDIR(st.st_mode):
            result[rel] = ("dir", None, st.st_mtime, stat.S_IMODE(st.st_mode))
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result[rel] = ("file", digest, st.st_mtime, stat.S_IMODE(st.st_mode))
    return result
