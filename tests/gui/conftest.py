from __future__ import annotations

import os

# Must be set before Qt is imported anywhere.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Callable, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from helpers.recorder import Recorder  # noqa: E402
from helpers.trees import SHAPES, make_tree, random_bytes  # noqa: E402
from zipwise.gui.main_window import MainWindow, apply_language  # noqa: E402
from zipwise.jobs import JobContext  # noqa: E402
from zipwise.model import CreateOptions  # noqa: E402
from zipwise.ops import create_archive  # noqa: E402
from zipwise.settings import Settings  # noqa: E402


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    gui_dir = Path(__file__).parent
    for item in items:
        if gui_dir in Path(str(item.path)).parents:
            item.add_marker(pytest.mark.gui)


@pytest.fixture(autouse=True)
def reset_language() -> Iterator[None]:
    apply_language("en")
    yield
    apply_language("en")


@pytest.fixture
def proj_tree(tmp_path: Path) -> Path:
    return make_tree(tmp_path / "src" / "proj", SHAPES["nested"])


@pytest.fixture
def sample_zip(tmp_path: Path, proj_tree: Path) -> Path:
    dest = tmp_path / "sample.zip"
    create_archive([proj_tree], dest, CreateOptions(), JobContext())
    return dest


@pytest.fixture
def win(qtbot, tmp_path: Path) -> tuple[MainWindow, Recorder]:
    window = MainWindow(Settings(), tmp_path / "settings.json")
    qtbot.addWidget(window)
    rec = Recorder()
    window.show_error = rec.errors.append  # type: ignore[method-assign]
    window._message = lambda title, text: rec.messages.append((title, text))  # type: ignore[method-assign]
    window._show_progress = lambda dlg: None  # type: ignore[method-assign]
    return window, rec


@pytest.fixture
def open_wait(qtbot) -> Callable[[MainWindow, Path | str], str]:
    """Open an archive in a window and wait for the result: "opened", "failed" or "cancelled"."""

    def go(window: MainWindow, path: Path | str) -> str:
        with qtbot.waitSignal(window.open_ended, timeout=10000) as blocker:
            window.open_archive(path)
        return str(blocker.args[0])

    return go


@pytest.fixture
def bulk_tree(tmp_path: Path) -> Path:
    """Six incompressible files, so that an extraction has several entries to be cut between."""
    spec = {f"f{i}.bin": random_bytes(i, 60_000) for i in range(6)}
    return make_tree(tmp_path / "src" / "bulk", spec)


@pytest.fixture
def bulk_zip(tmp_path: Path, bulk_tree: Path) -> Path:
    dest = tmp_path / "bulk.zip"
    create_archive([bulk_tree], dest, CreateOptions(), JobContext())
    return dest


def completed_files(out: Path, bulk_tree: Path) -> list[Path]:
    """Assert that ``out`` holds no ``.zwpart`` and only complete, byte-identical files.

    Returns the sources of the files found, so a test can check how far extraction got.
    """
    found: list[Path] = []
    if not out.exists():
        return found
    for path in sorted(out.rglob("*")):
        if not path.is_file() or path.name.endswith(".zipwise-resume.json"):
            continue  # the resume journal is bookkeeping, not extracted data
        assert not path.name.endswith(".zwpart"), f"leftover partial file {path}"
        source = bulk_tree / path.relative_to(out / bulk_tree.name)
        assert source.is_file(), f"unexpected file {path}"
        assert path.read_bytes() == source.read_bytes(), f"{path} is incomplete or altered"
        found.append(source)
    return found


@pytest.fixture
def check_extraction() -> Callable[[Path, Path], list[Path]]:
    return completed_files
