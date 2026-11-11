from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers.roundtrip import assert_roundtrip
from helpers.trees import SHAPES, Link, make_tree, snapshot
from zipwise.model import CreateOptions, ExtractOptions, FormatId
from zipwise.ops import create_archive, extract_archive

LEVELS = ["fast", "normal", "best"]


@pytest.mark.parametrize("password", [None, "s3cret éم"], ids=["plain", "aes"])
@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_roundtrip_shapes_levels_password(
    tmp_path: Path, shape: str, level: str, password: str | None
) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES[shape])
    options = CreateOptions(level=level, password=password)
    assert_roundtrip(tmp_path, tree, options)


@pytest.mark.parametrize("level", [0, 1, 5, 9])
def test_roundtrip_integer_levels(tmp_path: Path, level: int) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["mixed"])
    assert_roundtrip(tmp_path, tree, CreateOptions(level=level))


@pytest.mark.parametrize("smart", [True, False])
def test_roundtrip_smart_store_toggle(tmp_path: Path, smart: bool) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["mixed"])
    assert_roundtrip(tmp_path, tree, CreateOptions(smart_store=smart))


def test_roundtrip_forced_zip64(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["nested"])
    assert_roundtrip(tmp_path, tree, CreateOptions(force_zip64=True))


def test_roundtrip_single_file_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    make_tree(src, {"only.txt": b"just one"})
    archive = tmp_path / "one.zip"
    create_archive([src / "only.txt"], archive)
    extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "only.txt").read_bytes() == b"just one"


def test_roundtrip_multiple_sources(tmp_path: Path) -> None:
    src = tmp_path / "src"
    make_tree(src, {"a/x.txt": b"x", "b/y.txt": b"y", "c.txt": b"c"})
    archive = tmp_path / "multi.zip"
    create_archive([src / "a", src / "b", src / "c.txt"], archive)
    extract_archive(archive, tmp_path / "out")
    assert sorted(snapshot(tmp_path / "out")) == ["a", "a/x.txt", "b", "b/y.txt", "c.txt"]


def test_roundtrip_root_dir_drops_top_folder(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "tree", SHAPES["nested"])
    archive = tmp_path / "flat.zip"
    create_archive([tree], archive, CreateOptions(root_dir=tree))
    extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "top.txt").read_bytes() == b"top"


def test_roundtrip_large_random_file_is_identical(tmp_path: Path) -> None:
    src = tmp_path / "src"
    payload = os.urandom(300_000)
    make_tree(src, {"big.bin": payload})
    archive = tmp_path / "big.zip"
    create_archive([src / "big.bin"], archive, CreateOptions(level="best"))
    extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "big.bin").read_bytes() == payload


@pytest.mark.needs_symlink
@pytest.mark.parametrize("password", [None, "pw"], ids=["plain", "aes"])
def test_roundtrip_symlinks_are_stored_not_followed(tmp_path: Path, password: str | None) -> None:
    spec = {"real.txt": b"data", "dir/inner.txt": b"in", "link.txt": Link("real.txt")}
    tree = make_tree(tmp_path / "src" / "tree", spec)
    archive = tmp_path / "l.zip"
    create_archive([tree], archive, CreateOptions(password=password))
    dest = tmp_path / "out"
    extract_archive(archive, dest, ExtractOptions(password=password))
    link = dest / "tree" / "link.txt"
    assert link.is_symlink()
    assert os.readlink(link).replace("\\", "/") == "real.txt"
    assert link.read_bytes() == b"data"


@pytest.mark.needs_symlink
def test_follow_symlinks_stores_the_content(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "tree", {"real.txt": b"data", "link.txt": Link("real.txt")})
    archive = tmp_path / "f.zip"
    create_archive([tree], archive, CreateOptions(follow_symlinks=True))
    dest = tmp_path / "out"
    extract_archive(archive, dest)
    assert not (dest / "tree" / "link.txt").is_symlink()
    assert (dest / "tree" / "link.txt").read_bytes() == b"data"


def test_format_is_zip(tmp_path: Path) -> None:
    assert CreateOptions().format is FormatId.ZIP
