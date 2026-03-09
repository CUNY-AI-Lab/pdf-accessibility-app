from pathlib import Path

from scripts.corpus_benchmark import ROOT_DIR, _should_skip, discover_pdfs


def test_should_skip_generated_outputs_only() -> None:
    assert _should_skip(Path("/tmp/accessible_output.pdf"))
    assert _should_skip(Path("/tmp/doc_fontfix_tagged.pdf"))
    assert _should_skip(Path("/tmp/doc.gsfix.pdf"))
    assert _should_skip(Path("/tmp/repaired_input.pdf"))


def test_should_not_skip_legitimate_source_with_accessible_substring() -> None:
    assert not _should_skip(Path("/tmp/bmuv_accessible_pdf_manual.pdf"))


def test_discover_pdfs_accepts_explicit_pdf_file_root(tmp_path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    found = discover_pdfs(scan_roots=[pdf_path], exclude_wac=False)

    assert found == [pdf_path]


def test_discover_pdfs_does_not_inject_wac_when_scan_roots_are_explicit(tmp_path) -> None:
    pdf_dir = tmp_path / "docs"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    found = discover_pdfs(scan_roots=[pdf_dir], exclude_wac=False)

    assert pdf_path in found
    assert (ROOT_DIR / "test_wac.pdf") not in found
