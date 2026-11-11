"""Every command through ``main(argv)``, in text and JSON mode."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from cli_support import FakeTTY, RunFn

from helpers.names import ARABIC, extracted_files, legacy_zip
from helpers.trees import make_tree, random_bytes
from zipwise.model import CreateOptions, FormatId
from zipwise.ops import create_archive

# -- create ---------------------------------------------------------------------------------


def test_create_text_summary(run: RunFn, data: Path, tmp_path: Path) -> None:
    out = tmp_path / "t.zip"
    result = run("create", out, data)
    assert result.code == 0
    assert result.out.startswith(f"Created {out}")
    assert "entries" in result.out and "verified" in result.out
    assert result.err == ""
    assert out.is_file()


def test_create_json_keys(run: RunFn, data: Path, tmp_path: Path) -> None:
    result = run("create", "--json", tmp_path / "t.zip", data)
    doc = result.json()
    assert doc["ok"] is True and doc["command"] == "create"
    assert {
        "issues",
        "entries_done",
        "entries_skipped",
        "bytes_done",
        "output_paths",
        "elapsed",
        "archive",
        "format",
        "verified",
        "encrypted",
        "input_bytes",
        "archive_bytes",
        "parts",
        "exit_code",
    } <= set(doc)
    assert doc["format"] == "zip" and doc["verified"] is True and doc["parts"] == []
    assert doc["entries_done"] == 5 and doc["exit_code"] == 0


@pytest.mark.parametrize(
    ("name", "fmt"),
    [
        ("o.zip", "zip"),
        ("o.tar", "tar"),
        ("o.tar.gz", "tar.gz"),
        ("o.7z", "7z"),
        ("o.tgz", "tar.gz"),
    ],
)
def test_create_infers_format_and_round_trips(
    run: RunFn, data: Path, tmp_path: Path, name: str, fmt: str
) -> None:
    out = tmp_path / name
    assert run("create", "--json", out, data).json()["format"] == fmt
    assert run("test", out).code == 0
    dest = tmp_path / "back"
    assert run("extract", out, "-o", dest).code == 0
    assert (dest / "data" / "sub" / "b.txt").read_bytes() == b"beta\n" * 30


def test_create_explicit_format_overrides_suffix(run: RunFn, data: Path, tmp_path: Path) -> None:
    out = tmp_path / "weird.dat"
    assert run("create", "-f", "zip", out, data).code == 0
    assert run("info", "--json", out).json()["format"] == "zip"


def test_create_unknown_suffix_without_format(run: RunFn, data: Path, tmp_path: Path) -> None:
    result = run("create", tmp_path / "x.foo", data)
    assert result.code == 2
    assert "-f" in result.err


def test_create_refuses_to_overwrite_without_force(run: RunFn, data: Path, archive: Path) -> None:
    before = archive.read_bytes()
    result = run("create", archive, data)
    assert result.code == 2 and "--force" in result.err
    assert archive.read_bytes() == before
    assert run("create", "--force", archive, data).code == 0


def test_create_missing_source(run: RunFn, tmp_path: Path) -> None:
    result = run("create", tmp_path / "x.zip", tmp_path / "nope")
    assert result.code == 2
    assert not (tmp_path / "x.zip").exists()


@pytest.mark.parametrize("level", ["fast", "normal", "best", "0", "9"])
def test_create_levels(run: RunFn, data: Path, tmp_path: Path, level: str) -> None:
    assert run("create", "-l", level, tmp_path / "l.zip", data).code == 0


def test_create_no_verify_summary(run: RunFn, data: Path, tmp_path: Path) -> None:
    result = run("create", "--no-verify", tmp_path / "n.zip", data)
    assert result.code == 0 and "verified" not in result.out
    assert (
        run("create", "--json", "--no-verify", "--force", tmp_path / "n.zip", data).json()[
            "verified"
        ]
        is False
    )


def test_create_encrypt_names_needs_password(run: RunFn, data: Path, tmp_path: Path) -> None:
    result = run("create", "--encrypt-names", tmp_path / "e.7z", data)
    assert result.code == 2 and "password" in result.err


def test_create_password_on_unencryptable_format(run: RunFn, data: Path, tmp_path: Path) -> None:
    assert run("create", "--password=pw", tmp_path / "e.tar", data).code == 2


def test_create_split_round_trip(run: RunFn, tmp_path: Path) -> None:
    src = make_tree(tmp_path / "big", {"r.bin": random_bytes(1, 30_000)})
    out = tmp_path / "s.zip"
    result = run("create", "--json", "--split", "10000", out, src)
    assert result.code == 0
    doc = result.json()
    assert len(doc["parts"]) >= 3 and not out.exists()
    assert (tmp_path / "s.zip.001").is_file()
    assert (tmp_path / "s.zip.parts.json").is_file()
    assert (tmp_path / "HOW-TO-OPEN.txt").is_file()
    listing = run("list", tmp_path / "s.zip.001")
    assert listing.code == 0 and "big/r.bin" in listing.out
    dest = tmp_path / "x"
    assert run("extract", tmp_path / "s.zip.002", "-o", dest).code == 0
    assert (dest / "big" / "r.bin").read_bytes() == random_bytes(1, 30_000)


def test_create_split_preset_name_is_accepted(run: RunFn, data: Path, tmp_path: Path) -> None:
    result = run("create", "--split", "email", tmp_path / "p.zip", data)
    assert result.code == 0
    assert (tmp_path / "p.zip.001").is_file()
    assert "1 part" in result.out


def test_create_follow_symlinks_flag_is_accepted(run: RunFn, data: Path, tmp_path: Path) -> None:
    assert run("create", "--follow-symlinks", tmp_path / "f.zip", data).code == 0


def test_create_no_smart_store_flag_is_accepted(run: RunFn, data: Path, tmp_path: Path) -> None:
    assert run("create", "--no-smart-store", tmp_path / "f.zip", data).code == 0


# -- passwords ------------------------------------------------------------------------------


@pytest.fixture
def secret_archive(run: RunFn, data: Path, tmp_path: Path) -> Path:
    pw = tmp_path / "pw.txt"
    pw.write_text("s3cret-Pass\n", encoding="utf-8")
    out = tmp_path / "secret.zip"
    assert run("create", "--password-file", pw, out, data).code == 0
    return out


def test_password_file_round_trip(run: RunFn, secret_archive: Path, tmp_path: Path) -> None:
    pw = tmp_path / "pw.txt"
    assert run("test", "--password-file", pw, secret_archive).code == 0
    dest = tmp_path / "out"
    assert run("extract", "--password-file", pw, secret_archive, "-o", dest).code == 0
    assert (dest / "data" / "a.txt").read_bytes() == b"alpha\n" * 20


def test_password_on_command_line(run: RunFn, secret_archive: Path) -> None:
    assert run("test", secret_archive, "--password=s3cret-Pass").code == 0


@pytest.mark.parametrize("command", ["test", "extract"])
def test_wrong_password_exits_6(
    run: RunFn, secret_archive: Path, tmp_path: Path, command: str
) -> None:
    extra = ["-o", tmp_path / "o"] if command == "extract" else []
    result = run(command, secret_archive, "--password=nope", *extra)
    assert result.code == 6
    assert "nope" not in result.out + result.err


@pytest.mark.parametrize("command", ["test", "extract"])
def test_missing_password_without_tty_exits_6(
    run: RunFn, secret_archive: Path, tmp_path: Path, command: str
) -> None:
    extra = ["-o", tmp_path / "o"] if command == "extract" else []
    assert run(command, secret_archive, *extra).code == 6


def test_missing_password_in_json_mode(run: RunFn, secret_archive: Path) -> None:
    result = run("test", "--json", secret_archive)
    doc = result.json()
    assert result.code == 6 and doc["ok"] is False
    assert doc["error"]["code"] == "password-required"


def test_password_prompt_on_a_terminal(
    run: RunFn, secret_archive: Path, tmp_path: Path, tty, monkeypatch: pytest.MonkeyPatch
) -> None:
    tty()
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "s3cret-Pass")
    result = run("extract", secret_archive, "-o", tmp_path / "o")
    assert result.code == 0
    assert "s3cret-Pass" not in result.out + result.err


def test_bare_password_flag_prompts_twice_when_creating(
    run: RunFn, data: Path, tmp_path: Path, tty, monkeypatch: pytest.MonkeyPatch
) -> None:
    tty()
    asked: list[str] = []

    def fake(prompt: str = "") -> str:
        asked.append(prompt)
        return "pw-1234"

    monkeypatch.setattr("getpass.getpass", fake)
    out = tmp_path / "p.zip"
    assert run("create", out, data, "-p").code == 0
    assert len(asked) == 2
    assert run("test", out, "--password=pw-1234").code == 0


def test_bare_password_flag_mismatch(
    run: RunFn, data: Path, tmp_path: Path, tty, monkeypatch: pytest.MonkeyPatch
) -> None:
    tty()
    answers = iter(["one", "two"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    result = run("create", tmp_path / "p.zip", data, "-p")
    assert result.code == 2 and "match" in result.err


def test_bare_password_flag_without_terminal(run: RunFn, data: Path, tmp_path: Path) -> None:
    result = run("create", tmp_path / "p.zip", data, "-p")
    assert result.code == 2 and "--password-file" in result.err


@pytest.mark.parametrize("content", ["", "\n"])
def test_empty_password_file(run: RunFn, data: Path, tmp_path: Path, content: str) -> None:
    pw = tmp_path / "pw.txt"
    pw.write_text(content)
    assert run("create", "--password-file", pw, tmp_path / "x.zip", data).code == 2


def test_password_file_missing(run: RunFn, data: Path, tmp_path: Path) -> None:
    assert run("create", "--password-file", tmp_path / "no", tmp_path / "x.zip", data).code == 2


def test_password_and_file_together(run: RunFn, data: Path, tmp_path: Path) -> None:
    pw = tmp_path / "pw.txt"
    pw.write_text("x")
    result = run("create", "--password=a", "--password-file", pw, tmp_path / "x.zip", data)
    assert result.code == 2


@pytest.mark.parametrize("json_mode", [False, True])
def test_password_never_appears_in_output(
    run: RunFn, data: Path, tmp_path: Path, json_mode: bool
) -> None:
    flags = ["--json"] if json_mode else []
    out = tmp_path / "pw.zip"
    created = run("create", *flags, "--password=Zq9-unique-secret", out, data)
    listed = run("list", *flags, out)
    infoed = run("info", *flags, out)
    wrong = run("test", *flags, out, "--password=Zq9-unique-wrong")
    for r in (created, listed, infoed, wrong):
        text = r.out + r.err
        assert "unique-secret" not in text and "unique-wrong" not in text
    assert created.code == 0 and wrong.code == 6


# -- extract --------------------------------------------------------------------------------


def test_extract_text_and_json(run: RunFn, archive: Path, tmp_path: Path) -> None:
    text = run("extract", archive, "-o", tmp_path / "t")
    assert text.code == 0 and text.out.startswith("Extracted 5 entries to")
    doc = run("extract", "--json", archive, "-o", tmp_path / "j").json()
    assert doc["ok"] is True and doc["command"] == "extract"
    assert doc["destination"] == str(tmp_path / "j")
    assert {"issues", "entries_done", "entries_skipped", "bytes_done", "output_paths"} <= set(doc)


def test_smart_extract_wraps_several_top_level_items(
    run: RunFn, wide_archive: Path, isolated_environment: Path
) -> None:
    assert run("extract", wide_archive).code == 0
    assert (isolated_environment / "wide" / "readme.txt").is_file()
    assert (isolated_environment / "wide" / "data" / "a.txt").is_file()
    assert not (isolated_environment / "readme.txt").exists()


def test_smart_extract_single_top_level_goes_to_cwd(
    run: RunFn, archive: Path, isolated_environment: Path
) -> None:
    assert run("extract", archive).code == 0
    assert (isolated_environment / "data" / "a.txt").is_file()
    assert not (isolated_environment / "one").exists()


def test_extract_here_forces_current_directory(
    run: RunFn, wide_archive: Path, isolated_environment: Path
) -> None:
    assert run("extract", "--here", wide_archive).code == 0
    assert (isolated_environment / "readme.txt").is_file()
    assert not (isolated_environment / "wide").exists()


def test_extract_dest_wins_over_smart_folder(
    run: RunFn, wide_archive: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "chosen"
    assert run("extract", wide_archive, "-o", dest).code == 0
    assert (dest / "readme.txt").is_file() and not (dest / "wide").exists()


def test_smart_extract_single_file_archive(
    run: RunFn, tmp_path: Path, isolated_environment: Path
) -> None:
    src = tmp_path / "note.txt"
    src.write_text("only one")
    gz = tmp_path / "note.txt.gz"
    assert run("create", gz, src).code == 0
    assert run("extract", gz).code == 0
    assert (isolated_environment / "note.txt").read_text() == "only one"


def test_extract_selected_names(run: RunFn, archive: Path, tmp_path: Path) -> None:
    dest = tmp_path / "some"
    assert run("extract", archive, "data/sub/b.txt", "-o", dest).code == 0
    assert extracted_files(dest) == ["data/sub/b.txt"]


def test_extract_unknown_name_is_a_usage_error(run: RunFn, archive: Path, tmp_path: Path) -> None:
    assert run("extract", archive, "nothing/here", "-o", tmp_path / "x").code == 2


def test_extract_strip_components(run: RunFn, archive: Path, tmp_path: Path) -> None:
    dest = tmp_path / "s"
    assert run("extract", archive, "--strip-components", "1", "-o", dest).code == 0
    assert (dest / "a.txt").is_file() and (dest / "sub" / "b.txt").is_file()


def test_extract_no_restore_mode_flag(run: RunFn, archive: Path, tmp_path: Path) -> None:
    assert run("extract", archive, "--no-restore-mode", "-o", tmp_path / "m").code == 0


@pytest.fixture
def occupied(run: RunFn, archive: Path, tmp_path: Path) -> Path:
    """A destination that already holds a different ``data/a.txt``."""
    dest = tmp_path / "dest"
    (dest / "data").mkdir(parents=True)
    (dest / "data" / "a.txt").write_text("old")
    return dest


def test_on_conflict_skip_exits_1(run: RunFn, archive: Path, occupied: Path) -> None:
    result = run("extract", archive, "-o", occupied, "--on-conflict", "skip")
    assert result.code == 1
    assert "warning: data/a.txt: skipped" in result.err
    assert (occupied / "data" / "a.txt").read_text() == "old"
    assert "1 skipped" in result.out


def test_on_conflict_default_without_terminal_is_skip(
    run: RunFn, archive: Path, occupied: Path
) -> None:
    assert run("extract", archive, "-o", occupied).code == 1
    assert (occupied / "data" / "a.txt").read_text() == "old"


def test_on_conflict_rename(run: RunFn, archive: Path, occupied: Path) -> None:
    result = run("extract", archive, "-o", occupied, "--on-conflict", "rename")
    assert result.code == 0
    assert (occupied / "data" / "a.txt").read_text() == "old"
    assert len(list((occupied / "data").glob("a*.txt"))) == 2
    assert "info:" in result.err


def test_on_conflict_overwrite(run: RunFn, archive: Path, occupied: Path) -> None:
    assert run("extract", archive, "-o", occupied, "--on-conflict", "overwrite").code == 0
    assert (occupied / "data" / "a.txt").read_bytes() == b"alpha\n" * 20


@pytest.mark.parametrize(
    ("answers", "survivors"),
    [
        (["o"], {"a.txt": b"alpha\n" * 20}),
        (["s"], {"a.txt": b"old"}),
        (["r"], {"a.txt": b"old"}),
    ],
)
def test_on_conflict_ask_answers(
    run: RunFn, archive: Path, occupied: Path, tty, answers: list[str], survivors: dict[str, bytes]
) -> None:
    tty(*answers)
    result = run("extract", archive, "-o", occupied)
    assert result.code in (0, 1)
    for name, content in survivors.items():
        assert (occupied / "data" / name).read_bytes() == content
    assert "[s]kip" in result.err


def test_on_conflict_ask_remembers_all(run: RunFn, archive: Path, tmp_path: Path, tty) -> None:
    dest = tmp_path / "d"
    (dest / "data" / "sub").mkdir(parents=True)
    (dest / "data" / "a.txt").write_text("old")
    (dest / "data" / "sub" / "b.txt").write_text("old")
    tty("oa")  # a single answer: the second conflict must not prompt again
    result = run("extract", archive, "-o", dest, "--on-conflict", "ask")
    assert result.code == 0
    assert (dest / "data" / "sub" / "b.txt").read_bytes() == b"beta\n" * 30
    assert result.err.count("[s]kip") == 1


def test_on_conflict_ask_reprompts_on_garbage(
    run: RunFn, archive: Path, occupied: Path, tty
) -> None:
    tty("what", "", "o")
    assert run("extract", archive, "-o", occupied).code == 0
    assert (occupied / "data" / "a.txt").read_bytes() == b"alpha\n" * 20


def test_resume_after_cancel(
    run: RunFn, archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from helpers.jobs import stepping_context
    from zipwise.cli import commands

    dest = tmp_path / "resumed"
    ctx, _, _ = stepping_context(cancel_on="data/sub/b.txt")
    monkeypatch.setattr(commands, "make_context", lambda env: ctx)
    assert run("extract", archive, "-o", dest).code == 130
    monkeypatch.undo()
    result = run("extract", archive, "-o", dest, "--resume")
    assert result.code == 0
    assert extracted_files(dest) == ["data/a.txt", "data/sub/b.txt", "data/sub/c.bin"]
    assert not list(dest.rglob("*.zwpart"))


def test_extract_legacy_names_with_override(run: RunFn, tmp_path: Path) -> None:
    legacy = legacy_zip(tmp_path / "ar.zip", ARABIC, "cp1256")
    dest = tmp_path / "ar"
    assert run("extract", legacy, "--encoding", "cp1256", "-o", dest).code == 0
    assert extracted_files(dest) == sorted(n.replace("\\", "/") for n in ARABIC)


def test_extract_unknown_encoding(run: RunFn, archive: Path, tmp_path: Path) -> None:
    assert run("extract", archive, "--encoding", "klingon", "-o", tmp_path / "x").code == 2


# -- list / test / info ----------------------------------------------------------------------


def test_list_names_only(run: RunFn, archive: Path) -> None:
    result = run("list", archive)
    assert result.code == 0
    lines = result.out.splitlines()
    assert "data/a.txt" in lines and "data/sub/b.txt" in lines
    assert lines[-1].startswith("5 entries")


def test_list_long(run: RunFn, archive: Path) -> None:
    result = run("list", "--long", archive)
    header = result.out.splitlines()[0]
    assert header.split()[:6] == ["Size", "Packed", "Ratio", "Modified", "Flags", "Name"]
    assert "data/sub/c.bin" in result.out


def test_list_json(run: RunFn, archive: Path) -> None:
    doc = run("list", "--json", archive).json()
    assert doc["ok"] is True and doc["command"] == "list"
    assert doc["entries_count"] == len(doc["entries"]) == 5
    assert {"name", "type", "size", "compressed_size", "mtime", "encrypted", "index"} <= set(
        doc["entries"][0]
    )
    assert {"name_encoding", "encoding_confidence", "total_size", "archive"} <= set(doc)


def test_list_json_arabic_names_are_real_text(run: RunFn, tmp_path: Path) -> None:
    name = "ملف عربي.txt"
    tree = make_tree(tmp_path / "t", {name: b"x"})
    out = tmp_path / "ar.zip"
    create_archive([tree], out, CreateOptions(format=FormatId.ZIP))
    result = run("list", "--json", out)
    assert f"t/{name}" in [e["name"] for e in result.json()["entries"]]
    assert name in result.out and "\\u0645" not in result.out


def test_list_legacy_names_note_and_override(run: RunFn, tmp_path: Path) -> None:
    legacy = legacy_zip(tmp_path / "ar.zip", ARABIC, "cp1256")
    guessed = run("list", legacy)
    assert guessed.code == 0
    assert "note: names were decoded as" in guessed.err and "--encoding" in guessed.err
    forced = run("list", "--encoding", "cp1256", legacy)
    assert "note:" not in forced.err
    assert ARABIC[0] in forced.out
    assert run("list", "--json", legacy).json()["name_encoding"] not in (None, "utf-8", "ascii")


def test_list_has_no_note_for_utf8(run: RunFn, archive: Path) -> None:
    assert "note:" not in run("list", archive).err


def test_test_ok(run: RunFn, archive: Path) -> None:
    result = run("test", archive)
    assert result.code == 0 and result.out.strip() == "OK (5 entries)"


def test_test_json(run: RunFn, archive: Path) -> None:
    doc = run("test", "--json", archive).json()
    assert doc["ok"] is True and doc["command"] == "test" and doc["entries_count"] == 5


def _damaged(tmp_path: Path) -> Path:
    src = make_tree(tmp_path / "rnd", {"r.bin": random_bytes(3, 4000)})
    out = tmp_path / "bad.zip"
    create_archive([src], out, CreateOptions(format=FormatId.ZIP, smart_store=False))
    raw = bytearray(out.read_bytes())
    raw[len(raw) // 3] ^= 0xFF
    out.write_bytes(bytes(raw))
    return out


def test_test_damaged_archive_exits_4(run: RunFn, tmp_path: Path) -> None:
    result = run("test", _damaged(tmp_path))
    assert result.code == 4
    assert "error:" in result.err and result.out == ""


def test_test_damaged_archive_json(run: RunFn, tmp_path: Path) -> None:
    result = run("test", "--json", _damaged(tmp_path))
    doc = result.json()
    assert result.code == 4 and doc["ok"] is False
    assert doc["error"]["code"] == "test-failed"
    assert doc["exit_code"] == 4


def test_truncated_archive_exits_4(run: RunFn, archive: Path, tmp_path: Path) -> None:
    broken = tmp_path / "cut.zip"
    broken.write_bytes(archive.read_bytes()[:60])
    assert run("test", broken).code == 4
    assert run("list", "--json", broken).json()["error"]["code"] == "corrupt-archive"


def test_info_text(run: RunFn, archive: Path) -> None:
    result = run("info", archive)
    assert result.code == 0
    for label in ("Format:", "Entries:", "Encrypted:", "Names:", "Multi-part:", "Ratio:"):
        assert label in result.out
    assert "zip" in result.out


def test_info_json(run: RunFn, archive: Path) -> None:
    doc = run("info", "--json", archive).json()
    assert doc["format"] == "zip" and doc["entries_count"] == 5
    assert {
        "path",
        "total_size",
        "compressed_size",
        "encrypted",
        "name_encoding",
        "encoding_confidence",
        "multipart",
        "comment",
    } <= set(doc)


def test_info_encrypted(run: RunFn, secret_archive: Path) -> None:
    assert run("info", "--json", secret_archive).json()["encrypted"] is True


# -- split / join ---------------------------------------------------------------------------


@pytest.fixture
def blob(tmp_path: Path) -> Path:
    path = tmp_path / "blob.bin"
    path.write_bytes(random_bytes(9, 25_000))
    return path


def test_split_and_join_round_trip(run: RunFn, blob: Path, tmp_path: Path) -> None:
    parts_dir = tmp_path / "parts"
    result = run("split", blob, "--size", "10000", "-o", parts_dir)
    assert result.code == 0
    assert "blob.bin.001" in result.out and "3 parts" in result.out
    assert (parts_dir / "blob.bin.parts.json").is_file()
    assert (parts_dir / "HOW-TO-OPEN.txt").is_file()
    assert blob.exists()
    joined = tmp_path / "joined.bin"
    assert run("join", parts_dir / "blob.bin.002", "-o", joined).code == 0
    assert joined.read_bytes() == blob.read_bytes()


def test_split_json(run: RunFn, blob: Path, tmp_path: Path) -> None:
    doc = run("split", "--json", blob, "--size", "10KB", "-o", tmp_path / "p").json()
    assert doc["ok"] is True and doc["command"] == "split"
    assert [p["size"] for p in doc["parts"]] == [10_000, 10_000, 5_000]
    assert doc["part_size"] == 10_000
    assert {"manifest", "notes", "source", "output_paths"} <= set(doc)


def test_split_delete_source(run: RunFn, blob: Path, tmp_path: Path) -> None:
    assert run("split", blob, "--size", "20000", "-o", tmp_path / "p", "--delete-source").code == 0
    assert not blob.exists()


def test_split_keep_flag(run: RunFn, blob: Path, tmp_path: Path) -> None:
    assert run("split", blob, "--size", "20000", "-o", tmp_path / "p", "--keep").code == 0
    assert blob.exists()


def test_split_missing_file(run: RunFn, tmp_path: Path) -> None:
    assert run("split", tmp_path / "gone", "--size", "1M").code == 2


def test_join_json_and_manifest_verification(run: RunFn, blob: Path, tmp_path: Path) -> None:
    parts = tmp_path / "p"
    run("split", blob, "--size", "10000", "-o", parts)
    doc = run("join", "--json", parts / "blob.bin.001", "-o", tmp_path / "j.bin").json()
    assert doc["verified"] is True and doc["parts_count"] == 3 and doc["command"] == "join"


def test_join_detects_a_corrupted_part(run: RunFn, blob: Path, tmp_path: Path) -> None:
    parts = tmp_path / "p"
    run("split", blob, "--size", "10000", "-o", parts)
    victim = parts / "blob.bin.002"
    raw = bytearray(victim.read_bytes())
    raw[5] ^= 0x01
    victim.write_bytes(bytes(raw))
    out = tmp_path / "j.bin"
    result = run("join", parts / "blob.bin.001", "-o", out)
    assert result.code == 4 and "blob.bin.002" in result.err
    assert not out.exists()


def test_join_with_a_missing_part(run: RunFn, blob: Path, tmp_path: Path) -> None:
    parts = tmp_path / "p"
    run("split", blob, "--size", "10000", "-o", parts)
    (parts / "blob.bin.002").unlink()
    assert run("join", parts / "blob.bin.001", "-o", tmp_path / "j.bin").code == 4


def test_join_into_a_folder_uses_the_original_name(run: RunFn, blob: Path, tmp_path: Path) -> None:
    parts = tmp_path / "p"
    run("split", blob, "--size", "10000", "-o", parts)
    out = tmp_path / "folder"
    out.mkdir()
    assert run("join", parts / "blob.bin.001", "-o", out).code == 0
    assert (out / "blob.bin").read_bytes() == blob.read_bytes()


# -- quiet and progress ---------------------------------------------------------------------


@pytest.mark.parametrize("command", ["create", "extract", "test", "split", "join"])
def test_quiet_prints_nothing_on_success(
    run: RunFn, data: Path, archive: Path, blob: Path, tmp_path: Path, command: str
) -> None:
    parts = tmp_path / "qp"
    argv = {
        "create": ["create", "-q", tmp_path / "q.zip", data],
        "extract": ["extract", "-q", archive, "-o", tmp_path / "qx"],
        "test": ["test", "-q", archive],
        "split": ["split", "-q", blob, "--size", "10000", "-o", parts],
    }
    if command == "join":
        run("split", blob, "--size", "10000", "-o", parts)
        argv["join"] = ["join", "-q", parts / "blob.bin.001", "-o", tmp_path / "j.bin"]
    result = run(*argv[command])
    assert result.code == 0 and result.out == "" and result.err == ""


def test_quiet_still_reports_warnings(run: RunFn, archive: Path, occupied: Path) -> None:
    result = run("extract", "-q", archive, "-o", occupied)
    assert result.code == 1 and result.out == "" and "warning:" in result.err


def test_progress_bar_on_a_terminal(
    run: RunFn, data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = FakeTTY()
    monkeypatch.setattr(sys, "stderr", err)
    from zipwise.cli.main import main

    assert main(["create", str(tmp_path / "pb.zip"), str(data)]) == 0
    shown = err.getvalue()
    assert "\r" in shown and "%" in shown and "Compressing" in shown


@pytest.mark.parametrize("flag", ["--quiet", "--no-progress", "--json"])
def test_progress_bar_can_be_switched_off(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    err = FakeTTY()
    monkeypatch.setattr(sys, "stderr", err)
    from zipwise.cli.main import main

    assert main([flag, "create", str(tmp_path / "pb.zip"), str(data)]) == 0
    assert "\r" not in err.getvalue()


def test_progress_bar_is_absent_when_stderr_is_not_a_terminal(
    run: RunFn, data: Path, tmp_path: Path
) -> None:
    assert "\r" not in run("create", tmp_path / "np.zip", data).err


# -- json purity ----------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["create", "extract", "list", "test", "info"])
def test_json_stdout_is_exactly_one_document(
    run: RunFn, data: Path, archive: Path, tmp_path: Path, command: str
) -> None:
    argv = {
        "create": ["create", tmp_path / "jd.zip", data],
        "extract": ["extract", archive, "-o", tmp_path / "jx"],
        "list": ["list", archive],
        "test": ["test", archive],
        "info": ["info", archive],
    }[command]
    result = run("--json", *argv)
    decoder = json.JSONDecoder()
    doc, end = decoder.raw_decode(result.out)
    assert result.out[end:].strip() == "" and doc["command"] == command


def test_json_with_warnings_keeps_stdout_clean(run: RunFn, archive: Path, occupied: Path) -> None:
    result = run("extract", "--json", archive, "-o", occupied)
    doc = result.json()
    assert result.code == 1 and doc["exit_code"] == 1
    assert doc["issues"][0]["code"] == "skipped-existing"
    assert "warning:" in result.err
