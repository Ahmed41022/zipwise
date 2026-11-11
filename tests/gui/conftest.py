from __future__ import annotations

import os

# Must be set before Qt is imported anywhere.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from helpers.recorder import Recorder  # noqa: E402
from helpers.trees import SHAPES, make_tree  # noqa: E402
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
