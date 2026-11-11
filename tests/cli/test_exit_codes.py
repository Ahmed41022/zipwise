"""One test per exit code, cancellation, internal errors and the subprocess smoke tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from cli_support import RunFn

from helpers.builders import file_entry, write_raw_zip, write_stored_rar4
from helpers.names import ARABIC, legacy_zip
from helpers.trees import make_tree
from zipwise.cli import commands
from zipwise.cli.main import main
from zipwise.jobs import CancelToken, JobContext


def test_exit_0(run: RunFn, archive: Path) -> None:
    assert run("test", archive).code == 0


def test_exit_1_for_a_blocked_entry(run: RunFn, tmp_path: Path) -> None:
    evil = write_raw_zip(tmp_path / "evil.zip", [file_entry("ok.txt"), file_entry("../evil.txt")])
    dest = tmp_path / "inner" / "dest"
    result = run("extract", evil, "-o", dest)
    assert result.code == 1
    assert "evil.txt" in result.err and "error:" in result.err
    assert (dest / "ok.txt").is_file()
    assert not (tmp_path / "inner" / "evil.txt").exists()
    assert not (tmp_path / "evil.txt").exists()


def test_exit_1_json_for_a_blocked_entry(run: RunFn, tmp_path: Path) -> None:
    evil = write_raw_zip(tmp_path / "evil.zip", [file_entry("ok.txt"), file_entry("a/../../x")])
    result = run("extract", "--json", evil, "-o", tmp_path / "d")
    doc = result.json()
    assert result.code == 1 and doc["ok"] is False and doc["exit_code"] == 1
    assert doc["entries_skipped"] == 1 and doc["issues"][0]["severity"] == "error"


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "{t}/x.zip"],
        ["list"],
        ["extract", "{t}/missing.zip"],
        ["info", "{t}/missing.zip"],
    ],
)
def test_exit_2(run: RunFn, tmp_path: Path, argv: list[str]) -> None:
    assert run(*[a.format(t=tmp_path) for a in argv]).code == 2


def test_exit_2_for_a_missing_archive_in_json(run: RunFn, tmp_path: Path) -> None:
    result = run("list", "--json", tmp_path / "missing.zip")
    doc = result.json()
    assert result.code == 2 and doc["error"]["code"] == "usage"
    assert result.out.count("{") >= 1 and result.err.count("error:") == 1


def test_exit_3_for_an_unknown_file(run: RunFn, tmp_path: Path) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_text("just text, not an archive")
    result = run("list", junk)
    assert result.code == 3 and "error:" in result.err
    assert run("extract", junk, "-o", tmp_path / "o").code == 3


def test_exit_3_for_rar_without_a_tool(
    run: RunFn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rar = write_stored_rar4(tmp_path / "s.rar", {"a.txt": b"hello"})
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = run("extract", rar, "-o", tmp_path / "o")
    assert result.code == 3
    assert result.err.startswith("error:") and "hint:" in result.err


def test_exit_3_for_creating_rar(run: RunFn, data: Path, tmp_path: Path) -> None:
    assert run("create", "-f", "rar", tmp_path / "x.rar", data).code == 3


def test_exit_4_for_a_corrupted_archive(run: RunFn, archive: Path, tmp_path: Path) -> None:
    broken = tmp_path / "b.zip"
    broken.write_bytes(archive.read_bytes()[: len(archive.read_bytes()) // 2])
    assert run("test", broken).code == 4


def test_exit_5_for_a_bomb_by_size(run: RunFn, tmp_path: Path) -> None:
    bomb = write_raw_zip(tmp_path / "bomb.zip", [file_entry("zeros.bin", b"\0" * 5_000_000)])
    assert bomb.stat().st_size < 20_000
    dest = tmp_path / "out"
    result = run("extract", bomb, "-o", dest, "--max-size", "1000")
    assert result.code == 5
    assert not (dest / "zeros.bin").exists()


def test_exit_5_json_error_document(run: RunFn, tmp_path: Path) -> None:
    bomb = write_raw_zip(tmp_path / "bomb.zip", [file_entry("zeros.bin", b"\0" * 5_000_000)])
    result = run("extract", "--json", bomb, "-o", tmp_path / "o", "--max-size", "1K")
    doc = result.json()
    assert result.code == 5 and doc["ok"] is False
    assert set(doc["error"]) == {"code", "message", "hint"}


def test_exit_5_for_too_many_entries(run: RunFn, tmp_path: Path) -> None:
    many = write_raw_zip(tmp_path / "many.zip", [file_entry(f"f{i}.txt", b"") for i in range(30)])
    assert run("extract", many, "-o", tmp_path / "o", "--max-entries", "5").code == 5


def test_exit_6_wrong_and_missing_password(run: RunFn, data: Path, tmp_path: Path) -> None:
    out = tmp_path / "p.zip"
    assert run("create", "--password=right", out, data).code == 0
    assert run("test", out, "--password=wrong").code == 6
    assert run("test", out).code == 6


def test_exit_7_for_an_unwritable_destination(run: RunFn, data: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    result = run("create", blocker / "sub" / "out.zip", data)
    assert result.code == 7
    assert "error:" in result.err


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="POSIX"
)
def test_exit_7_for_a_read_only_folder(run: RunFn, archive: Path, tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert run("extract", archive, "-o", locked / "x").code == 7
    finally:
        locked.chmod(0o700)


def test_stray_oserror_becomes_exit_7(
    run: RunFn, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(args: object, env: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setitem(commands.HANDLERS, "info", boom)
    assert run("info", archive).code == 7


# -- cancellation ---------------------------------------------------------------------------


def _cancelled_context(env: object) -> JobContext:
    token = CancelToken()
    token.cancel()
    return JobContext(token)


@pytest.mark.parametrize("command", ["create", "extract", "list", "test", "info", "split", "join"])
def test_exit_130_when_the_job_is_cancelled(
    run: RunFn,
    data: Path,
    archive: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"x" * 5000)
    parts = tmp_path / "parts"
    run("split", blob, "--size", "2000", "-o", parts)
    monkeypatch.setattr(commands, "make_context", _cancelled_context)
    argv = {
        "create": ["create", tmp_path / "c.zip", data],
        "extract": ["extract", archive, "-o", tmp_path / "x"],
        "list": ["list", archive],
        "test": ["test", archive],
        "info": ["info", archive],
        "split": ["split", blob, "--size", "1000", "-o", tmp_path / "p2"],
        "join": ["join", parts / "blob.bin.001", "-o", tmp_path / "j.bin"],
    }[command]
    result = run(*argv)
    assert result.code == 130
    assert "cancelled" in result.err.lower()


def test_cancelled_create_leaves_nothing_behind(
    run: RunFn, data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from helpers.jobs import stepping_context

    ctx, _, _ = stepping_context(cancel_on="data/sub/b.txt")
    monkeypatch.setattr(commands, "make_context", lambda env: ctx)
    out = tmp_path / "gone.zip"
    assert run("create", out, data).code == 130
    assert not out.exists() and not list(tmp_path.glob("gone.zip*"))


def test_cancelled_json_document(
    run: RunFn, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "make_context", _cancelled_context)
    result = run("test", "--json", archive)
    assert result.code == 130
    assert result.json()["error"]["code"] == "cancelled"


def test_keyboard_interrupt_exits_130(
    run: RunFn, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(args: object, env: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setitem(commands.HANDLERS, "test", interrupt)
    assert run("test", archive).code == 130


def test_sigint_handler_cancels_the_token_and_is_restored(
    archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = signal.getsignal(signal.SIGINT)
    seen: dict[str, bool] = {}

    def handler_probe(args: object, env: commands.RunEnv) -> commands.Result:
        signal.raise_signal(signal.SIGINT)  # what Ctrl-C does
        seen["cancelled"] = env.cancel.cancelled
        env.cancel.raise_if_cancelled()
        return commands.Result("test")

    monkeypatch.setitem(commands.HANDLERS, "test", handler_probe)
    assert main(["test", str(archive)]) == 130
    assert seen["cancelled"] is True
    assert signal.getsignal(signal.SIGINT) is before


# -- internal errors ------------------------------------------------------------------------


def _explode(args: object, env: object) -> None:
    raise ZeroDivisionError("kaboom")


def test_internal_error_is_one_line_and_exit_70(
    run: RunFn, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(commands.HANDLERS, "info", _explode)
    result = run("info", archive)
    assert result.code == 70
    assert result.err.strip().splitlines() == ["internal error: ZeroDivisionError: kaboom"]
    assert result.out == ""


def test_internal_error_traceback_only_in_debug_mode(
    run: RunFn, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(commands.HANDLERS, "info", _explode)
    monkeypatch.setenv("ZIPWISE_DEBUG", "1")
    result = run("info", archive)
    assert result.code == 70 and "Traceback" in result.err


def test_internal_error_in_json_mode(
    run: RunFn, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(commands.HANDLERS, "info", _explode)
    result = run("info", "--json", archive)
    doc = result.json()
    assert result.code == 70 and doc["error"]["code"] == "internal-error"


# -- subprocess smoke tests -----------------------------------------------------------------


def _zipwise(tmp: Path, *args: object, **env_extra: str) -> subprocess.CompletedProcess[bytes]:
    config = tmp / "cfg"
    config.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "APPDATA": str(config),
        "XDG_CONFIG_HOME": str(config),
        "PYTHONPATH": os.pathsep.join(
            [
                p
                for p in (str(Path(__file__).parents[2] / "src"), os.environ.get("PYTHONPATH", ""))
                if p
            ]
        ),
        **env_extra,
    }
    return subprocess.run(
        [sys.executable, "-m", "zipwise", *map(str, args)],
        cwd=tmp,
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_subprocess_create_test_list(tmp_path: Path) -> None:
    src = make_tree(tmp_path / "dir", {"a.txt": b"hi", "s/b.txt": b"there"})
    created = _zipwise(tmp_path, "create", "out.zip", src)
    assert created.returncode == 0, created.stderr
    tested = _zipwise(tmp_path, "test", "out.zip")
    assert tested.returncode == 0 and tested.stdout.decode().startswith("OK")
    listed = _zipwise(tmp_path, "list", "--json", "out.zip")
    doc = json.loads(listed.stdout)
    assert listed.returncode == 0 and doc["entries_count"] == 4
    assert {"dir/a.txt", "dir/s/b.txt"} <= {e["name"] for e in doc["entries"]}


def test_subprocess_version_and_usage_error(tmp_path: Path) -> None:
    version = _zipwise(tmp_path, "--version")
    assert version.returncode == 0 and version.stdout.decode().startswith("zipwise ")
    bad = _zipwise(tmp_path, "create")
    assert bad.returncode == 2 and bad.stdout == b"" and b"usage:" in bad.stderr


def test_subprocess_json_error_document(tmp_path: Path) -> None:
    result = _zipwise(tmp_path, "info", "--json", "nothing.zip")
    doc = json.loads(result.stdout)
    assert result.returncode == 2 and doc["ok"] is False and doc["command"] == "info"


def test_subprocess_arabic_names_survive_any_console(tmp_path: Path) -> None:
    name = "ملف عربي.txt"
    tree = make_tree(tmp_path / "t", {name: b"x"})
    assert _zipwise(tmp_path, "create", "ar.zip", tree).returncode == 0
    as_json = _zipwise(tmp_path, "list", "--json", "ar.zip", PYTHONIOENCODING="cp1252")
    assert as_json.returncode == 0
    assert name.encode("utf-8") in as_json.stdout  # real UTF-8, not \\u escapes
    assert name in json.loads(as_json.stdout)["entries"][1]["name"]
    as_text = _zipwise(tmp_path, "list", "ar.zip", PYTHONIOENCODING="ascii")
    assert as_text.returncode == 0 and b"UnicodeEncodeError" not in as_text.stderr
    assert b"\\u0645" in as_text.stdout


def test_subprocess_legacy_names_do_not_crash_a_narrow_console(tmp_path: Path) -> None:
    legacy = legacy_zip(tmp_path / "old.zip", ARABIC, "cp1256")
    result = _zipwise(tmp_path, "list", legacy, PYTHONIOENCODING="cp1252")
    assert result.returncode == 0 and b"Traceback" not in result.stderr
