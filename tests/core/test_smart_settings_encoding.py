from __future__ import annotations

import json
from pathlib import Path

import pytest

from zipwise.encoding import decode_name, detect_names
from zipwise.errors import UsageError
from zipwise.model import ConflictAction
from zipwise.settings import Settings, load_settings, save_settings, settings_path
from zipwise.smart import (
    COMPRESSED_EXTENSIONS,
    is_compressed_name,
    resolve_level,
    should_store,
)


@pytest.mark.parametrize(
    ("level", "expected"),
    [("fast", 1), ("normal", 6), ("best", 9), (0, 0), (1, 1), (5, 5), (9, 9)],
)
def test_resolve_level(level: str | int, expected: int) -> None:
    assert resolve_level(level) == expected


@pytest.mark.parametrize("level", ["ultra", "", -1, 10, 100, True])
def test_resolve_level_rejects_bad_values(level: object) -> None:
    with pytest.raises(UsageError):
        resolve_level(level)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    ["a.jpg", "A.JPG", "x/y/z.png", "movie.mp4", "song.mp3", "b.zip", "c.7z", "d.gz", "e.pdf",
     "f.docx", "g.xlsx", "h.webp", "i.flac", "j.zst", "k.xz", "l.bz2", "m.rar", "n.mkv"],
)  # fmt: skip
def test_compressed_extensions_are_stored(name: str) -> None:
    assert is_compressed_name(name)
    assert should_store(name, 1000)


@pytest.mark.parametrize(
    "name", ["a.txt", "b.py", "c.csv", "d.json", "e.bmp", "f.wav", "g", "h.tar", "i.zip.txt"]
)
def test_other_extensions_are_compressed(name: str) -> None:
    assert not is_compressed_name(name)
    assert not should_store(name, 1000)


def test_smart_store_can_be_switched_off() -> None:
    assert not should_store("a.jpg", 1000, smart=False)


def test_empty_files_are_always_stored() -> None:
    assert should_store("empty.txt", 0, smart=False)


def test_extension_list_is_lowercase_with_dots() -> None:
    assert all(e.startswith(".") and e == e.lower() for e in COMPRESSED_EXTENSIONS)


def test_backslash_names_are_handled() -> None:
    assert is_compressed_name("dir\\pic.jpg")


# --- encoding (minimal detector) ------------------------------------------------------------


def test_ascii_names_are_ascii() -> None:
    guess = detect_names([b"a.txt", b"dir/b.txt"])
    assert guess.encoding == "ascii"
    assert guess.confidence == 1.0


def test_valid_utf8_is_utf8() -> None:
    guess = detect_names(["café.txt".encode(), b"plain.txt"])
    assert guess.encoding == "utf-8"
    assert guess.confidence > 0.5


def test_invalid_utf8_falls_back_to_cp437() -> None:
    guess = detect_names(["café.txt".encode("cp1252")])
    assert guess.encoding == "cp437"


def test_empty_name_list_is_ascii() -> None:
    assert detect_names([]).encoding == "ascii"


def test_decode_name_is_lenient() -> None:
    assert decode_name("hé".encode("cp437"), "cp437") == "hé"
    assert "�" in decode_name(b"\xff\xfe", "utf-8")


# --- settings -------------------------------------------------------------------------------


def test_defaults(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "missing.json")
    assert s == Settings()
    assert s.verify_after_create is True
    assert s.max_ratio == 250


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "cfg" / "settings.json"
    original = Settings(language="ar", default_level="best", max_total_bytes=10, smart_store=False)
    save_settings(original, target)
    assert load_settings(target) == original
    assert list(target.parent.iterdir()) == [target]  # no temp file left behind


@pytest.mark.parametrize("content", ["", "{", "[]", "null", "\u0000\u0001", '"text"'])
def test_corrupt_files_yield_defaults(tmp_path: Path, content: str) -> None:
    target = tmp_path / "s.json"
    target.write_text(content, encoding="utf-8")
    assert load_settings(target) == Settings()


def test_unknown_keys_and_wrong_types_are_ignored(tmp_path: Path) -> None:
    target = tmp_path / "s.json"
    target.write_text(
        json.dumps({"language": "ar", "max_entries": "many", "smart_store": 1, "future": True}),
        encoding="utf-8",
    )
    s = load_settings(target)
    assert s.language == "ar"
    assert s.max_entries == Settings().max_entries
    assert s.smart_store is True


def test_to_policy_maps_limits() -> None:
    policy = Settings(max_ratio=10.5, max_entries=7, allow_symlinks=False).to_policy()
    assert (policy.max_ratio, policy.max_entries, policy.allow_symlinks) == (10.5, 7, False)


def test_to_policy_tolerates_bad_windows_mode() -> None:
    assert Settings(sanitize_windows_names="bogus").to_policy().sanitize_windows_names == "auto"


def test_default_conflict_is_a_known_action() -> None:
    assert ConflictAction(Settings().on_conflict) is ConflictAction.ASK


@pytest.mark.parametrize("nt", [True, False])
def test_settings_path_per_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, nt: bool
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = settings_path(windows=nt)
    if nt:
        assert path == tmp_path / "appdata" / "Zipwise" / "settings.json"
    else:
        assert path == tmp_path / "xdg" / "zipwise" / "settings.json"
