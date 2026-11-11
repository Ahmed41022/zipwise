from __future__ import annotations

import re
from collections.abc import Iterator

import pytest

from zipwise import i18n

PLACEHOLDER = re.compile(r"\{(\w+)\}")
ARABIC_LETTER = re.compile(r"[؀-ۿ]")


@pytest.fixture(autouse=True)
def restore_language() -> Iterator[None]:
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def test_languages_available() -> None:
    langs = i18n.available_languages()
    assert langs[0] == "en"
    assert "ar" in langs


def test_key_parity() -> None:
    en, ar = i18n.load("en"), i18n.load("ar")
    assert sorted(set(en) - set(ar)) == []
    assert sorted(set(ar) - set(en)) == []
    assert len(en) > 100


def test_placeholder_parity() -> None:
    en, ar = i18n.load("en"), i18n.load("ar")
    for key, text in en.items():
        assert sorted(PLACEHOLDER.findall(text)) == sorted(PLACEHOLDER.findall(ar[key])), key


def test_arabic_values_are_translated_not_copied() -> None:
    en, ar = i18n.load("en"), i18n.load("ar")
    same = [k for k in en if en[k] == ar[k]]
    assert set(same) <= {"app.title"}
    for key, text in ar.items():
        if key != "app.title":
            assert ARABIC_LETTER.search(text), key


def test_no_empty_values() -> None:
    for lang in i18n.available_languages():
        assert all(v.strip() for v in i18n.load(lang).values())


def test_tr_formats_placeholders() -> None:
    assert i18n.tr("status.entries", count=3) == "3 entries"
    i18n.set_language("ar")
    assert "3" in i18n.tr("status.entries", count=3)


def test_missing_placeholder_value_does_not_raise() -> None:
    assert "{count}" in i18n.tr("status.entries")
    assert i18n.tr("status.entries", other=1) == "{count} entries"


def test_unknown_key_returns_key() -> None:
    assert i18n.tr("no.such.key") == "no.such.key"
    i18n.set_language("ar")
    assert i18n.tr("no.such.key", x=1) == "no.such.key"


def test_missing_key_in_arabic_falls_back_to_english(monkeypatch: pytest.MonkeyPatch) -> None:
    ar = dict(i18n.load("ar"))
    del ar["action.open"]
    monkeypatch.setitem(i18n._catalogues, "ar", ar)
    i18n.set_language("ar")
    assert i18n.tr("action.open") == "Open"
    assert i18n.tr("action.new") != "New archive"


def test_unknown_language_falls_back_to_english() -> None:
    assert i18n.set_language("xx") == "en"
    assert i18n.current_language() == "en"
    assert i18n.load("xx") == {}


def test_rtl_detection() -> None:
    assert i18n.is_rtl("ar")
    assert not i18n.is_rtl("en")
    i18n.set_language("ar")
    assert i18n.is_rtl()


def test_catalogues_are_flat_string_maps() -> None:
    for lang in i18n.available_languages():
        for key, value in i18n.load(lang).items():
            assert re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)+", key), key
            assert isinstance(value, str)
