"""Unit tests for the name-encoding detector, on lists of raw names."""

from __future__ import annotations

import time

import pytest

from helpers.names import (
    ARABIC,
    CORPUS,
    GREEK,
    HEBREW,
    JAPANESE,
    KOREAN,
    RUSSIAN,
    SIMPLIFIED,
    TRADITIONAL,
    WEST_CP437,
    WEST_CP850,
    WEST_CP1252,
)
from zipwise.encoding import (
    CANDIDATES,
    EncodingGuess,
    decode_name,
    decode_name_strict,
    detect_names,
)


def raw(names: list[str], encoding: str) -> list[bytes]:
    return [n.encode(encoding) for n in names]


def roundtrip(names: list[str], encoding: str, hint: str | None = None) -> EncodingGuess:
    """Detect, then check that decoding with the guess gives back the original names."""
    guess = detect_names(raw(names, encoding), hint)
    assert [r.decode(guess.encoding) for r in raw(names, encoding)] == names, guess
    return guess


# --- the easy cases -------------------------------------------------------------------------


def test_pure_ascii_is_ascii() -> None:
    guess = detect_names([b"readme.txt", b"src/main.py"])
    assert (guess.encoding, guess.confidence) == ("ascii", 1.0)


def test_empty_list_is_ascii() -> None:
    assert detect_names([]).encoding == "ascii"


@pytest.mark.parametrize(
    "names",
    [
        ["Отчёт.docx"],
        ["تقرير المبيعات.docx", "report.txt"],
        ["日本語/ファイル.txt", "データ.csv"],
        ["café.txt", "naïve.md", "plain.txt"],
        ["emoji 😀.txt"],
    ],
)
def test_valid_utf8_with_multibyte_is_utf8(names: list[str]) -> None:
    guess = detect_names(raw(names, "utf-8"))
    assert guess.encoding == "utf-8"
    assert guess.confidence >= 0.9


def test_more_utf8_evidence_means_more_confidence() -> None:
    one = detect_names(["é.txt".encode()])
    many = detect_names(raw(ARABIC, "utf-8"))
    assert many.confidence >= one.confidence


def test_utf8_alternatives_name_cp437() -> None:
    assert "cp437" in detect_names(raw(ARABIC, "utf-8")).alternatives


def test_mixed_utf8_and_legacy_names_choose_utf8() -> None:
    names = [n.encode("utf-8") for n in ARABIC] + ["café".encode("cp1252")]
    guess = detect_names(names)
    assert guess.encoding == "utf-8"


# --- every code page, whole archive ---------------------------------------------------------


@pytest.mark.parametrize("encoding", list(CORPUS), ids=list(CORPUS))
def test_corpus_roundtrip(encoding: str) -> None:
    guess = roundtrip(CORPUS[encoding], encoding)
    assert guess.confidence > 0.5


@pytest.mark.parametrize("encoding", list(CORPUS), ids=list(CORPUS))
def test_corpus_with_ascii_names_mixed_in(encoding: str) -> None:
    names = [*CORPUS[encoding], "README.txt", "docs/index.html"]
    roundtrip(names, encoding)


@pytest.mark.parametrize("encoding", list(CORPUS), ids=list(CORPUS))
def test_corpus_roundtrip_is_independent_of_order(encoding: str) -> None:
    roundtrip(list(reversed(CORPUS[encoding])), encoding)


@pytest.mark.parametrize("encoding", list(CORPUS), ids=list(CORPUS))
def test_two_names_are_enough(encoding: str) -> None:
    roundtrip(CORPUS[encoding][:2], encoding)


def test_all_candidates_are_known_codecs() -> None:
    for name in CANDIDATES:
        assert "x".encode(name) == b"x"


# --- the classic confusions -----------------------------------------------------------------


def test_cp1256_arabic_is_not_cp437_gibberish() -> None:
    names = raw(ARABIC, "cp1256")
    gibberish = names[0].decode("cp437")
    assert "╩" in gibberish or "╟" in gibberish or "╚" in gibberish  # what cp437 would show
    guess = roundtrip(ARABIC, "cp1256")
    assert guess.encoding == "cp1256"


def test_cp1256_arabic_is_not_cp1252_latin() -> None:
    assert detect_names(raw(ARABIC, "cp1256")).encoding == "cp1256"
    assert raw(ARABIC, "cp1256")[0].decode("cp1252").startswith("Ê")


def test_cp720_dos_arabic_is_not_cp1256() -> None:
    assert detect_names(raw(ARABIC, "cp720")).encoding == "cp720"


def test_cp1256_windows_arabic_is_not_cp720() -> None:
    assert detect_names(raw(ARABIC, "cp1256")).encoding == "cp1256"


def test_persian_letters_still_pick_cp1256() -> None:
    names = ["پروژه جديد.docx", "گزارش چهارم.pdf", "ژاله و چنار.txt"]
    assert detect_names(raw(names, "cp1256")).encoding == "cp1256"


def test_cp866_is_not_cp1251() -> None:
    guess = roundtrip(RUSSIAN, "cp866")
    assert guess.encoding == "cp866"


def test_cp1251_is_not_cp866() -> None:
    guess = roundtrip(RUSSIAN, "cp1251")
    assert guess.encoding == "cp1251"


@pytest.mark.parametrize(
    "names",
    [
        ["Щука и ёж.txt", "Эхо.txt", "Чехов/Вишнёвый сад.fb2"],
        ["дом.txt", "сад/огород.txt", "кот и пёс.jpg"],
        ["Фотографии с отпуска.zip", "Мои документы/Резюме.docx"],
    ],
)
@pytest.mark.parametrize("encoding", ["cp866", "cp1251"])
def test_russian_both_ways(names: list[str], encoding: str) -> None:
    assert detect_names(raw(names, encoding)).encoding == encoding


def test_cp866_text_decoded_as_cp1251_would_be_rare_letters() -> None:
    wrong = raw(RUSSIAN, "cp866")[0]
    try:
        text = wrong.decode("cp1251")
    except UnicodeDecodeError:
        return  # some bytes are undefined in cp1251: the candidate is simply rejected
    assert any(ch in "ЂЃЉЊЌЋЏђѓљњќћџЎўЈҐ" for ch in text)


def test_shift_jis_is_not_gbk() -> None:
    assert roundtrip(JAPANESE, "cp932").encoding == "cp932"


def test_gbk_is_not_shift_jis() -> None:
    assert roundtrip(SIMPLIFIED, "gbk").encoding == "gbk"


def test_kanji_only_japanese_is_shift_jis() -> None:
    names = ["売上報告書.xlsx", "議事録.docx", "契約書/見積書.pdf"]
    assert detect_names(raw(names, "cp932")).encoding == "cp932"


def test_big5_is_not_gbk_and_gbk_is_not_big5() -> None:
    assert roundtrip(TRADITIONAL, "big5").encoding == "big5"
    assert roundtrip(SIMPLIFIED, "gbk").encoding == "gbk"


def test_korean_is_cp949_not_a_chinese_code_page() -> None:
    assert roundtrip(KOREAN, "cp949").encoding == "cp949"


def test_greek_is_not_latin_accents() -> None:
    assert roundtrip(GREEK, "cp1253").encoding == "cp1253"
    assert raw(GREEK, "cp1253")[0].decode("cp1252").startswith("Á")


def test_hebrew_is_not_cyrillic_or_greek() -> None:
    assert roundtrip(HEBREW, "cp1255").encoding == "cp1255"


def test_western_dos_pages_are_not_windows_1252() -> None:
    assert roundtrip(WEST_CP437, "cp437").encoding == "cp437"
    assert roundtrip(WEST_CP850, "cp850").encoding == "cp850"


def test_windows_1252_is_not_dos() -> None:
    assert roundtrip(WEST_CP1252, "cp1252").encoding == "cp1252"


def test_cp437_and_cp850_share_names_when_letters_are_common() -> None:
    names = ["Müller Übersicht.xlsx", "señor niño.docx"]
    guess = detect_names(raw(names, "cp437"))
    assert guess.encoding == "cp437"  # the historical default wins an exact tie
    assert "cp850" in guess.alternatives


def test_decoding_failure_rejects_a_candidate() -> None:
    # 0x81 is undefined in cp1252, so a name using it cannot be cp1252
    guess = detect_names([b"Mu\x81ller.txt"])
    assert guess.encoding != "cp1252"


# --- locale hint ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hint", "expected"),
    [(None, "cp437"), ("de_DE", "cp850"), ("pt_BR.UTF-8", "cp850"), ("en_US", "cp437")],
)
def test_hint_breaks_exact_ties(hint: str | None, expected: str) -> None:
    tied = [b"M\x81ller.txt", b"se\xa4or.txt"]  # same text in cp437 and cp850
    assert detect_names(tied, hint).encoding == expected


@pytest.mark.parametrize("hint", ["ru_RU", "ja_JP", "ar_EG", "zh_CN", "ko_KR", "he_IL", "xx", ""])
def test_hint_never_overrides_evidence(hint: str) -> None:
    assert detect_names(raw(ARABIC, "cp1256"), hint).encoding == "cp1256"
    assert detect_names(raw(SIMPLIFIED, "gbk"), hint).encoding == "gbk"


def test_hint_can_be_an_encoding_name() -> None:
    tied = [b"M\x81ller.txt"]
    assert detect_names(tied, "cp850").encoding == "cp850"


def test_hint_resolves_ambiguous_russian() -> None:
    # a very short name is ambiguous; the hint is allowed to decide between the two readings
    name = ["дом.txt".encode("cp1251")]
    assert detect_names(name, "ru_RU").encoding in ("cp1251", "cp866")


# --- shape of the result --------------------------------------------------------------------


def test_confidence_is_a_probability_like_number() -> None:
    for enc, names in CORPUS.items():
        guess = detect_names(raw(names, enc))
        assert 0.0 <= guess.confidence <= 1.0


def test_clear_evidence_beats_a_single_ambiguous_name() -> None:
    strong = detect_names(raw(ARABIC, "cp1256"))
    weak = detect_names([raw(["é"], "cp1252")[0]])
    assert strong.confidence > weak.confidence


def test_alternatives_are_other_candidates() -> None:
    guess = detect_names(raw(RUSSIAN, "cp1251"))
    assert guess.encoding not in guess.alternatives
    assert set(guess.alternatives) <= set(CANDIDATES) | {"utf-8"}


def test_detection_ignores_duplicates_and_is_deterministic() -> None:
    names = raw(ARABIC * 3, "cp1256")
    assert detect_names(names) == detect_names(names)


def test_large_lists_are_sampled_and_fast() -> None:
    names = [f"{ARABIC[i % len(ARABIC)]} {i}".encode("cp1256") for i in range(20_000)]
    started = time.monotonic()
    assert detect_names(names).encoding == "cp1256"
    assert time.monotonic() - started < 5


# --- decode helpers -------------------------------------------------------------------------


def test_decode_name_uses_the_encoding() -> None:
    assert decode_name("ملف.txt".encode("cp1256"), "cp1256") == "ملف.txt"


def test_decode_name_never_raises() -> None:
    assert "�" in decode_name(b"\xff\xfe", "utf-8")


def test_decode_name_strict_returns_none_on_failure() -> None:
    assert decode_name_strict(b"\xff", "utf-8") is None
    assert decode_name_strict(b"abc", "utf-8") == "abc"
