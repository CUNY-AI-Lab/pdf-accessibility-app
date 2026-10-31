from pathlib import Path

import pikepdf
import pytest

from app.pipeline import pdf_repair


def test_repaired_copy_is_available_only_inside_the_context(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        pdf.save(source)
    original = source.read_bytes()
    monkeypatch.setattr(pdf_repair, "add_missing_icc_components", lambda pdf: 1)

    with pdf_repair.pdfminer_readable(source) as repaired:
        assert repaired != source
        assert repaired.is_file()
        with pikepdf.open(repaired) as pdf:
            assert len(pdf.pages) == 1

    assert not repaired.parent.exists()
    assert source.read_bytes() == original


def test_repaired_copy_directory_is_cleaned_if_save_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        pdf.save(source)
    original_temporary_directory = pdf_repair.tempfile.TemporaryDirectory
    # Retain the objects so garbage collection cannot mask missing cleanup.
    directories = []

    def temporary_directory():
        directory = original_temporary_directory(dir=tmp_path)
        directories.append(directory)
        return directory

    def fail_save(self, destination):
        raise OSError("cannot save repaired PDF")

    monkeypatch.setattr(pdf_repair, "add_missing_icc_components", lambda pdf: 1)
    monkeypatch.setattr(pdf_repair.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(pikepdf.Pdf, "save", fail_save)

    try:
        with pytest.raises(OSError, match="cannot save repaired PDF"):
            with pdf_repair.pdfminer_readable(source):
                pytest.fail("a failed save must not yield a repaired path")
        assert len(directories) == 1
        assert not Path(directories[0].name).exists()
    finally:
        for directory in directories:
            directory.cleanup()
