"""Parser vocabulary, argument errors and the pure formatting helpers."""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pytest
from cli_support import Run, RunFn

from helpers.names import extracted_files
from zipwise import __version__
from zipwise.cli.commands import archive_stem
from zipwise.cli.parser import (
    COMMAND_NAMES,
    FORMAT_SUFFIXES,
    PASSWORD_COMMANDS,
    PASSWORD_PROMPT,
    build_parser,
    format_arg,
    format_from_name,
    level_arg,
)
from zipwise.cli.render import (
    ProgressBar,
    Result,
    arrow,
    format_listing,
    human_size,
    issue_line,
    listing_footer,
    plural,
    result_document,
    saving_percent,
)
from zipwise.jobs import ProgressEvent
from zipwise.model import ArchiveEntry, EntryType, FormatId, Issue

INFERENCE = [
    ("a.zip", FormatId.ZIP),
    ("a.ZIP", FormatId.ZIP),
    ("a.7z", FormatId.SEVENZ),
    ("a.tar", FormatId.TAR),
    ("a.tar.gz", FormatId.TAR_GZ),
    ("a.tgz", FormatId.TAR_GZ),
    ("a.tar.bz2", FormatId.TAR_BZ2),
    ("a.tbz2", FormatId.TAR_BZ2),
    ("a.tar.xz", FormatId.TAR_XZ),
    ("a.txz", FormatId.TAR_XZ),
    ("a.tar.zst", FormatId.TAR_ZST),
    ("a.gz", FormatId.GZ),
    ("a.bz2", FormatId.BZ2),
    ("a.xz", FormatId.XZ),
    ("a.zst", FormatId.ZST),
    ("my.backup.tar.gz", FormatId.TAR_GZ),
    ("a.txt", None),
    ("noext", None),
]


@pytest.mark.parametrize(("name", "expected"), INFERENCE)
def test_format_inference_table(name: str, expected: FormatId | None) -> None:
    assert format_from_name(name) is expected


def test_every_suffix_has_a_format() -> None:
    assert {fmt for _, fmt in FORMAT_SUFFIXES} >= set(FormatId)


@pytest.mark.parametrize(("text", "expected"), [("tgz", FormatId.TAR_GZ), ("7z", FormatId.SEVENZ)])
def test_format_aliases(text: str, expected: FormatId) -> None:
    assert format_arg(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("fast", "fast"), ("BEST", "best"), ("normal", "normal"), ("0", 0), ("9", 9)],
)
def test_level_values(text: str, expected: str | int) -> None:
    assert level_arg(text) == expected


@pytest.mark.parametrize("text", ["10", "-1", "ultra", ""])
def test_level_rejects(text: str) -> None:
    with pytest.raises(Exception, match="level"):
        level_arg(text)


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("photos.zip", "photos"),
        ("photos.tar.gz", "photos"),
        ("a.zip.001", "a"),
        ("a.b.7z", "a.b"),
        ("weird.dat", "weird"),
        (".zip", ".zip"),
    ],
)
def test_archive_stem(name: str, stem: str) -> None:
    assert archive_stem(name) == stem


def test_version(run: RunFn) -> None:
    result = run("--version")
    assert result.code == 0
    assert __version__ in result.out


@pytest.mark.parametrize("command", COMMAND_NAMES)
def test_help_has_an_example(run: RunFn, command: str) -> None:
    result = run(command, "--help")
    assert result.code == 0
    assert "example:" in result.out
    assert "zipwise" in result.out


def test_no_command_is_a_usage_error(run: RunFn) -> None:
    result = run()
    assert result.code == 2
    assert "required" in result.err


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "x.zip"],
        ["extract"],
        ["create", "x.zip", "src", "--level", "11"],
        ["create", "x.zip", "src", "--split", "banana"],
        ["split", "f"],
        ["extract", "a.zip", "-o", "d", "--here"],
        ["extract", "a.zip", "--on-conflict", "maybe"],
        ["list", "a.zip", "--max-nothing"],
        ["nonsense"],
    ],
)
def test_usage_errors_exit_2(run: RunFn, argv: list[str]) -> None:
    result = run(*argv)
    assert result.code == 2
    assert "usage:" in result.err and "error:" in result.err
    assert result.out == ""


def test_usage_error_in_json_mode_is_a_document(run: RunFn) -> None:
    result = run("create", "--json", "x.zip")
    assert result.code == 2
    doc = result.json()
    assert doc["ok"] is False
    assert doc["command"] == "create"
    assert set(doc["error"]) == {"code", "message", "hint"}


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("flag", ["--json", "--quiet", "--no-progress"])
def test_global_options_work_before_or_after_the_command(
    run: RunFn, archive, position: str, flag: str
) -> None:
    argv = ["list", str(archive)]
    argv = [flag, *argv] if position == "before" else [*argv, flag]
    result = run(*argv)
    assert result.code == 0
    if flag == "--json":
        assert result.json()["command"] == "list"
    else:
        assert "data/a.txt" in result.out


@pytest.mark.parametrize(
    "argv",
    [
        ["extract", "ARC", "data/a.txt", "-o", "DEST"],
        ["extract", "ARC", "-o", "DEST", "data/a.txt"],
        ["extract", "-o", "DEST", "ARC", "data/a.txt"],
        ["extract", "-o", "DEST", "ARC", "--on-conflict", "overwrite", "data/a.txt"],
        ["--json", "extract", "ARC", "-o", "DEST", "data/a.txt", "--no-progress"],
        ["extract", "ARC", "-o", "DEST", "--json", "data/a.txt"],
    ],
)
def test_entry_names_and_options_in_any_order(
    run: RunFn, archive: Path, tmp_path: Path, argv: list[str]
) -> None:
    dest = tmp_path / "out"
    swap = {"ARC": str(archive), "DEST": str(dest)}
    result = run(*[swap.get(a, a) for a in argv])
    assert result.code == 0, result.err
    assert extracted_files(dest) == ["data/a.txt"]


def test_strip_components_before_names(run: RunFn, archive: Path, tmp_path: Path) -> None:
    dest = tmp_path / "s"
    result = run("extract", archive, "-o", dest, "--strip-components", "1", "data/sub")
    assert result.code == 0, result.err
    assert extracted_files(dest) == ["sub/b.txt", "sub/c.bin"]


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "OUT", "--level", "fast", "DATA"],
        ["create", "--level", "fast", "OUT", "DATA"],
        ["create", "OUT", "DATA", "--level", "fast"],
    ],
)
def test_create_sources_and_options_in_any_order(
    run: RunFn, data: Path, tmp_path: Path, argv: list[str]
) -> None:
    out = tmp_path / "o.zip"
    swap = {"OUT": str(out), "DATA": str(data)}
    assert run(*[swap.get(a, a) for a in argv]).code == 0
    assert out.is_file()


def test_list_and_info_take_the_archive_after_options(run: RunFn, archive: Path) -> None:
    assert run("list", "-l", "--encoding", "utf-8", archive).code == 0
    assert run("info", "--encoding", "utf-8", archive).code == 0


def test_unknown_option_after_names_is_still_an_error(run: RunFn, archive: Path) -> None:
    result = run("extract", archive, "data/a.txt", "--bogus")
    assert result.code == 2 and "--bogus" in result.err


def test_missing_positional_with_intermixed_options_in_json(run: RunFn) -> None:
    result = run("create", "--level", "fast", "--json", "x.zip")
    assert result.code == 2
    doc = result.json()
    assert doc["command"] == "create" and doc["error"]["code"] == "usage"


@pytest.mark.parametrize("command", PASSWORD_COMMANDS)
def test_password_commands_accept_both_password_flags(command: str) -> None:
    parser = build_parser()
    positional = ["a.zip", "src"] if command == "create" else ["a.zip"]
    args = parser.parse_args([command, *positional, "--password-file", "pw.txt"])
    assert args.password_file == "pw.txt"
    bare = parser.parse_args([command, *positional, "-p"])
    assert bare.password == PASSWORD_PROMPT
    before = parser.parse_args([command, "--password=x", *positional])
    assert before.password == "x" and before.archive == "a.zip"


# -- render helpers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("packed", "whole", "text"),
    [(25, 100, "25%"), (0, 100, "0%"), (995, 1000, "99%"), (100, 100, None), (234, 4, None)],
)
def test_saving_percent(packed: int, whole: int, text: str | None) -> None:
    assert saving_percent(packed, whole) == text
    assert saving_percent(5, 0) is None


def test_listing_hides_the_ratio_when_nothing_was_saved() -> None:
    entries = [_entry("tiny.txt", size=7, compressed_size=9)]
    row = format_listing(entries, long=True)[1]
    assert "%" not in row and row.split()[4] == "-"
    assert listing_footer(entries) == "1 entry, 7 B, 9 B packed"


@pytest.mark.parametrize(
    ("size", "text"),
    [(0, "0 B"), (999, "999 B"), (1000, "1.0 KB"), (1_500_000, "1.5 MB"), (3 * 10**9, "3.0 GB")],
)
def test_human_size(size: int, text: str) -> None:
    assert human_size(size) == text


def test_plural() -> None:
    assert plural(1, "entry") == "1 entry"
    assert plural(2, "entry") == "2 entries"
    assert plural(3, "part") == "3 parts"


def test_arrow_falls_back_on_narrow_encodings() -> None:
    narrow = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    wide = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    assert arrow(narrow) == "->"
    assert arrow(wide) == "→"


def test_issue_line_formats() -> None:
    assert issue_line(Issue("warning", "x", "a/b", "skipped")) == "warning: a/b: skipped"
    assert issue_line(Issue("error", "x", None, "boom")) == "error: boom"


def _entry(name: str, **kw: object) -> ArchiveEntry:
    return ArchiveEntry(name=name, **kw)  # type: ignore[arg-type]


def test_listing_names_only_and_long() -> None:
    entries = [
        _entry("d", type=EntryType.DIR),
        _entry(
            "d/f.txt",
            size=2000,
            compressed_size=500,
            mtime=datetime(2024, 3, 5, 12, 30),
            encrypted=True,
        ),
        _entry("l", type=EntryType.SYMLINK, link_target="d/f.txt"),
    ]
    assert format_listing(entries, long=False) == ["d", "d/f.txt", "l -> d/f.txt"]
    long = format_listing(entries, long=True)
    assert long[0].split()[:3] == ["Size", "Packed", "Ratio"]
    row = long[2]
    assert "2.0 KB" in row and "25%" in row and "2024-03-05 12:30" in row and "-E" in row
    assert listing_footer(entries).startswith("3 entries, 2.0 KB")


def test_result_document_shapes() -> None:
    ok = result_document(Result("x", data={"n": 1}))
    assert list(ok) == ["ok", "command", "n", "exit_code"]
    bad = result_document(
        Result("x", exit_code=4, ok=False, error={"code": "c", "message": "m", "hint": None})
    )
    assert bad["error"] == {"code": "c", "message": "m", "hint": None}
    assert bad["ok"] is False and bad["exit_code"] == 4


def test_progress_bar_truncates_and_clears() -> None:
    stream = io.StringIO()
    bar = ProgressBar(stream, columns=50)
    event = ProgressEvent("compress", "a/very/long/path/" * 5 + "file.txt", 500, 1000, 1, 2)
    bar(event)
    line = stream.getvalue().lstrip("\r")
    assert len(line) <= 49
    assert "50%" in line and "..." in line and line.endswith("file.txt")
    bar.clear()
    assert stream.getvalue().endswith("\r")


def test_progress_bar_uses_entries_without_byte_total() -> None:
    bar = ProgressBar(io.StringIO(), columns=80)
    assert "1/4" in bar.format(ProgressEvent("scan", "", 0, 0, 1, 4))


def _unused(_: Run) -> None:  # keeps the shared import honest for type checkers
    return None
