from __future__ import annotations

import pytest

from zipwise.errors import UsageError
from zipwise.split import PRESETS, parse_size


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1", 1),
        ("1000", 1000),
        ("25M", 25_000_000),
        ("25m", 25_000_000),
        ("25MB", 25_000_000),
        ("25 MB", 25_000_000),
        ("700MiB", 700 * 1024**2),
        ("700mib", 700 * 1024**2),
        ("1.5G", 1_500_000_000),
        ("1.5GiB", 1610612736),
        ("2GB", 2_000_000_000),
        ("4GiB", 4 * 1024**3),
        ("10K", 10_000),
        ("10KB", 10_000),
        ("10KiB", 10_240),
        ("1T", 1_000_000_000_000),
        ("0.5K", 500),
        (".5M", 500_000),
        ("512B", 512),
        ("  8M  ", 8_000_000),
        ("1.0M", 1_000_000),
        (12345, 12345),
    ],
)
def test_parse_size(text: str | int, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("email", 25_000_000),
        ("whatsapp", 2_000_000_000),
        ("telegram", 2_000_000_000),
        ("telegram-premium", 4_000_000_000),
        ("fat32", 2**32 - 1),
        ("EMAIL", 25_000_000),
        ("Telegram_Premium", 4_000_000_000),
    ],
)
def test_presets(name: str, expected: int) -> None:
    assert parse_size(name) == expected


def test_preset_table_matches_the_plan() -> None:
    assert set(PRESETS) == {"email", "whatsapp", "telegram", "telegram-premium", "fat32"}
    assert PRESETS["fat32"] == 4 * 1024**3 - 1


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "0", "0M", "0.0G", "-5", "-1M", "abc", "12XB", "1.2.3M", "M", "1e6", "1,5G",
     "nan", "inf", "0.0001K", "email2", "10 M B"],
)  # fmt: skip
def test_garbage_sizes_are_usage_errors(bad: str) -> None:
    with pytest.raises(UsageError):
        parse_size(bad)


@pytest.mark.parametrize("bad", [0, -1, True])
def test_non_positive_integers_are_usage_errors(bad: int) -> None:
    with pytest.raises(UsageError):
        parse_size(bad)


def test_error_has_a_hint_listing_presets() -> None:
    with pytest.raises(UsageError) as info:
        parse_size("lots")
    assert "email" in (info.value.hint or "")
