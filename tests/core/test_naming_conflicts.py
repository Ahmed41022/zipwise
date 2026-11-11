from __future__ import annotations

from pathlib import Path

import pytest

from zipwise.conflicts import ConflictResolver, unique_path
from zipwise.jobs import CancelToken, JobContext
from zipwise.model import ConflictAction
from zipwise.naming import (
    RESERVED_NAMES,
    CaseClashResolver,
    is_reserved,
    numbered_name,
    sanitize_component,
    unique_name,
)

PLAIN_RESERVED = ["CON", "PRN", "AUX", "NUL"] + [f"COM{i}" for i in range(1, 10)]
PLAIN_RESERVED += [f"LPT{i}" for i in range(1, 10)]


def test_reserved_set_is_complete() -> None:
    assert set(PLAIN_RESERVED) <= RESERVED_NAMES
    assert len(PLAIN_RESERVED) == 22


@pytest.mark.parametrize("name", PLAIN_RESERVED)
def test_reserved_bare_name_gets_prefix(name: str) -> None:
    assert sanitize_component(name) == "_" + name


@pytest.mark.parametrize("name", PLAIN_RESERVED)
def test_reserved_with_extension_gets_prefix(name: str) -> None:
    assert sanitize_component(name.lower() + ".txt") == "_" + name.lower() + ".txt"


@pytest.mark.parametrize("name", ["CON.tar.gz", "nul .txt", "Aux.", "com1 "])
def test_reserved_variants(name: str) -> None:
    assert sanitize_component(name).startswith("_")
    assert is_reserved(name)


@pytest.mark.parametrize("name", ["CONSOLE", "COM10", "COM0", "LPT", "NULL", "xCON", "a.CON"])
def test_near_misses_are_not_reserved(name: str) -> None:
    assert sanitize_component(name) == name


@pytest.mark.parametrize("char", list('<>:"|?*'))
def test_invalid_characters_become_underscore(char: str) -> None:
    assert sanitize_component(f"a{char}b") == "a_b"


@pytest.mark.parametrize("code", range(32))
def test_control_characters_become_underscore(code: int) -> None:
    assert sanitize_component(f"a{chr(code)}b") == "a_b"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.", "a"),
        ("a. . ", "a"),
        ("a ", "a"),
        ("...", "_"),
        (".. ", "_"),
        ("", "_"),
        (" ", "_"),
        ("x:y", "x_y"),
        ("a?b", "a_b"),
        (".hidden", ".hidden"),
        ("normal name.txt", "normal name.txt"),
        ("ملف.txt", "ملف.txt"),
    ],
)
def test_sanitize_examples(name: str, expected: str) -> None:
    assert sanitize_component(name) == expected


def test_non_windows_mode_only_removes_nul() -> None:
    assert sanitize_component("CON", windows=False) == "CON"
    assert sanitize_component("a:b?", windows=False) == "a:b?"
    assert sanitize_component("a\0b", windows=False) == "a_b"


@pytest.mark.parametrize(
    ("name", "n", "expected"),
    [
        ("report.txt", 2, "report (2).txt"),
        ("archive.tar.gz", 3, "archive.tar (3).gz"),
        ("noext", 2, "noext (2)"),
        (".bashrc", 2, ".bashrc (2)"),
    ],
)
def test_numbered_name(name: str, n: int, expected: str) -> None:
    assert numbered_name(name, n) == expected


def test_unique_name_skips_taken_names() -> None:
    taken = {"a.txt", "a (2).txt", "a (3).txt"}
    assert unique_name("a.txt", taken.__contains__) == "a (4).txt"
    assert unique_name("b.txt", taken.__contains__) == "b.txt"


def test_case_clash_noop_on_case_sensitive_target() -> None:
    r = CaseClashResolver(case_insensitive=False)
    assert r.resolve(("Readme.txt",)) == ("Readme.txt",)
    assert r.resolve(("README.TXT",)) == ("README.TXT",)


def test_case_clash_renames_second_file() -> None:
    r = CaseClashResolver(case_insensitive=True)
    assert r.resolve(("Readme.txt",)) == ("Readme.txt",)
    assert r.resolve(("README.TXT",)) == ("README (2).TXT",)
    assert r.resolve(("readme.txt",)) == ("readme (3).txt",)


def test_case_clash_identical_duplicate_is_left_for_the_conflict_policy() -> None:
    r = CaseClashResolver(case_insensitive=True)
    assert r.resolve(("a.txt",)) == ("a.txt",)
    assert r.resolve(("a.txt",)) == ("a.txt",)


def test_case_clash_merges_directories_to_first_spelling() -> None:
    r = CaseClashResolver(case_insensitive=True)
    assert r.resolve(("Docs", "a.txt")) == ("Docs", "a.txt")
    assert r.resolve(("docs", "b.txt")) == ("Docs", "b.txt")
    assert r.resolve(("DOCS",), is_dir=True) == ("Docs",)


def test_case_clash_detects_unicode_normalisation() -> None:
    r = CaseClashResolver(case_insensitive=True)
    composed = "café.txt"
    decomposed = "café.txt"
    assert r.resolve((composed,)) == (composed,)
    renamed = r.resolve((decomposed,))
    assert renamed != (decomposed,)
    assert "(2)" in renamed[0]


def test_case_clash_rename_does_not_collide_with_a_later_real_name() -> None:
    r = CaseClashResolver(case_insensitive=True)
    r.resolve(("a.txt",))
    assert r.resolve(("A.txt",)) == ("A (2).txt",)
    assert r.resolve(("a (2).txt",)) == ("a (2) (2).txt",)  # would clash with the rename


# --- conflicts ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action", [ConflictAction.SKIP, ConflictAction.RENAME, ConflictAction.OVERWRITE]
)
def test_fixed_policies_are_returned_as_is(action: ConflictAction) -> None:
    assert ConflictResolver(action, JobContext()).resolve(Path("x")) is action


def test_ask_without_callback_never_overwrites() -> None:
    assert ConflictResolver(ConflictAction.ASK, JobContext()).resolve(Path("x")) is (
        ConflictAction.SKIP
    )


def test_ask_calls_back_each_time_unless_remembered() -> None:
    answers = iter([(ConflictAction.RENAME, False), (ConflictAction.OVERWRITE, False)])
    seen: list[Path] = []

    def callback(path: Path) -> tuple[ConflictAction, bool]:
        seen.append(path)
        return next(answers)

    resolver = ConflictResolver(ConflictAction.ASK, JobContext(resolve_conflict=callback))
    assert resolver.resolve(Path("a")) is ConflictAction.RENAME
    assert resolver.resolve(Path("b")) is ConflictAction.OVERWRITE
    assert seen == [Path("a"), Path("b")]


def test_ask_remembers_apply_to_all() -> None:
    calls = 0

    def callback(path: Path) -> tuple[ConflictAction, bool]:
        nonlocal calls
        calls += 1
        return ConflictAction.OVERWRITE, True

    resolver = ConflictResolver(ConflictAction.ASK, JobContext(resolve_conflict=callback))
    assert [resolver.resolve(Path(n)) for n in "abc"] == [ConflictAction.OVERWRITE] * 3
    assert calls == 1


def test_ask_answering_ask_falls_back_to_skip() -> None:
    ctx = JobContext(resolve_conflict=lambda p: (ConflictAction.ASK, False))
    assert ConflictResolver(ConflictAction.ASK, ctx).resolve(Path("a")) is ConflictAction.SKIP


def test_ask_respects_cancellation() -> None:
    token = CancelToken()
    token.cancel()
    ctx = JobContext(cancel=token, resolve_conflict=lambda p: (ConflictAction.SKIP, False))
    from zipwise.errors import Cancelled

    with pytest.raises(Cancelled):
        ConflictResolver(ConflictAction.ASK, ctx).resolve(Path("a"))


def test_unique_path_finds_a_free_name(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("1")
    (tmp_path / "f (2).txt").write_text("2")
    assert unique_path(tmp_path / "f.txt") == tmp_path / "f (3).txt"
    assert unique_path(tmp_path / "g.txt") == tmp_path / "g.txt"
