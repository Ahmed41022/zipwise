"""The system locale breaks near-ties in name-encoding detection, and nothing more."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers.names import ARABIC, extracted_files, legacy_zip
from zipwise import encoding
from zipwise.encoding import _hint_order, detect_names
from zipwise.ops import archive_info, extract_archive, list_archive


def use_hint(monkeypatch: pytest.MonkeyPatch, hint: str | None) -> None:
    monkeypatch.setattr("zipwise.encoding.system_locale_hint", lambda: hint)


def names_of(path: Path) -> list[str]:
    return [e.name for e in list_archive(path)]


@pytest.mark.parametrize("name", ["קורות חיים.docx", "מוזיקה/שירים אהובים.mp3"])
def test_short_hebrew_name_needs_the_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    archive = legacy_zip(tmp_path / "he.zip", [name], "cp1255")
    assert names_of(archive) != [name]  # a near-tie the scorer alone gets wrong
    use_hint(monkeypatch, "cp1255")
    assert names_of(archive) == [name]
    use_hint(monkeypatch, "he_IL.UTF-8")
    assert names_of(archive) == [name]
    dest = tmp_path / "out"
    extract_archive(archive, dest)
    assert extracted_files(dest) == [name]


def test_short_arabic_name_with_the_arabic_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = legacy_zip(tmp_path / "ar.zip", ["ملف.txt"], "cp1256")
    use_hint(monkeypatch, "cp1256")
    assert names_of(archive) == ["ملف.txt"]
    assert archive_info(archive).name_encoding == "cp1256"


@pytest.mark.parametrize(("hint", "expected"), [("cp866", "сыр.txt"), ("cp1251", "бла.txt")])
def test_ambiguous_cyrillic_bytes_follow_the_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hint: str, expected: str
) -> None:
    # these bytes are a real word in both Cyrillic code pages
    raw = "сыр.txt".encode("cp866")
    assert raw.decode("cp1251") == "бла.txt"
    archive = legacy_zip(tmp_path / "ru.zip", [raw.decode("cp866")], "cp866")
    use_hint(monkeypatch, hint)
    assert names_of(archive) == [expected]


def test_a_clear_winner_beats_a_wrong_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = legacy_zip(tmp_path / "ar.zip", ARABIC, "cp1256")
    use_hint(monkeypatch, "cp1251")
    assert sorted(names_of(archive)) == sorted(ARABIC)
    assert detect_names([n.encode("cp1256") for n in ARABIC], "ru_RU").encoding == "cp1256"


def test_no_hint_in_tests_by_default() -> None:
    assert encoding.system_locale_hint() is None  # see tests/conftest.py


@pytest.mark.parametrize(
    ("hint", "order"),
    [
        (None, ()),
        ("", ()),
        ("cp1255", ("cp1255",)),
        ("1256,720", ("cp1256", "cp720")),
        ("1251,866", ("cp1251", "cp866")),
        ("1252,437", ("cp1252", "cp437")),
        ("1253,737", ("cp1253",)),  # cp737 is not a candidate
        ("1255,862", ("cp1255",)),
        ("936,936", ("gbk",)),
        ("950", ("big5",)),
        ("932", ("cp932",)),
        ("949", ("cp949",)),
        ("windows-1256", ("cp1256",)),
        ("ar_EG", ("cp1256", "cp720")),
        ("he_IL.UTF-8", ("cp1255",)),
        ("ru_RU.utf8", ("cp866", "cp1251")),
        ("uk_UA", ("cp866", "cp1251")),
        ("el_GR", ("cp1253",)),
        ("ja_JP", ("cp932",)),
        ("ko_KR", ("cp949",)),
        ("zh_CN", ("gbk",)),
        ("zh_TW", ("big5",)),
        ("de_DE@euro", ("cp850", "cp437", "cp1252")),
        ("xx_YY", ()),
        ("65001", ()),
    ],
)
def test_hint_order(hint: str | None, order: tuple[str, ...]) -> None:
    assert _hint_order(hint) == order


def test_system_hint_on_windows_uses_the_ansi_and_oem_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()  # the real function, not the conftest stub
    monkeypatch.setattr(encoding, "_windows_code_pages", lambda: ["1256", "720"])
    assert encoding.system_locale_hint() == "1256,720"


def test_system_hint_elsewhere_uses_the_locale_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(encoding, "_windows_code_pages", lambda: [])
    monkeypatch.setattr(encoding.locale, "getlocale", lambda *args: ("el_GR", "UTF-8"))
    assert encoding.system_locale_hint() == "el_GR"
    monkeypatch.setattr(encoding.locale, "getlocale", lambda *args: ("C", None))
    assert encoding.system_locale_hint() is None


def test_utf8_windows_pages_give_no_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(encoding, "_windows_code_pages", lambda: ["65001", "65001"])
    monkeypatch.setattr(encoding.locale, "getlocale", lambda *args: (None, None))
    assert encoding.system_locale_hint() is None


def test_the_real_system_hint_is_harmless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    hint = encoding.system_locale_hint()
    assert hint is None or isinstance(hint, str)
    _hint_order(hint)  # whatever this machine says can be parsed
