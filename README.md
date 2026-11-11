# Zipwise

Zipwise is a free, MIT-licensed file archiver for Windows and Linux. It has a command-line
tool, an optional Qt GUI, and a shared Python core. It handles ZIP, 7z, TAR (plain, gzip,
bzip2, xz, zstd), single-file gz/bz2/xz/zst, and reads RAR.

What it is built for:

- **File-name encodings.** ZIP files made on older systems often store names in a legacy code
  page. Zipwise detects the encoding, shows you the result, and lets you override it, so Arabic,
  Cyrillic, Greek, Hebrew and CJK names come out right instead of as `╩▐╤φ╤`.
- **Compress to fit.** Cut an archive into parts sized for e-mail, WhatsApp, Telegram or FAT32,
  with a checksum manifest and a plain-text note for the recipient.
- **Verify after create.** Every archive is read back and checked before Zipwise reports
  success.
- **Safe extraction.** Every entry is checked before any byte is written: path traversal,
  absolute paths, escaping links, decompression bombs and Windows-invalid names are handled, not
  trusted.
- **Smart defaults.** Files that are already compressed (JPEG, MP4, PDF, ...) are stored, not
  deflated again; the format comes from the archive name; the destination folder is chosen so
  an archive never sprays files into the current folder.
- **Progress, cancel, resume.** Long jobs show progress and can be cancelled without leaving a
  half-written archive. An interrupted extraction can be resumed.

## Install

Python 3.11 or newer is required. From a clone of the repository:

```
git clone <repository-url>
cd <repository-folder>
pip install .
```

To include the GUI (adds PySide6):

```
pip install ".[gui]"
```

This installs the `zipwise` command (and `zipwise-gui` with the `gui` extra). `python -m zipwise`
works too. Zipwise is tested on Windows and Linux with Python 3.11 and 3.13.

## Quick tour of the CLI

Run `zipwise --help`, or `zipwise COMMAND --help`, for every option. The commands are
`create`, `extract`, `list`, `test`, `info`, `split` and `join`.

### Create

The format is taken from the archive name (`.zip`, `.7z`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`,
`.tar.xz`, `.tar.zst`, `.gz`, `.bz2`, `.xz`, `.zst`); use `-f` to override it.

```
zipwise create photos.zip Pictures notes.txt --level best
```
```
Created photos.zip (4 entries, 15.7 KB → 3.5 KB, 23%) and verified it.
```

Levels are `fast`, `normal` (the default), `best`, or a number from 0 to 9. Other useful flags:
`--no-verify`, `--no-smart-store`, `--follow-symlinks` (store what links point to instead of the
links), `--force` (replace an existing archive; without it Zipwise refuses).

Passwords: `--password-file FILE` reads the first line of a file. `-p` / `--password` takes the
password as a value, or asks for it on the terminal (twice) when given without one; put a bare
`-p` after the positional arguments. ZIP uses AES-256. For 7z, `--encrypt-names` also encrypts
the file names.

```
zipwise create secret.7z Documents --password-file pw.txt --encrypt-names
```

### Split for sending

`--split` takes a size (`25M`, `700MiB`, `1.5G`) or a preset (see
[Compress to fit](#compress-to-fit)). The archive is written and verified first, then cut into
numbered parts.

```
zipwise create holiday.zip Pictures --split email
```
```
/home/me/holiday.zip.001
/home/me/holiday.zip.002
/home/me/holiday.zip.parts.json
/home/me/HOW-TO-OPEN.txt
Created holiday.zip in 2 parts (3 entries, 15.0 KB → 3.4 KB, 23%) and verified it.
```

(That output is from a small test folder split into 3K parts; the `email` preset gives 25 MB
parts.)

Parts, the manifest and `HOW-TO-OPEN.txt` are written next to where the archive would have
been. `list`, `test` and `extract` work on `holiday.zip.001` directly, without joining.

To put the parts back together (any part will do; the manifest is checked when present):

```
zipwise join holiday.zip.001 -o holiday.zip
```

`zipwise split FILE --size SIZE|PRESET [-o DIR]` cuts any file the same way (the original is
kept unless you add `--delete-source`).

### List, info, test

```
zipwise list photos.zip --long
zipwise info photos.zip
zipwise test photos.zip
```
```
   Size  Packed  Ratio  Modified          Flags  Name
      -       -      -  2026-11-11 11:10  d-     photos
12.0 KB    43 B     0%  2026-11-11 11:10  --     photos/a.txt
 3.0 KB  3.0 KB   100%  2026-11-11 11:10  --     photos/b.jpg
  650 B    22 B     3%  2026-11-11 11:10  --     notes.txt
4 entries, 15.7 KB, 3.1 KB packed (20%)
```

`test` prints `OK (N entries)` and exits 0, or exits 4 if the archive is damaged.

### An archive with Arabic file names

A ZIP written without the UTF-8 flag, with names in Windows Arabic (cp1256):

```
zipwise list arabic.zip
```
```
note: names were decoded as cp1256 (confidence 95%); use --encoding ENC if they look wrong
مرحبا.txt
تقرير_المبيعات.txt
فاتورة 2024.txt
صورة.txt
4 entries, 122 B, 122 B packed (100%)
```

If the guess is wrong, say which encoding to use. It applies to `list`, `info` and `extract`:

```
zipwise list arabic.zip --encoding cp1256
zipwise extract arabic.zip --encoding cp1256 -o arabic
```

### Extract

```
zipwise extract photos.zip
```
```
Extracted 4 entries to /home/me/photos.
```

Without `-o`, Zipwise looks at the archive: if everything sits under one top-level folder it
extracts into the current folder, otherwise it creates a folder named after the archive.
`-o DIR` picks the destination; `--here` always uses the current folder.

To extract only some entries, put the names before the options:

```
zipwise extract backup.7z notes.txt photos -o restored
```

Other options: `--on-conflict skip|rename|overwrite|ask` (the default is `ask` on a terminal and
`skip` otherwise), `--strip-components N`, `--no-symlinks`, `--no-restore-mode`, and the
[safety limits](#safety-model).

**Resume.** If an extraction is interrupted (Ctrl-C, power loss, full disk), run the same
command again with `--resume` to skip the files that were already finished:

```
zipwise extract big-backup.zip -o restored --resume
```

Each file is written under a temporary name and renamed when complete. A small journal,
`.<archive-name>.zipwise-resume.json`, is kept in the destination folder while the job is
unfinished and removed when it succeeds. If the archive changed since the journal was written,
the journal is discarded and extraction starts over. Only extraction can be resumed; creating an
archive always starts from the beginning.

### Machine-readable output

`--json` (before or after the command) prints one JSON document on stdout. Progress is not
drawn, and warnings and errors are also repeated on stderr as text.

```
zipwise extract evil.zip -o out --json
```
```json
{
  "ok": false,
  "command": "extract",
  "issues": [
    {
      "severity": "error",
      "code": "path-traversal",
      "entry": "../evil.txt",
      "message": "blocked: path escapes the destination"
    }
  ],
  "entries_done": 1,
  "entries_skipped": 1,
  "bytes_done": 4,
  "output_paths": ["/home/me/out"],
  "elapsed": 0.015,
  "archive": "evil.zip",
  "destination": "out",
  "exit_code": 1
}
```

Every document has `ok`, `command` and `exit_code`. Commands that run a job (`create`, `extract`,
`test`, `split`, `join`) add `issues`, `entries_done`, `entries_skipped`, `bytes_done`,
`output_paths` and `elapsed`, plus fields of their own (`archive`, `destination`, `format`,
`verified`, `parts`, ...). `info` returns the archive description (`format`, `entries_count`,
`total_size`, `compressed_size`, `encrypted`, `name_encoding`, `encoding_confidence`,
`multipart`, `comment`). `list` returns `entries_count`, `total_size`, `name_encoding`,
`encoding_confidence` and an `entries` array (`name`, `type`, `size`, `compressed_size`, `mtime`,
`mode`, `link_target`, `encrypted`, `crc`, `index`).

When a command fails, the document has `"ok": false` and an `error` object instead of the job
fields:

```json
{
  "ok": false,
  "command": "list",
  "error": {
    "code": "usage",
    "message": "No such file or folder: nothere.zip",
    "hint": null
  },
  "exit_code": 2
}
```

`--quiet` / `-q` prints nothing on success. `--no-progress` turns the progress bar off; it is
only drawn when stderr is a terminal anyway.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Finished, but some entries were skipped, blocked or renamed (see the warnings) |
| 2 | Bad arguments or missing input |
| 3 | Unsupported format, or a required backend or tool is missing (for example no `unrar`) |
| 4 | Corrupt archive, or `test` found damage |
| 5 | Safety violation or decompression bomb; nothing more was written |
| 6 | Password required or wrong password |
| 7 | I/O failure (disk full, permission denied, read-only target) |
| 70 | Internal error (a bug; set `ZIPWISE_DEBUG=1` to see the traceback) |
| 130 | Cancelled (Ctrl-C) |

## Formats

| Format | Create | Extract / list / test | Password | Split |
|---|---|---|---|---|
| ZIP (Deflate, Zip64) | yes | yes | AES-256 on create; AES-256 and legacy ZipCrypto on read | yes |
| 7z (LZMA2, solid) | yes | yes | AES-256, optional encrypted names | yes |
| TAR | yes | yes | no | yes |
| tar.gz / tgz, tar.bz2, tar.xz, tar.zst | yes | yes | no | yes |
| gz, bz2, xz, zst (one file) | yes | yes | no | yes |
| RAR | no | yes (needs an external tool) | read | n/a |

Notes:

- "Split" means the finished archive is cut into volume files (`name.zip.001`, `name.7z.001`,
  ...). 7-Zip and WinRAR open these directly.
- TAR archives are written as PAX, so long and non-ASCII names survive. ZIP archives written by
  Zipwise always set the UTF-8 flag.
- 7z archives are solid, so the "store already-compressed files" rule applies to ZIP only; for
  7z and TAR one compression level covers the whole archive.
- A single-file format (`.gz` and friends) holds exactly one file; `create` refuses folders and
  multiple sources for it.
- Symlinks are stored as links, not followed (unless `--follow-symlinks`).

## Safety model

Extraction is planned before anything is written. Zipwise never calls `extractall`-style
shortcuts; every entry goes through the same checks.

**Blocked** (the entry is skipped, reported, and the exit code is 1):

- absolute paths, drive-letter paths and UNC paths;
- any `..` component; backslashes are treated as separators;
- names containing a NUL byte;
- a symbolic link whose target is absolute or leaves the destination, or passes through another
  link;
- a file whose path goes *through* a link stored earlier in the same archive;
- a hard link whose target is unsafe.

**Skipped with a warning:** device files, FIFOs and sockets; symlinks when `--no-symlinks` is
given (or, on Windows, when the account may not create links).

**Renamed (reported as `info`):** names Windows cannot hold (`CON`, `NUL`, `COM1`..., characters
such as `< > : " | ? *`, trailing dots and spaces), and names that differ only by case on a
case-insensitive destination (`Readme.txt` and `README.TXT` become `README (2).txt`).

**Limits.** These abort the whole job with exit code 5 before any file is written, using the
sizes declared in the archive; they are checked again against the bytes actually written, because
headers can lie.

| Limit | Default | Raise it with |
|---|---|---|
| Expansion ratio (uncompressed / compressed), checked once at least 256 MiB would be written | 250 | `--max-ratio N` |
| Total bytes written | the free space on the destination drive | `--max-size SIZE` |
| Number of entries | 200,000 | `--max-entries N` |

If the declared total does not fit on the disk, extraction stops with exit code 7. The same limits
(plus `max_entry_bytes`, a per-file limit with no default) can be set in the settings file:
`%APPDATA%\Zipwise\settings.json` on Windows, `$XDG_CONFIG_HOME/zipwise/settings.json`
(`~/.config/zipwise/settings.json`) on Linux. The CLI also reads `default_level`, `smart_store`,
`verify_after_create`, `on_conflict`, `allow_symlinks` and `sanitize_windows_names`
(`auto`, `always` or `never`) from it. A missing or malformed file means defaults.

An existing file is never overwritten unless you choose `overwrite`.

## File-name encodings

Old ZIP tools store names in the system code page, and the archive does not say which one. When
a ZIP does not carry the UTF-8 flag (or the Unicode Path field), Zipwise looks at the raw name
bytes of the whole archive. If they are all valid UTF-8 it uses that. Otherwise it decodes them
with each candidate code page (cp437, cp850, cp1252, cp1256, cp720, cp866, cp1251, cp1253,
cp1255, cp932, GBK, Big5, cp949), scores the results (letters from one script score well; box
drawing, symbol runs, mixed scripts inside a word and unassigned characters count against), and
picks the best. Near-ties are broken by your system's code page or locale. Entries that are
flagged as UTF-8 are never re-decoded, and an archive that mixes both kinds is handled
entry by entry.

The guess is shown by `info` and by `list` (with a confidence value) and can always be overridden
with `--encoding`. Code page names such as `cp1256`, `windows-1256` and `1256` all work.

Limits: the detector only has the names to go on. With a handful of very short names (one or two
names of a few letters), several code pages decode to plausible text, the guess can be wrong and
the confidence value is low. Check the result and pass `--encoding` when it matters.

## Compress to fit

| Preset | Part size |
|---|---|
| `email` | 25 MB |
| `whatsapp` | 2 GB |
| `telegram` | 2 GB |
| `telegram-premium` | 4 GB |
| `fat32` | 4 GiB minus 1 byte |

Presets are plain sizes; messaging apps change their limits, so use a custom size when in
doubt. Custom sizes take a number with an optional unit: `K`, `M`, `G` (and `KB`, `MB`, `GB`)
are powers of 1000; `KiB`, `MiB`, `GiB` are powers of 1024; a bare number is bytes. `25M` and
`700MiB` are both valid.

Splitting writes the whole archive first, verifies it, then cuts it and removes the whole file, so
it needs up to twice the archive size in free disk space (checked up front).

What the recipient gets, next to the parts:

- `NAME.001`, `NAME.002`, ...: the parts. Keep them together and do not rename them.
- `NAME.parts.json`: the manifest, with the size and SHA-256 of every part and of the whole file.
  `zipwise join` checks it, and refuses to join parts that are missing, out of order or damaged.
- `HOW-TO-OPEN.txt`: instructions in English and Arabic for opening the parts with 7-Zip or
  WinRAR, with `zipwise join`, or with no extra software at all (`copy /b` on Windows, `cat` on
  Linux). If you split several files into one folder, the last note replaces the earlier one.

## GUI

```
zipwise-gui [ARCHIVE]
python -m zipwise.gui.app [ARCHIVE]
```

The GUI needs the `gui` extra. It opens an archive in a sortable table (name, size, packed,
ratio, modified, type) with folder navigation, and has toolbar actions for New, Open, Extract,
Test and Info, plus Settings and About. Dropping an archive on the window opens it; dropping
files or folders opens the create dialog (format, level, password, split preset). The extract
dialog offers a destination, a name-encoding choice with a live preview of the names, and a
conflict policy. Long jobs show a progress dialog with Cancel; file-exists conflicts and
password prompts are asked in dialogs.

It is available in English and Arabic, chosen in the Language menu or in Settings; Arabic uses a
right-to-left layout. Settings are stored in the same file as the CLI's.

## RAR

Zipwise reads RAR (list, test, extract) but does not create it. Reading file contents needs an
external tool on `PATH`: `unrar`, `unar`, `7z` (or `7zz`) or `bsdtar`. Listing only parses headers.
Without a tool, extract and test stop with exit code 3 and an install hint. The tool is not
bundled.

```
winget install RARLab.WinRAR        # Windows (or: winget install 7zip.7zip)
sudo apt install unrar              # Debian / Ubuntu
```

Archives with encrypted headers also need the `cryptography` package. Only the first volume of a
multi-volume RAR set can be opened.

## Limitations and not in v1

- No spanned ZIP (`.z01` + `.zip`). Splitting writes `.001` volume files instead.
- No editing of an archive in place (add, delete or rename entries); create a new one.
- No Explorer or file-manager integration yet, no installer, no standalone build.
- Creating an archive cannot be resumed.
- No RAR creation, no repair of damaged archives, no ISO, CAB or ARJ.
- 7z support is tested against files written by py7zr (which Zipwise uses); archives from other
  7z tools should work but are less exercised.
- POSIX permission bits are stored and restored on Linux and ignored on Windows. Creating
  symlinks on Windows needs Developer Mode or the right privilege; without it a link entry is
  skipped with a warning.
- Compression is single-threaded.

## Dependency licences

Zipwise itself is MIT-licensed. It uses these packages, none of them modified or bundled:

| Package | Used for | Licence |
|---|---|---|
| pyzipper | ZIP AES-256 | MIT |
| py7zr | 7z | LGPL-2.1-or-later |
| zstandard | zstd | BSD-3-Clause |
| rarfile | RAR reading | ISC |
| PySide6 (optional, `gui` extra) | GUI | LGPL-3.0 |

The external RAR tools are separate programs under their own licences.

## Licence

MIT. See `LICENSE`. For development, see `CONTRIBUTING.md`; for release notes, `CHANGELOG.md`.
