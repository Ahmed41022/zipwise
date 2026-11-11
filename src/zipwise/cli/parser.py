"""Builds the ``argparse`` parser. No archive logic lives here."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import Any

from .. import __version__
from ..errors import UsageError
from ..model import FormatId
from ..split import parse_size

COMMAND_NAMES = ("create", "extract", "list", "test", "info", "split", "join")

# Sub-commands that take ``-p/--password`` and ``--password-file``.
PASSWORD_COMMANDS = ("create", "extract", "list", "test", "info")

# Value of ``-p`` / ``--password`` given without a value: ask on the terminal.
PASSWORD_PROMPT = "\x00prompt"

# Longest suffixes first so ``.tar.gz`` wins over ``.gz``.
FORMAT_SUFFIXES: tuple[tuple[str, FormatId], ...] = (
    (".tar.gz", FormatId.TAR_GZ),
    (".tar.bz2", FormatId.TAR_BZ2),
    (".tar.xz", FormatId.TAR_XZ),
    (".tar.zst", FormatId.TAR_ZST),
    (".tgz", FormatId.TAR_GZ),
    (".tbz2", FormatId.TAR_BZ2),
    (".txz", FormatId.TAR_XZ),
    (".zip", FormatId.ZIP),
    (".7z", FormatId.SEVENZ),
    (".tar", FormatId.TAR),
    (".gz", FormatId.GZ),
    (".bz2", FormatId.BZ2),
    (".xz", FormatId.XZ),
    (".zst", FormatId.ZST),
    (".rar", FormatId.RAR),
)

_FORMAT_ALIASES: dict[str, FormatId] = {
    **{f.value: f for f in FormatId},
    "tgz": FormatId.TAR_GZ,
    "tbz2": FormatId.TAR_BZ2,
    "txz": FormatId.TAR_XZ,
    "tzst": FormatId.TAR_ZST,
    "sevenz": FormatId.SEVENZ,
}

_LEVEL_NAMES = ("fast", "normal", "best")
_CONFLICT_CHOICES = ("skip", "rename", "overwrite", "ask")


def format_from_name(name: str) -> FormatId | None:
    """The format an archive file name implies, or None for an unknown suffix."""
    lowered = name.lower()
    for suffix, fmt in FORMAT_SUFFIXES:
        if lowered.endswith(suffix):
            return fmt
    return None


def suffix_of(name: str) -> str | None:
    """The known archive suffix at the end of ``name`` (lower case), if any."""
    lowered = name.lower()
    for suffix, _ in FORMAT_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return None


def format_arg(text: str) -> FormatId:
    fmt = _FORMAT_ALIASES.get(text.strip().lower().lstrip("."))
    if fmt is None:
        raise argparse.ArgumentTypeError(
            f"unknown format {text!r} (choose from {', '.join(f.value for f in FormatId)})"
        )
    return fmt


def level_arg(text: str) -> str | int:
    lowered = text.strip().lower()
    if lowered in _LEVEL_NAMES:
        return lowered
    if lowered.isdigit() and 0 <= int(lowered) <= 9:
        return int(lowered)
    raise argparse.ArgumentTypeError(f"invalid level {text!r} (use fast, normal, best or 0-9)")


def size_arg(text: str) -> int:
    try:
        return parse_size(text)
    except UsageError as exc:
        raise argparse.ArgumentTypeError(exc.message) from exc


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


class ParserError(Exception):
    """Raised instead of ``SystemExit`` so ``main`` can render the error in text or JSON."""

    def __init__(self, message: str, usage: str, prog: str) -> None:
        super().__init__(message)
        self.message = message
        self.usage = usage
        self.prog = prog


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        raise ParserError(message, self.format_usage(), self.prog)


class _CommandParser(_Parser):
    """A sub-command parser whose positionals and options may come in any order.

    The sub-parsers action calls ``parse_known_args``; routing that through the intermixed
    variant lets ``extract a.7z -o out notes.txt`` work. On Python 3.11 and 3.12 the
    intermixed parse itself calls ``parse_known_args`` again, hence the re-entry guard.
    """

    _intermixed_active = False

    def parse_known_args(  # type: ignore[override]
        self, args: Sequence[str] | None = None, namespace: Any = None
    ) -> tuple[Any, list[str]]:
        if self._intermixed_active:
            return super().parse_known_args(args, namespace)
        self._intermixed_active = True
        try:
            return self.parse_known_intermixed_args(args, namespace)
        finally:
            self._intermixed_active = False


def _globals() -> _CommandParser:
    """Options accepted before or after the sub-command (SUPPRESS keeps the first value)."""
    common = _CommandParser(add_help=False)
    group = common.add_argument_group("global options")
    group.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print one JSON document on stdout instead of text",
    )
    group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print nothing on success (warnings and errors still go to stderr)",
    )
    group.add_argument(
        "--no-progress",
        action="store_true",
        default=argparse.SUPPRESS,
        help="never draw the progress bar",
    )
    return common


def _password_args(parser: argparse.ArgumentParser, *, confirm_note: str) -> None:
    group = parser.add_argument_group("password")
    group.add_argument(
        "-p",
        "--password",
        nargs="?",
        const=PASSWORD_PROMPT,
        default=None,
        metavar="PASSWORD",
        help=f"password; without a value it is asked for {confirm_note}. "
        "Put a bare -p after the positional arguments (or write --password=VALUE)",
    )
    group.add_argument(
        "--password-file",
        metavar="FILE",
        help="read the password from the first line of FILE",
    )


def build_parser() -> argparse.ArgumentParser:
    common = _globals()
    parser = _Parser(
        prog="zipwise",
        description="Create, inspect and safely extract ZIP, 7z, TAR, gz/bz2/xz/zst and RAR.",
        parents=[common],
        epilog="Run 'zipwise COMMAND --help' for the options of one command.",
    )
    parser.add_argument("--version", action="version", version=f"zipwise {__version__}")
    sub = parser.add_subparsers(
        dest="command", metavar="COMMAND", parser_class=_CommandParser, required=True
    )

    def add(name: str, summary: str, example: str) -> argparse.ArgumentParser:
        return sub.add_parser(
            name,
            help=summary,
            description=summary,
            parents=[common],
            epilog=f"example:\n  {example}",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

    # -- create ---------------------------------------------------------------------------
    p = add(
        "create",
        "create an archive from files and folders",
        "zipwise create photos.zip Pictures/ notes.txt --level best --split email",
    )
    p.add_argument("archive", metavar="ARCHIVE", help="archive to write")
    p.add_argument("sources", nargs="+", metavar="SOURCE", help="files or folders to add")
    p.add_argument(
        "-f",
        "--format",
        type=format_arg,
        default=None,
        help="zip, 7z, tar, tar.gz, tar.bz2, tar.xz, tar.zst, gz, bz2, xz, zst "
        "(default: from the archive name)",
    )
    p.add_argument(
        "-l",
        "--level",
        type=level_arg,
        default=None,
        metavar="LEVEL",
        help="fast, normal, best or 0-9",
    )
    _password_args(p, confirm_note="twice")
    p.add_argument("--encrypt-names", action="store_true", help="also encrypt file names (7z only)")
    p.add_argument(
        "--split",
        type=size_arg,
        default=None,
        metavar="SIZE|PRESET",
        help="cut the archive into parts of SIZE (25M, 700MiB, ...) or a preset "
        "(email, whatsapp, telegram, telegram-premium, fat32)",
    )
    p.add_argument("--no-verify", action="store_true", help="skip the check after writing")
    p.add_argument(
        "--no-smart-store",
        action="store_true",
        help="compress already-compressed files too (ZIP)",
    )
    p.add_argument(
        "--follow-symlinks", action="store_true", help="archive what links point to, not the links"
    )
    p.add_argument("--force", action="store_true", help="overwrite an existing archive")

    # -- extract --------------------------------------------------------------------------
    p = add(
        "extract",
        "extract an archive safely",
        "zipwise extract backup.7z -o restored --on-conflict rename",
    )
    p.add_argument("archive", metavar="ARCHIVE")
    p.add_argument("names", nargs="*", metavar="NAME", help="extract only these entries")
    where = p.add_mutually_exclusive_group()
    where.add_argument("-o", "--dest", metavar="DIR", help="destination folder")
    where.add_argument(
        "--here", action="store_true", help="extract into the current folder, never a new one"
    )
    _password_args(p, confirm_note="once")
    p.add_argument(
        "--encoding", metavar="ENC", help="file-name encoding of legacy ZIPs (cp1256...)"
    )
    p.add_argument(
        "--on-conflict",
        choices=_CONFLICT_CHOICES,
        default=None,
        help="existing files: skip, rename, overwrite or ask (default: ask on a terminal, "
        "else skip)",
    )
    p.add_argument("--resume", action="store_true", help="continue an interrupted extraction")
    p.add_argument("--max-ratio", type=_positive_float, metavar="N", help="largest allowed ratio")
    p.add_argument("--max-size", type=size_arg, metavar="SIZE", help="largest total output")
    p.add_argument("--max-entries", type=_positive_int, metavar="N", help="most entries allowed")
    p.add_argument("--no-symlinks", action="store_true", help="do not create symbolic links")
    p.add_argument(
        "--strip-components",
        type=_positive_int,
        default=0,
        metavar="N",
        help="drop N leading folders from every name",
    )
    p.add_argument("--no-restore-mode", action="store_true", help="do not restore permission bits")

    # -- list -----------------------------------------------------------------------------
    p = add("list", "list the contents of an archive", "zipwise list backup.zip --long")
    p.add_argument("archive", metavar="ARCHIVE")
    p.add_argument("--encoding", metavar="ENC", help="file-name encoding of legacy ZIPs")
    p.add_argument(
        "-l", "--long", action="store_true", help="show size, packed size, ratio, date and flags"
    )
    _password_args(p, confirm_note="once (needed for 7z with encrypted names)")

    # -- test -----------------------------------------------------------------------------
    p = add("test", "check that an archive is intact", "zipwise test backup.zip")
    p.add_argument("archive", metavar="ARCHIVE")
    _password_args(p, confirm_note="once")

    # -- info -----------------------------------------------------------------------------
    p = add("info", "show format, sizes, encryption and name encoding", "zipwise info backup.zip")
    p.add_argument("archive", metavar="ARCHIVE")
    p.add_argument("--encoding", metavar="ENC", help="file-name encoding of legacy ZIPs")
    _password_args(p, confirm_note="once (needed for 7z with encrypted names)")

    # -- split ----------------------------------------------------------------------------
    p = add(
        "split",
        "cut any file into numbered parts with a checksum manifest",
        "zipwise split movie.mkv --size 25M -o parts/",
    )
    p.add_argument("file", metavar="FILE")
    p.add_argument(
        "--size",
        type=size_arg,
        required=True,
        metavar="SIZE|PRESET",
        help="part size (25M, 700MiB, ...) or a preset",
    )
    p.add_argument("-o", "--output", dest="out_dir", metavar="DIR", help="folder for the parts")
    p.add_argument(
        "--keep",
        action="store_true",
        help="keep the original file (this is the default; see --delete-source)",
    )
    p.add_argument(
        "--delete-source", action="store_true", help="remove the original after a successful split"
    )

    # -- join -----------------------------------------------------------------------------
    p = add(
        "join",
        "put the parts of a split file back together",
        "zipwise join backup.zip.001 -o backup.zip",
    )
    p.add_argument("part", metavar="PART", help="any part of the set")
    p.add_argument("-o", "--output", metavar="OUTPUT", help="output file or folder")

    return parser
