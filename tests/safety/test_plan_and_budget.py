from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from zipwise.errors import BombDetected, IOFailure
from zipwise.model import ArchiveEntry, EntryType
from zipwise.safety import (
    ByteBudget,
    ExtractionPlan,
    SafetyPolicy,
    plan_extraction,
    split_entry_name,
)

DEST = Path("dest")
BIG = 10**12


def entry(
    name: str,
    etype: EntryType = EntryType.FILE,
    *,
    size: int = 1,
    link: str | None = None,
    compressed: int | None = None,
    index: int = 0,
) -> ArchiveEntry:
    return ArchiveEntry(
        name=name,
        type=etype,
        size=size,
        link_target=link,
        compressed_size=compressed,
        index=index,
    )


def plan(entries: list[ArchiveEntry], policy: SafetyPolicy | None = None) -> ExtractionPlan:
    numbered = [dataclasses.replace(e, index=i) for i, e in enumerate(entries)]
    return plan_extraction(numbered, DEST, policy or SafetyPolicy(), free_space=BIG)


# --- path normalisation ---------------------------------------------------------------------

BLOCKED_NAMES = [
    ("../evil", "path-traversal"),
    ("a/../../evil", "path-traversal"),
    ("a/../b", "path-traversal"),
    ("..", "path-traversal"),
    ("a/..", "path-traversal"),
    ("..\\evil", "path-traversal"),
    ("a\\..\\..\\evil", "path-traversal"),
    ("a/b/../../../x", "path-traversal"),
    ("/abs", "absolute-path"),
    ("/", "absolute-path"),
    ("\\abs", "absolute-path"),
    ("C:\\abs", "absolute-path"),
    ("c:/abs", "absolute-path"),
    ("D:evil", "absolute-path"),
    ("\\\\srv\\share\\x", "absolute-path"),
    ("//srv/share/x", "absolute-path"),
    ("\\\\?\\C:\\x", "absolute-path"),
    ("a\0b", "invalid-name"),
    ("\0", "invalid-name"),
    ("", "empty-name"),
    (".", "empty-name"),
    ("./", "empty-name"),
    ("././.", "empty-name"),
]


@pytest.mark.parametrize(("name", "code"), BLOCKED_NAMES)
def test_split_entry_name_blocks(name: str, code: str) -> None:
    parts, got, _ = split_entry_name(name)
    assert parts is None
    assert got == code


PLAN_BLOCKED = [(n, c) for n, c in BLOCKED_NAMES if n != "D:evil"]


@pytest.mark.parametrize(("name", "code"), PLAN_BLOCKED)
def test_plan_blocks_hostile_file_names(name: str, code: str) -> None:
    result = plan([entry(name)])
    assert result.planned == []
    assert [i.code for i in result.issues] == [code]
    assert result.issues[0].severity == "error"
    assert result.skipped == 1


def test_drive_relative_name_is_left_to_the_renaming_step_when_sanitising() -> None:
    assert split_entry_name("x:y", strict_drive=False)[0] == ("x:y",)
    assert split_entry_name("C:\\x", strict_drive=False)[1] == "absolute-path"
    assert split_entry_name("C:", strict_drive=False)[1] == "absolute-path"
    result = plan([entry("x:y")], SafetyPolicy(sanitize_windows_names="always"))
    assert [p.parts for p in result.planned] == [("x:y",)]


def test_drive_relative_name_is_blocked_on_windows_without_sanitising() -> None:
    result = plan([entry("D:evil")], SafetyPolicy(sanitize_windows_names="never"))
    assert bool(result.planned) is (os.name != "nt")


@pytest.mark.parametrize(
    ("name", "parts"),
    [
        ("a/b", ("a", "b")),
        ("./a", ("a",)),
        ("a//b", ("a", "b")),
        ("a\\b", ("a", "b")),
        ("a/./b/", ("a", "b")),
        ("dir/file.txt", ("dir", "file.txt")),
        ("..hidden/x", ("..hidden", "x")),
        ("a..b", ("a..b",)),
        ("\u0645\u0644\u0641/\u0645.txt", ("\u0645\u0644\u0641", "\u0645.txt")),
    ],
)
def test_split_entry_name_accepts(name: str, parts: tuple[str, ...]) -> None:
    assert split_entry_name(name) == (parts, "", "")


@pytest.mark.parametrize("name", [".", "./", ""])
def test_root_directory_entries_are_skipped_silently(name: str) -> None:
    result = plan([entry(name, EntryType.DIR)])
    assert result.planned == [] and result.issues == [] and result.skipped == 0


@pytest.mark.parametrize(
    ("name", "parts"),
    [("/abs/file", ("abs", "file")), ("C:\\x\\y", ("x", "y")), ("c:/q", ("q",))],
)
def test_allow_absolute_strips_the_anchor(name: str, parts: tuple[str, ...]) -> None:
    assert split_entry_name(name, allow_absolute=True)[0] == parts


@pytest.mark.parametrize("name", ["/../x", "C:\\..\\x", "../x"])
def test_allow_absolute_never_allows_traversal(name: str) -> None:
    assert split_entry_name(name, allow_absolute=True)[0] is None


# --- links and special files ----------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["../outside", "../../x", "/etc/passwd", "C:\\Windows", "..\\x", "a/../../x", "", "\\\\srv\\s"],
)
def test_symlinks_leaving_the_destination_are_blocked(target: str) -> None:
    result = plan([entry("link", EntryType.SYMLINK, link=target)])
    assert result.planned == []
    assert result.issues[0].code == "symlink-escape"


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("link", "file.txt"),
        ("link", "./file.txt"),
        ("d/link", "../file.txt"),
        ("d/link", "sub/file.txt"),
        ("a/b/link", "../../x"),
        ("link", "dir/../other"),
    ],
)
def test_symlinks_staying_inside_are_planned(name: str, target: str) -> None:
    result = plan([entry(name, EntryType.SYMLINK, link=target)])
    assert len(result.planned) == 1
    assert result.issues == []


def test_symlink_deeper_target_that_escapes_from_nested_location() -> None:
    result = plan([entry("a/b/link", EntryType.SYMLINK, link="../../..")])
    assert result.issues[0].code == "symlink-escape"


def test_file_through_a_symlink_entry_is_blocked() -> None:
    result = plan(
        [entry("link", EntryType.SYMLINK, link="sub"), entry("link/file.txt")],
    )
    assert [p.entry.name for p in result.planned] == ["link"]
    assert [i.code for i in result.issues] == ["symlink-traversal"]


def test_escaping_link_and_the_file_behind_it_are_both_blocked() -> None:
    result = plan(
        [entry("link", EntryType.SYMLINK, link="../outside"), entry("link/file", size=3)],
    )
    assert result.planned == []
    assert sorted(i.code for i in result.issues) == ["symlink-escape", "symlink-traversal"]


def test_file_before_its_symlink_is_blocked_too() -> None:
    result = plan([entry("link/file"), entry("link", EntryType.SYMLINK, link="sub")])
    assert [p.entry.name for p in result.planned] == ["link"]


def test_chains_of_links_pointing_at_links_are_allowed() -> None:
    result = plan(
        [
            entry("libx.so", EntryType.SYMLINK, link="libx.so.1"),
            entry("libx.so.1", EntryType.SYMLINK, link="libx.so.1.2"),
            entry("libx.so.1.2"),
        ]
    )
    assert len(result.planned) == 3 and result.issues == []


def test_link_that_walks_back_through_another_link_is_blocked() -> None:
    result = plan(
        [
            entry("a", EntryType.SYMLINK, link="."),
            entry("b", EntryType.SYMLINK, link="a/sub/../.."),
        ]
    )
    assert [p.entry.name for p in result.planned] == ["a"]
    assert result.issues[0].code == "symlink-escape"


def test_symlinks_can_be_disabled() -> None:
    result = plan([entry("l", EntryType.SYMLINK, link="x")], SafetyPolicy(allow_symlinks=False))
    assert result.planned == []
    assert result.issues[0].code == "symlink-disallowed"
    assert result.issues[0].severity == "warning"


@pytest.mark.parametrize("target", ["../x", "/abs", "C:\\x", "a/../../x", ""])
def test_hardlinks_to_outside_are_blocked(target: str) -> None:
    result = plan([entry("h", EntryType.HARDLINK, link=target)])
    assert result.planned == []
    assert result.issues[0].code == "hardlink-escape"


def test_hardlink_inside_is_planned_with_parts() -> None:
    result = plan([entry("h", EntryType.HARDLINK, link="dir/file")])
    assert result.planned[0].link_parts == ("dir", "file")


def test_hardlink_through_symlink_is_blocked() -> None:
    result = plan(
        [entry("s", EntryType.SYMLINK, link="x"), entry("h", EntryType.HARDLINK, link="s/f")]
    )
    assert [p.entry.name for p in result.planned] == ["s"]
    assert result.issues[0].code == "hardlink-escape"


def test_devices_and_fifos_are_skipped_with_a_warning() -> None:
    result = plan([entry("dev", EntryType.OTHER), entry("ok.txt")])
    assert [p.entry.name for p in result.planned] == ["ok.txt"]
    assert result.issues[0].code == "special-file"
    assert result.issues[0].severity == "warning"
    assert result.skipped == 1


# --- declared limits ------------------------------------------------------------------------


def test_too_many_entries_is_refused() -> None:
    entries = [entry(f"f{i}", EntryType.FILE, size=0) for i in range(2000)]
    with pytest.raises(BombDetected) as info:
        plan(entries, SafetyPolicy(max_entries=1000))
    assert info.value.code == "too-many-entries"


def test_entry_count_at_the_limit_is_fine() -> None:
    entries = [entry(f"f{i}", size=0) for i in range(50)]
    assert len(plan(entries, SafetyPolicy(max_entries=50)).planned) == 50


def test_per_entry_size_limit() -> None:
    with pytest.raises(BombDetected) as info:
        plan([entry("big", size=101)], SafetyPolicy(max_entry_bytes=100))
    assert info.value.code == "entry-too-large"


def test_total_size_limit() -> None:
    entries = [entry("a", size=60), entry("b", size=60)]
    with pytest.raises(BombDetected) as info:
        plan(entries, SafetyPolicy(max_total_bytes=100))
    assert info.value.code == "size-limit"


def test_total_size_exactly_at_the_limit_passes() -> None:
    assert plan([entry("a", size=100)], SafetyPolicy(max_total_bytes=100)).total_bytes == 100


def test_declared_total_must_fit_on_disk() -> None:
    with pytest.raises(IOFailure) as info:
        plan_extraction([entry("a", size=500)], DEST, SafetyPolicy(), free_space=100)
    assert info.value.code == "disk-full"


def test_explicit_total_limit_replaces_the_disk_check() -> None:
    policy = SafetyPolicy(max_total_bytes=1000)
    assert plan_extraction([entry("a", size=500)], DEST, policy, free_space=1).total_bytes == 500


@pytest.mark.parametrize(
    ("size", "compressed", "bomb"),
    [
        (10_000, 10, True),  # ratio 1000
        (10_000, 40, False),  # ratio 250 is allowed (limit is exclusive)
        (10_000, 39, True),
        (50, 1, False),  # under ratio_min_bytes
        (10_000, 0, True),  # zero-byte payload still counts as one byte
    ],
)
def test_ratio_limit_per_entry(size: int, compressed: int, bomb: bool) -> None:
    policy = SafetyPolicy(ratio_min_bytes=100)
    entries = [entry("z", size=size, compressed=compressed)]
    if bomb:
        with pytest.raises(BombDetected) as info:
            plan(entries, policy)
        assert info.value.code == "ratio"
    else:
        assert len(plan(entries, policy).planned) == 1


def test_ratio_limit_over_the_whole_archive() -> None:
    entries = [entry(f"z{i}", size=900, compressed=10) for i in range(5)]
    with pytest.raises(BombDetected):
        plan(entries, SafetyPolicy(ratio_min_bytes=1000, max_ratio=50))


def test_ratio_is_configurable() -> None:
    entries = [entry("z", size=10_000, compressed=10)]
    assert plan(entries, SafetyPolicy(ratio_min_bytes=100, max_ratio=5000)).planned


def test_blocked_entries_do_not_count_towards_limits() -> None:
    result = plan(
        [entry("../evil", size=10**9), entry("ok", size=5)], SafetyPolicy(max_total_bytes=100)
    )
    assert result.total_bytes == 5


@pytest.mark.parametrize(
    ("mode", "is_windows", "expected"),
    [
        ("auto", True, True),
        ("auto", False, False),
        ("always", False, True),
        ("always", True, True),
        ("never", True, False),
        ("never", False, False),
    ],
)
def test_windows_name_policy(mode: str, is_windows: bool, expected: bool) -> None:
    policy = SafetyPolicy(sanitize_windows_names=mode)  # type: ignore[arg-type]
    assert policy.sanitize_windows(is_windows=is_windows) is expected


# --- byte budget ----------------------------------------------------------------------------


def test_budget_allows_exactly_the_declared_size() -> None:
    budget = ByteBudget(None)
    budget.begin(entry("a", size=10))
    budget.add(6)
    budget.add(4)
    assert budget.total == 10


def test_budget_aborts_when_an_entry_outgrows_its_header() -> None:
    budget = ByteBudget(None)
    budget.begin(entry("a", size=10))
    budget.add(10)
    with pytest.raises(BombDetected) as info:
        budget.add(1)
    assert info.value.code == "size-overrun"


def test_budget_resets_per_entry_but_accumulates_total() -> None:
    budget = ByteBudget(25)
    budget.begin(entry("a", size=10))
    budget.add(10)
    budget.begin(entry("b", size=10))
    budget.add(10)
    budget.begin(entry("c", size=10))
    with pytest.raises(BombDetected) as info:
        budget.add(6)
    assert info.value.code == "size-limit"


def test_budget_unknown_size_is_limited_by_total_only() -> None:
    budget = ByteBudget(100)
    budget.begin(entry("a", size=-1))
    budget.add(100)
    with pytest.raises(BombDetected):
        budget.add(1)


def test_budget_per_entry_cap() -> None:
    budget = ByteBudget(None, max_entry=5)
    budget.begin(entry("a", size=-1))
    with pytest.raises(BombDetected):
        budget.add(6)


def test_budget_cap_never_raises_the_declared_size() -> None:
    budget = ByteBudget(None, max_entry=1000)
    budget.begin(entry("a", size=3))
    with pytest.raises(BombDetected):
        budget.add(4)
