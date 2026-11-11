from __future__ import annotations

from pathlib import Path

import pytest

from zipwise import registry
from zipwise.backends.base import ArchiveReader, ArchiveWriter
from zipwise.errors import BackendUnavailable, UnsupportedFormat
from zipwise.model import FormatId


@pytest.mark.parametrize("fmt", list(FormatId), ids=lambda f: f.name)
def test_every_format_has_a_row(fmt: FormatId) -> None:
    cap = registry.capability(fmt)
    assert cap.module.startswith("zipwise.backends.")
    assert cap.reader is not None


@pytest.mark.parametrize(
    "fmt", [f for f in FormatId if f is not FormatId.RAR], ids=lambda f: f.name
)
def test_every_format_except_rar_is_writable_in_the_table(fmt: FormatId) -> None:
    assert registry.capability(fmt).writer is not None


def test_rar_is_read_only_and_needs_a_tool() -> None:
    cap = registry.capability(FormatId.RAR)
    assert cap.writer is None
    assert cap.needs_tool == "unrar"
    with pytest.raises(UnsupportedFormat):
        registry.get_writer_class(FormatId.RAR)


@pytest.mark.parametrize("fmt", list(FormatId), ids=lambda f: f.name)
def test_every_reader_class_loads(fmt: FormatId) -> None:
    assert issubclass(registry.get_reader_class(fmt), ArchiveReader)


def test_zip_row_capabilities() -> None:
    cap = registry.capability(FormatId.ZIP)
    assert cap.can_encrypt and cap.can_store_per_file and cap.multi_entry


@pytest.mark.parametrize("fmt", [FormatId.GZ, FormatId.BZ2, FormatId.XZ, FormatId.ZST])
def test_single_file_formats_are_not_multi_entry(fmt: FormatId) -> None:
    assert registry.capability(fmt).multi_entry is False


def test_zip_classes_load_lazily() -> None:
    assert issubclass(registry.get_reader_class(FormatId.ZIP), ArchiveReader)
    assert issubclass(registry.get_writer_class(FormatId.ZIP), ArchiveWriter)


def test_missing_third_party_dependency_gives_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_import(name: str) -> object:
        raise ModuleNotFoundError("No module named 'py7zr'", name="py7zr")

    monkeypatch.setattr(registry.importlib, "import_module", fake_import)
    with pytest.raises(BackendUnavailable) as info:
        registry.get_reader_class(FormatId.SEVENZ)
    assert "pip install py7zr" in (info.value.hint or "")


def test_missing_internal_module_has_no_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_import(name: str) -> object:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(registry.importlib, "import_module", fake_import)
    with pytest.raises(BackendUnavailable) as info:
        registry.get_reader_class(FormatId.TAR)
    assert info.value.hint is None
    assert "not available" in info.value.message


def test_get_reader_detects_the_format(tmp_path: Path) -> None:
    import zipfile

    path = tmp_path / "x.dat"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a", "b")
    with registry.get_reader(path) as reader:
        assert reader.format is FormatId.ZIP
        assert [e.name for e in reader.entries()] == ["a"]
