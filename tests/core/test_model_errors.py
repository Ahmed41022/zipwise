from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path

import pytest

from zipwise import errors
from zipwise.errors import ExitCode
from zipwise.model import (
    ArchiveEntry,
    ArchiveInfo,
    ConflictAction,
    CreateOptions,
    EntryType,
    ExtractOptions,
    FormatId,
    Issue,
    OperationReport,
)
from zipwise.safety import SafetyPolicy

EXIT_CODES = [
    (errors.UsageError, 2),
    (errors.UnsupportedFormat, 3),
    (errors.BackendUnavailable, 3),
    (errors.CorruptArchive, 4),
    (errors.SafetyViolation, 5),
    (errors.BombDetected, 5),
    (errors.PasswordRequired, 6),
    (errors.WrongPassword, 6),
    (errors.IOFailure, 7),
    (errors.Cancelled, 130),
]


@pytest.mark.parametrize(("cls", "code"), EXIT_CODES)
def test_exit_code_per_exception(cls: type[errors.ZipwiseError], code: int) -> None:
    assert cls("boom").exit_code == code


@pytest.mark.parametrize(("cls", "_"), EXIT_CODES)
def test_every_error_has_stable_code_message_and_hint(
    cls: type[errors.ZipwiseError], _: int
) -> None:
    exc = cls("something happened", hint="try again") if cls is not errors.Cancelled else cls()
    data = exc.to_dict()
    assert set(data) == {"code", "message", "hint"}
    assert data["code"] == cls.code
    assert isinstance(exc, errors.ZipwiseError)


def test_bomb_is_a_safety_violation() -> None:
    assert issubclass(errors.BombDetected, errors.SafetyViolation)


def test_error_code_can_be_overridden_per_instance() -> None:
    exc = errors.IOFailure("full", code="disk-full")
    assert exc.code == "disk-full"
    assert errors.IOFailure.code == "io-failure"


def test_exit_code_enum_values() -> None:
    assert [int(c) for c in ExitCode] == [0, 1, 2, 3, 4, 5, 6, 7, 130]


@pytest.mark.parametrize("fmt", list(FormatId), ids=lambda f: f.name)
def test_format_ids_are_unique_strings(fmt: FormatId) -> None:
    assert isinstance(fmt.value, str)
    assert FormatId(fmt.value) is fmt


def test_format_id_has_twelve_members() -> None:
    assert len(FormatId) == 12


def test_entry_to_dict_is_json_serialisable() -> None:
    entry = ArchiveEntry(
        name="a/b.txt",
        type=EntryType.FILE,
        size=3,
        compressed_size=5,
        mtime=datetime(2024, 1, 2, 3, 4, 5),
        mode=0o644,
        crc=123,
        index=4,
    )
    data = json.loads(json.dumps(entry.to_dict()))
    assert data["name"] == "a/b.txt"
    assert data["type"] == "file"
    assert data["mtime"] == "2024-01-02T03:04:05"
    assert data["index"] == 4


def test_entry_defaults() -> None:
    entry = ArchiveEntry(name="x")
    assert entry.type is EntryType.FILE
    assert entry.raw_name is None
    assert entry.link_target is None
    assert entry.encrypted is False


def test_info_to_dict_is_json_serialisable() -> None:
    info = ArchiveInfo(Path("a.zip"), FormatId.ZIP, 2, 10, 5, False, "utf-8", 0.9)
    data = json.loads(json.dumps(info.to_dict()))
    assert data["format"] == "zip"
    assert data["multipart"] is False
    assert data["name_encoding"] == "utf-8"


def test_issue_to_dict() -> None:
    issue = Issue("error", "path-traversal", "../x", "blocked")
    assert issue.to_dict() == {
        "severity": "error",
        "code": "path-traversal",
        "entry": "../x",
        "message": "blocked",
    }


@pytest.mark.parametrize(
    ("severities", "ok", "exit_code"),
    [
        ([], True, 0),
        (["info"], True, 0),
        (["info", "warning"], True, 1),
        (["warning"], True, 1),
        (["error"], False, 1),
        (["info", "error", "warning"], False, 1),
    ],
)
def test_report_ok_and_exit_code(severities: list[str], ok: bool, exit_code: int) -> None:
    issues = [Issue(s, "c", None, "m") for s in severities]  # type: ignore[arg-type]
    report = OperationReport.build(issues)
    assert report.ok is ok
    assert int(report.exit_code) == exit_code


def test_report_to_dict_round_trips_through_json() -> None:
    report = OperationReport.build(
        [Issue("warning", "skipped-existing", "a", "skipped")],
        entries_done=3,
        entries_skipped=1,
        bytes_done=42,
        output_paths=[Path("out")],
        elapsed=1.23456789,
    )
    data = json.loads(json.dumps(report.to_dict()))
    assert data["ok"] is True
    assert data["entries_done"] == 3
    assert data["output_paths"] == ["out"]
    assert data["issues"][0]["code"] == "skipped-existing"
    assert data["elapsed"] == 1.234568


@pytest.mark.parametrize(
    "obj",
    [
        ArchiveEntry(name="x"),
        Issue("info", "c", None, "m"),
        OperationReport.build([]),
        CreateOptions(),
        ExtractOptions(),
    ],
    ids=lambda o: type(o).__name__,
)
def test_dataclasses_are_frozen(obj: object) -> None:
    field = dataclasses.fields(obj)[0].name  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(obj, field, None)


def test_create_options_defaults() -> None:
    o = CreateOptions()
    assert o.format is FormatId.ZIP
    assert o.level == "normal"
    assert o.password is None
    assert o.smart_store is True
    assert o.verify is True
    assert o.split_size is None
    assert o.follow_symlinks is False


def test_extract_options_defaults() -> None:
    o = ExtractOptions()
    assert isinstance(o.policy, SafetyPolicy)
    assert o.on_conflict is ConflictAction.ASK
    assert o.resume is False
    assert o.restore_mode is True
    assert o.strip_components == 0
    assert o.only is None
