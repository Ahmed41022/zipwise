# Contributing to Zipwise

Thanks for helping. This file covers the development setup, how the code is laid out, and what a
good change looks like.

## Development setup

Python 3.11 or newer is required. From a clone of the repository:

```
git clone <repository-url>
cd <repository-folder>
python -m venv .venv
```

Activate the environment (`.venv\Scripts\Activate.ps1` in PowerShell, `source .venv/bin/activate`
on Linux), then install in editable mode with the development and GUI extras:

```
python -m pip install -e ".[dev,gui]"
```

The GUI tests need PySide6. On Linux they also need a few system libraries for Qt's offscreen
platform:

```
sudo apt-get install -y libegl1 libxkbcommon0 libgl1 libdbus-1-3 libfontconfig1 libxcb-cursor0
```

## The four checks

Run these from the repository root before every push. CI runs the same four on Ubuntu and Windows
with Python 3.11 and 3.13, and all four must pass.

```
ruff check .
ruff format --check .
mypy src/zipwise
pytest -q
```

`ruff format .` fixes formatting. The GUI tests set `QT_QPA_PLATFORM=offscreen` themselves; set it
in your shell if you run Qt code by hand on a machine without a display.

## How the code is organised

```
src/zipwise/
  errors.py model.py jobs.py         exceptions and exit codes, data types, progress and cancel
  detect.py registry.py              format detection by content; format -> backend table
  safety.py naming.py conflicts.py   extraction policy, name sanitising, overwrite handling
  encoding.py split.py resume.py     name encodings, split and join, extraction journal
  smart.py settings.py fsutil.py     defaults, settings file, atomic files and paths
  ops/                               the public API: create, extract, listing, verify, info
  backends/                          zip, sevenz, tar, single (gz/bz2/xz/zst), rar
  cli/                               argparse front end
  gui/  i18n/                        PySide6 front end and its message catalogues
tests/                               core, backends, safety, encoding, split, cli, gui, helpers
```

The layering rules, some of them enforced by `tests/core/test_import_rules.py`:

- The core (everything except `cli/` and `gui/`) has no GUI imports and does not import either
  front end. Only `zipwise.gui` imports `PySide6`.
- `zipwise.cli` and `zipwise.gui` never import each other.
- Both front ends call only `zipwise.ops` (plus small helpers such as settings, split and
  encoding). They contain no archive logic: no format handling, no path checks.
- Backends never touch the destination file system. A reader pushes each entry's bytes into an
  `EntrySink`; the extraction code in `ops/extract.py` owns safety checks, naming, conflicts,
  atomic writes and metadata. A writer receives files from the create code and writes only to the
  stream or path it is given.
- Third-party exceptions (`zipfile`, `py7zr`, `tarfile`, `rarfile`) are translated into
  `ZipwiseError` subclasses at the backend boundary. Nothing from those libraries reaches the CLI
  or GUI.
- Every loop that copies bytes goes through the shared helpers in `fsutil`, so that cancellation
  is checked and progress is reported.

## Adding a format backend

1. Write one module in `src/zipwise/backends/` with a reader (subclass of `ArchiveReader`) and,
   if the format can be written, a writer (subclass of `ArchiveWriter`). Keep the third-party
   library confined to that module and translate its errors.
2. Add the format to `FormatId` in `model.py`, a signature to `detect.py`, and one row to
   `CAPABILITIES` in `registry.py` (module, reader, writer, whether it encrypts, whether it needs
   an external tool, and the package name for the install hint). Add the file suffix to
   `cli/parser.py` if it should be recognised by name.
3. Declare any new dependency in `pyproject.toml`, and add it to the licence table in the README.
4. Add tests:
   - a round trip through `tests/helpers/roundtrip.py` for every writable format (create, test,
     extract, compare names, bytes, mtimes, empty folders);
   - malicious archives built with low-level writers (path traversal, absolute paths, links that
     escape, device entries, lying size headers, too many entries) in `tests/safety/`;
   - corrupt and truncated input, and wrong or missing password where the format has one.

## Tests

- All test data is generated in the test (`tests/helpers/trees.py`, `builders.py`). Do not commit
  binary fixtures, archives or sample files.
- Keep the suite fast. Use small data; reach large-file behaviour (Zip64, multi-gigabyte sizes) by
  lowering thresholds through parameters or by streaming zeros, never by writing gigabytes. Build
  expensive objects once, in a session or module scoped fixture.
- Markers (declared in `pyproject.toml`):

  | Marker | Use |
  |---|---|
  | `slow` | noticeably slower than the rest |
  | `gui` | needs PySide6 and an offscreen Qt platform |
  | `needs_symlink` | needs permission to create symlinks (skipped when the OS refuses) |
  | `needs_unrar` | needs an external unrar-compatible tool (skipped when none is installed) |

  Deselect with `pytest -m "not slow"`. The "tool missing" path of the RAR backend is tested
  without a tool by emptying `PATH`.
- A bug fix comes with a test that fails without it.
- Do not import PySide6 in tests outside `tests/gui/`.

## Commits and pull requests

- Small commits, each doing one thing and leaving the four checks green.
- Subject line in the imperative mood, about 70 characters, no trailing full stop (for example
  "Block symlinks that pass through another link"). Use the body to say why, when it is not
  obvious.
- Pull requests describe what changed and why, and mention anything that changes behaviour a
  user can see (flags, output, exit codes). Update `README.md` and `CHANGELOG.md` in the same
  change.
- Files use LF line endings (`.gitattributes` enforces it); Markdown files end with a newline.
- Code style is whatever `ruff` and `ruff format` say (line length 100). Code in `src/zipwise`
  is fully type-annotated.

## Reporting a security issue

Archive handling is the sensitive part of this project: path traversal, links that escape the
destination, decompression bombs, unsafe overwrites, and anything that writes outside the
destination folder. If you find a problem of that kind, please do not open a public issue or pull
request with the details. Contact the maintainer, Ahmed Ragab, privately first, with the archive
(or the steps to build it), the command you ran and what happened, and allow time for a fix
before publishing. Ordinary bugs and feature requests can go through the normal issue tracker.
