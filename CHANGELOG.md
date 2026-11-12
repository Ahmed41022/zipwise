# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- An interoperability test suite (`tests/interop`) that runs the real 7-Zip, `rar` and `unrar`.
- `cryptography` is now a dependency, so RAR archives with encrypted file names open without
  extra steps.

### Fixed

- RAR archives with encrypted file names were listed as empty; they now ask for the password.
- ZIP entries that use Deflate64 (and other methods the standard library lacks) and 7z entries
  that use BCJ2 or ARM64 now fail with an error that names the method (exit code 3), and
  `test` exits with 3 instead of 4 for them.
- A RAR entry that 7-Zip, unar or bsdtar refused to extract was reported as damaged or as a
  wrong password; Zipwise now says the tool could not extract it and suggests `unrar`.
- Creating over an older split set (`--force`) removes that set's parts, manifest and
  `HOW-TO-OPEN.txt` instead of leaving them next to the new archive.

## [0.1.0] - 2026-11-11

First release.

### Added

- Formats: ZIP (Deflate, Zip64, empty folders, symlinks, modes and times), 7z, TAR, tar.gz,
  tar.bz2, tar.xz, tar.zst, and single-file gz, bz2, xz and zst for create, list, test and
  extract. RAR for list, test and extract (needs `unrar`, `unar`, `7z` or `bsdtar` on `PATH`).
- Content-based format detection by magic bytes, including TAR inside a compressor and
  multi-part `.001` sets.
- Passwords: AES-256 for ZIP, AES-256 with optional encrypted file names for 7z; legacy
  ZipCrypto ZIP and RAR can be read.
- Safe extraction: every entry is planned before anything is written. Blocks absolute and drive
  paths, UNC paths, `..` components, NUL bytes, links that leave the destination, paths through
  links, and unsafe hard links; skips device files and FIFOs; renames Windows-invalid names and
  case clashes; limits for expansion ratio, total size and entry count; a byte budget that
  catches headers that lie about sizes.
- File-name encoding detection for legacy ZIP names (cp437, cp850, cp1252, cp1256, cp720, cp866,
  cp1251, cp1253, cp1255, cp932, GBK, Big5, cp949) with a confidence value, a manual override
  and a preview.
- Compress to fit: split presets (`email`, `whatsapp`, `telegram`, `telegram-premium`, `fat32`)
  and custom sizes, `.001` volume files, a SHA-256 manifest checked by `join`, a bilingual
  `HOW-TO-OPEN.txt`, and reading `list`, `test` and `extract` straight from the parts.
- Verification after create, on by default.
- Smart defaults: already-compressed files are stored in ZIP, the format is chosen from the
  archive name, and the destination folder is chosen from the archive's layout.
- Resumable extraction with a per-entry journal.
- Atomic output: archives are written as `.partial` and entries as `.zwpart`, and removed on
  cancel or error.
- Command-line interface with `create`, `extract`, `list`, `test`, `info`, `split` and `join`;
  `--json`, `--quiet` and `--no-progress`; a progress bar; Ctrl-C cancels cleanly; documented
  exit codes.
- Optional PySide6 GUI (`zipwise-gui`): archive browser, create, extract, test and info, drag and
  drop, progress with cancel, conflict and password dialogs, settings, English and Arabic with a
  right-to-left layout.
- Settings file shared by the CLI and the GUI.
- Continuous integration on Ubuntu and Windows with Python 3.11 and 3.13: ruff, ruff format,
  mypy and pytest.

### Not included

- Spanned `.z01` ZIP, editing archives in place, resumable create, RAR creation, shell
  integration and installers.
