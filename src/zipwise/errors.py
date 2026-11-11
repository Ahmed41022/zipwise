"""Exception hierarchy and process exit codes."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    WARNINGS = 1
    USAGE = 2
    UNSUPPORTED = 3
    CORRUPT = 4
    SAFETY = 5
    PASSWORD = 6
    IO = 7
    CANCELLED = 130


class ZipwiseError(Exception):
    """Base class. Carries a stable machine-readable ``code`` and an optional hint."""

    code = "error"
    exit_code = ExitCode.USAGE

    def __init__(self, message: str, *, hint: str | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        if code is not None:
            self.code = code

    def to_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


class UsageError(ZipwiseError):
    code = "usage"
    exit_code = ExitCode.USAGE


class UnsupportedFormat(ZipwiseError):
    code = "unsupported-format"
    exit_code = ExitCode.UNSUPPORTED


class BackendUnavailable(ZipwiseError):
    code = "backend-unavailable"
    exit_code = ExitCode.UNSUPPORTED


class CorruptArchive(ZipwiseError):
    code = "corrupt-archive"
    exit_code = ExitCode.CORRUPT


class SafetyViolation(ZipwiseError):
    code = "safety-violation"
    exit_code = ExitCode.SAFETY


class BombDetected(SafetyViolation):
    code = "bomb-detected"


class PasswordRequired(ZipwiseError):
    code = "password-required"
    exit_code = ExitCode.PASSWORD


# Front-end neutral; the CLI and GUI each say how to supply the password.
PASSWORD_REQUIRED_HINT = "A password is needed to open this archive."


class WrongPassword(ZipwiseError):
    code = "wrong-password"
    exit_code = ExitCode.PASSWORD


class IOFailure(ZipwiseError):
    code = "io-failure"
    exit_code = ExitCode.IO


class Cancelled(ZipwiseError):
    code = "cancelled"
    exit_code = ExitCode.CANCELLED

    def __init__(self, message: str = "Operation cancelled", *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
