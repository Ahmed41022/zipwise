"""Entry point of the graphical front end."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QEventLoop
from PySide6.QtWidgets import QApplication

from ..settings import load_settings
from .main_window import MainWindow, apply_language


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zipwise-gui", description="Zipwise archiver")
    parser.add_argument("archive", nargs="?", help="archive to open")
    parser.add_argument("--smoke", action="store_true", help="open the window, close it, exit")
    parser.add_argument("--settings", type=Path, help="settings file to use")
    args, qt_args = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Zipwise")

    settings = load_settings(args.settings)
    apply_language(settings.language)
    window = MainWindow(settings, args.settings)
    window.show()
    if args.archive:
        window.open_archive(Path(args.archive))
    if args.smoke:
        if window.loading:
            loop = QEventLoop()
            window.open_ended.connect(loop.quit)
            loop.exec()
        app.processEvents()
        window.close()
        app.processEvents()
        return 0
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
