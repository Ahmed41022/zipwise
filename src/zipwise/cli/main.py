"""Entry point: parse, run one command, print the result, return the exit code."""

from __future__ import annotations

import contextlib
import dataclasses
import os
import signal
import sys
import traceback
from collections.abc import Sequence
from types import FrameType
from typing import TextIO

from ..errors import Cancelled, ExitCode, ZipwiseError
from ..fsutil import translate_oserror
from ..jobs import CancelToken
from . import commands
from .parser import COMMAND_NAMES, ParserError, build_parser
from .render import ProgressBar, Result, render_json, render_text

INTERNAL_ERROR = 70


def _isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return False


def _is_console(fd: int) -> bool:
    """False for a Windows character device that is not a console, such as ``NUL``.

    ``isatty()`` is True for any character device on Windows, so ``< NUL`` would look like a
    keyboard and a password prompt would wait forever. Only a console handle has a console
    mode. Elsewhere ``isatty()`` is already accurate.
    """
    if sys.platform == "win32":
        import ctypes
        import msvcrt

        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            return False
        mode = ctypes.c_uint32()
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            return bool(kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
        except (AttributeError, OSError):
            return True  # cannot tell: trust isatty()
    return True


def is_terminal(stream: object) -> bool:
    """True when ``stream`` is a real interactive terminal."""
    if not _isatty(stream):
        return False
    try:
        fd = stream.fileno()  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        return True  # a stream without a descriptor that says it is a terminal
    return _is_console(fd)


def interactive_terminal(stdin: object, stderr: object) -> bool:
    """Prompts are written to stderr and read from stdin, so both must be terminals."""
    return is_terminal(stdin) and is_terminal(stderr)


def _harden_streams(json_mode: bool) -> None:
    """Never let a console code page that lacks a character crash the output."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if json_mode and stream is sys.stdout:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            else:
                reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            pass


def _error_result(command: str | None, exc: ZipwiseError) -> Result:
    return Result(command, exit_code=int(exc.exit_code), ok=False, error=exc.to_dict())  # type: ignore[arg-type]


def _install_sigint(token: CancelToken) -> object:
    """Ctrl-C asks the running job to stop so it can remove its partial output."""

    def handler(signum: int, frame: FrameType | None) -> None:
        token.cancel()

    try:
        return signal.signal(signal.SIGINT, handler)
    except ValueError:  # not the main thread
        return None


def _restore_sigint(previous: object) -> None:
    if previous is not None:
        with contextlib.suppress(ValueError, TypeError):
            signal.signal(signal.SIGINT, previous)  # type: ignore[arg-type]


def _emit(result: Result, *, json_mode: bool, quiet: bool, out: TextIO, err: TextIO) -> None:
    if json_mode:
        render_json(result, out)
        # stdout stays one document; issues and errors are repeated on stderr for people
        bare = dataclasses.replace(result, lines=[], details=[], notes=[], summary=None)
        render_text(bare, err, err, quiet=quiet)
    else:
        render_text(result, out, err, quiet=quiet)


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in args_list
    _harden_streams(json_mode)
    parser = build_parser()
    guessed = next((a for a in args_list if a in COMMAND_NAMES), None)
    try:
        args = parser.parse_args(args_list)
    except ParserError as exc:
        if json_mode:
            result = Result(
                guessed,
                exit_code=int(ExitCode.USAGE),
                ok=False,
                error={"code": "usage", "message": exc.message, "hint": None},
            )
            render_json(result, sys.stdout)
        else:
            sys.stderr.write(exc.usage)
            sys.stderr.write(f"{exc.prog}: error: {exc.message}\n")
        return int(ExitCode.USAGE)
    except SystemExit as exc:  # --help / --version
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)

    command: str = args.command
    quiet: bool = getattr(args, "quiet", False)
    use_json: bool = getattr(args, "json", False)
    no_progress: bool = getattr(args, "no_progress", False)
    show_progress = is_terminal(sys.stderr) and not (quiet or no_progress or use_json)
    token = CancelToken()
    bar = ProgressBar(sys.stderr) if show_progress else None
    env = commands.RunEnv(
        cancel=token,
        progress=bar,
        stdin_tty=interactive_terminal(sys.stdin, sys.stderr),
        quiet=quiet,
        json=use_json,
    )
    previous = _install_sigint(token)
    try:
        result = commands.HANDLERS[command](args, env)
    except ZipwiseError as exc:
        result = _error_result(command, commands.with_cli_hint(command, exc))
    except KeyboardInterrupt:
        token.cancel()
        result = _error_result(command, Cancelled())
    except OSError as exc:
        result = _error_result(command, translate_oserror(exc))
    except Exception as exc:
        if os.environ.get("ZIPWISE_DEBUG") == "1":
            traceback.print_exc()
        result = Result(
            command,
            exit_code=INTERNAL_ERROR,
            ok=False,
            error={
                "code": "internal-error",
                "message": f"{type(exc).__name__}: {exc}",
                "hint": None,
            },
        )
    finally:
        _restore_sigint(previous)
        if bar is not None:
            bar.clear()
    if result.error is not None and result.error["code"] == "internal-error":
        sys.stderr.write(f"internal error: {result.error['message']}\n")
        if json_mode:
            render_json(result, sys.stdout)
        return result.exit_code
    _emit(result, json_mode=use_json, quiet=quiet, out=sys.stdout, err=sys.stderr)
    return result.exit_code
