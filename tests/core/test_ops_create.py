from __future__ import annotations

import os
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from helpers.jobs import stepping_context
from helpers.trees import BASE_MTIME, SHAPES, Link, make_tree, text_bytes
from zipwise.backends import zip as zipmod
from zipwise.errors import Cancelled, CorruptArchive, IOFailure, UsageError
from zipwise.model import CreateOptions, FormatId, Issue
from zipwise.ops import create_archive, list_archive


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def zip_infos(path: Path) -> dict[str, zipfile.ZipInfo]:
    with zipfile.ZipFile(path) as zf:
        return {i.filename: i for i in zf.infolist()}


def test_create_returns_a_report_and_leaves_no_partial_file(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["nested"])
    out = tmp_path / "out" / "t.zip"
    report = create_archive([tree], out)
    assert report.ok and report.issues == ()
    assert report.output_paths == (out,)
    assert report.entries_done == len(list_archive(out))
    assert leftovers(out.parent) == ["t.zip"]


def test_archive_is_written_to_a_partial_file_until_the_end(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["simple"])
    out = tmp_path / "t.zip"
    seen: list[list[str]] = []
    ctx, _, _ = stepping_context(observe=lambda e: seen.append(leftovers(tmp_path)))
    create_archive([tree], out, ctx=ctx)
    during = [names for names in seen if "t.zip.partial" in names]
    assert during, "the .partial file was never visible"
    assert all("t.zip" not in names for names in during)
    assert leftovers(tmp_path) == ["src", "t.zip"]


def test_cancel_removes_the_partial_and_creates_nothing(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["nested"])
    out = tmp_path / "t.zip"
    ctx, _, _ = stepping_context(cancel_on="t/d1/one.txt")
    with pytest.raises(Cancelled):
        create_archive([tree], out, ctx=ctx)
    assert leftovers(tmp_path) == ["src"]


def test_cancel_keeps_an_existing_archive_untouched(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["nested"])
    out = tmp_path / "t.zip"
    out.write_bytes(b"previous")
    ctx, _, _ = stepping_context(cancel_on="t/top.txt")
    with pytest.raises(Cancelled):
        create_archive([tree], out, ctx=ctx)
    assert out.read_bytes() == b"previous"
    assert leftovers(tmp_path) == ["src", "t.zip"]


def test_error_while_writing_removes_the_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["nested"])
    out = tmp_path / "t.zip"
    original = zipmod.ZipWriter.add_file
    calls = 0

    def flaky(self: zipmod.ZipWriter, src: Path, arcname: str, *, store: bool = False) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise IOFailure("disk exploded")
        original(self, src, arcname, store=store)

    monkeypatch.setattr(zipmod.ZipWriter, "add_file", flaky)
    with pytest.raises(IOFailure):
        create_archive([tree], out)
    assert leftovers(tmp_path) == ["src"]


def test_verification_runs_by_default_and_failure_discards_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["simple"])
    out = tmp_path / "t.zip"
    monkeypatch.setattr(
        zipmod.ZipReader,
        "test",
        lambda self, ctx: [Issue("error", "corrupt-archive", "a.txt", "bad bytes")],
    )
    with pytest.raises(CorruptArchive, match="Verification failed"):
        create_archive([tree], out)
    assert leftovers(tmp_path) == ["src"]


def test_verification_can_be_switched_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["simple"])
    out = tmp_path / "t.zip"
    called: list[bool] = []
    monkeypatch.setattr(zipmod.ZipReader, "test", lambda self, ctx: called.append(True) or [])
    create_archive([tree], out, CreateOptions(verify=False))
    assert called == [] and out.exists()


def test_verification_phase_is_reported(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["nested"])
    ctx, _, events = stepping_context()
    create_archive([tree], tmp_path / "t.zip", ctx=ctx)
    phases = [e.phase for e in events]
    assert phases[0] == "scan"
    assert "compress" in phases and "verify" in phases
    assert phases.index("compress") < phases.index("verify")
    last_compress = [e for e in events if e.phase == "compress"][-1]
    assert last_compress.bytes_done == last_compress.bytes_total
    assert last_compress.entries_done == last_compress.entries_total


@pytest.mark.parametrize("size", [0, -5])
def test_bad_split_size_is_a_usage_error(tmp_path: Path, size: int) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["simple"])
    with pytest.raises(UsageError, match="at least 1 byte"):
        create_archive([tree], tmp_path / "t.zip", CreateOptions(split_size=size))
    assert leftovers(tmp_path) == ["src"]


def test_split_size_creates_numbered_parts_and_no_whole_file(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["mixed"])
    report = create_archive([tree], tmp_path / "t.zip", CreateOptions(split_size=20_000))
    names = leftovers(tmp_path)
    assert "t.zip" not in names
    assert [n for n in names if n.startswith("t.zip.0")][0] == "t.zip.001"
    assert {p.name for p in report.output_paths} == set(names) - {"src"}


def test_rar_cannot_be_created(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["simple"])
    from zipwise.errors import UnsupportedFormat

    with pytest.raises(UnsupportedFormat):
        create_archive([tree], tmp_path / "t.rar", CreateOptions(format=FormatId.RAR))


def test_no_inputs_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        create_archive([], tmp_path / "t.zip")


def test_missing_input_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="not found"):
        create_archive([tmp_path / "nope"], tmp_path / "t.zip")
    assert leftovers(tmp_path) == []


def test_destination_directory_is_a_usage_error(tmp_path: Path) -> None:
    src = make_tree(tmp_path / "s", {"a": b"x"})
    with pytest.raises(UsageError):
        create_archive([src], tmp_path)


@pytest.mark.parametrize("level", ["turbo", 11, -3])
def test_bad_level_is_a_usage_error_and_cleans_up(tmp_path: Path, level: object) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["simple"])
    with pytest.raises(UsageError):
        create_archive([tree], tmp_path / "t.zip", CreateOptions(level=level))  # type: ignore[arg-type]
    assert leftovers(tmp_path) == ["src"]


def test_destination_inside_the_source_is_not_archived_into_itself(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "t", SHAPES["simple"])
    out = tree / "t.zip"
    create_archive([tree], out)
    assert sorted(i.name for i in list_archive(out)) == ["t", "t/a.txt", "t/b.txt"]


def test_existing_archive_is_replaced(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["simple"])
    out = tmp_path / "t.zip"
    out.write_bytes(b"old")
    create_archive([tree], out)
    assert zipfile.is_zipfile(out)


def test_empty_directory_alone_is_archivable(tmp_path: Path) -> None:
    (tmp_path / "void").mkdir()
    out = tmp_path / "v.zip"
    create_archive([tmp_path / "void"], out)
    assert [i.name for i in list_archive(out)] == ["void"]


def test_parent_folders_of_the_destination_are_created(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["simple"])
    out = tmp_path / "a" / "b" / "t.zip"
    create_archive([tree], out)
    assert out.exists()


# --- what ends up inside the ZIP ------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "stored"),
    [("photo.jpg", True), ("sub/archive.zip", True), ("text.txt", False), ("empty.dat", True)],
)
def test_per_file_store_for_compressed_types(tmp_path: Path, name: str, stored: bool) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["mixed"])
    out = tmp_path / "m.zip"
    create_archive([tree], out)
    method = zip_infos(out)[f"t/{name}"].compress_type
    assert (method == zipfile.ZIP_STORED) is stored


def test_smart_store_off_deflates_everything_non_empty(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["mixed"])
    out = tmp_path / "m.zip"
    create_archive([tree], out, CreateOptions(smart_store=False))
    infos = zip_infos(out)
    assert infos["t/photo.jpg"].compress_type == zipfile.ZIP_DEFLATED
    assert infos["t/empty.dat"].compress_type == zipfile.ZIP_STORED


def test_level_presets_trade_size_for_speed(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", {"big.txt": text_bytes(200_000, 4)})
    sizes = {}
    for level in ("fast", "best"):
        out = tmp_path / f"{level}.zip"
        create_archive([tree], out, CreateOptions(level=level))
        sizes[level] = out.stat().st_size
    assert sizes["best"] <= sizes["fast"]
    assert sizes["fast"] < 100_000


def test_level_zero_stores_everything(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["nested"])
    out = tmp_path / "z.zip"
    create_archive([tree], out, CreateOptions(level=0))
    assert {i.compress_type for i in zip_infos(out).values()} == {zipfile.ZIP_STORED}


@pytest.mark.parametrize("shape", ["simple", "unicode", "empty_dirs"])
def test_every_name_carries_the_utf8_flag_even_ascii(tmp_path: Path, shape: str) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES[shape])
    out = tmp_path / "u.zip"
    create_archive([tree], out)
    assert all(i.flag_bits & 0x800 for i in zip_infos(out).values())


def test_names_are_posix_style_and_directories_end_with_slash(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["nested"])
    out = tmp_path / "n.zip"
    create_archive([tree], out)
    names = list(zip_infos(out))
    assert "t/d1/d2/two.txt" in names
    assert "t/d1/" in names
    assert not any("\\" in n for n in names)


def test_empty_directories_are_explicit_entries(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["empty_dirs"])
    out = tmp_path / "e.zip"
    create_archive([tree], out)
    infos = zip_infos(out)
    assert infos["t/empty/"].is_dir()
    assert infos["t/parent/child/"].is_dir()
    assert infos["t/empty/"].external_attr & 0x10


def test_unix_attributes_are_stored(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", {"f.txt": b"x", "d/": None})
    if os.name != "nt":
        os.chmod(tree / "f.txt", 0o640)
        os.chmod(tree / "d", 0o750)
    out = tmp_path / "m.zip"
    create_archive([tree], out)
    infos = zip_infos(out)
    file_mode = (infos["t/f.txt"].external_attr >> 16) & 0o7777
    dir_mode = (infos["t/d/"].external_attr >> 16) & 0o7777
    assert infos["t/f.txt"].create_system == 3
    assert (file_mode, dir_mode) == ((0o640, 0o750) if os.name != "nt" else (0o644, 0o755))
    assert stat.S_ISREG(infos["t/f.txt"].external_attr >> 16)
    assert stat.S_ISDIR(infos["t/d/"].external_attr >> 16)


def test_mtimes_are_exact_to_the_second_through_the_unix_time_field(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", {"f.txt": b"x"})
    os.utime(
        tree / "f.txt", (BASE_MTIME + 1, BASE_MTIME + 1)
    )  # odd second: DOS time cannot hold it
    out = tmp_path / "m.zip"
    create_archive([tree], out)
    entry = next(e for e in list_archive(out) if e.name == "t/f.txt")
    assert entry.mtime == datetime.fromtimestamp(BASE_MTIME + 1, tz=UTC)


@pytest.mark.parametrize("stamp", [0, 86_400, 4_300_000_000])
def test_out_of_range_mtimes_are_clamped_not_fatal(tmp_path: Path, stamp: int) -> None:
    tree = make_tree(tmp_path / "src" / "t", {"f.txt": b"x"})
    try:
        os.utime(tree / "f.txt", (stamp, stamp))
    except (OSError, OverflowError):
        pytest.skip("file system cannot hold this timestamp")
    out = tmp_path / "m.zip"
    create_archive([tree], out)
    with zipfile.ZipFile(out) as zf:
        assert zf.testzip() is None
        info = zf.getinfo("t/f.txt")
        assert 1980 <= info.date_time[0] <= 2107


def test_forced_zip64_uses_zip64_headers(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["simple"])
    plain, big = tmp_path / "p.zip", tmp_path / "z.zip"
    create_archive([tree], plain)
    create_archive([tree], big, CreateOptions(force_zip64=True))
    assert zip_infos(plain)["t/a.txt"].extract_version == 20
    assert zip_infos(big)["t/a.txt"].extract_version == 45


@pytest.mark.needs_symlink
def test_symlinks_are_stored_as_link_entries(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", {"real.txt": b"data", "l": Link("real.txt")})
    out = tmp_path / "l.zip"
    create_archive([tree], out)
    info = zip_infos(out)["t/l"]
    assert stat.S_ISLNK(info.external_attr >> 16)
    with zipfile.ZipFile(out) as zf:
        assert zf.read("t/l") == b"real.txt"
    assert info.compress_type == zipfile.ZIP_STORED


def test_password_encrypts_every_file_but_not_directories(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["empty_dirs"])
    out = tmp_path / "p.zip"
    create_archive([tree], out, CreateOptions(password="pw"))
    infos = zip_infos(out)
    assert infos["t/parent/file.txt"].flag_bits & 1
    assert not infos["t/empty/"].flag_bits & 1


def test_empty_password_means_no_encryption(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "src" / "t", SHAPES["simple"])
    out = tmp_path / "p.zip"
    create_archive([tree], out, CreateOptions(password=""))
    assert not any(i.flag_bits & 1 for i in zip_infos(out).values())
