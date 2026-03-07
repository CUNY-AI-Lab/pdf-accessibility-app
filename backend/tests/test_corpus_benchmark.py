from pathlib import Path

from scripts.corpus_benchmark import _should_skip


def test_should_skip_generated_outputs_only() -> None:
    assert _should_skip(Path("/tmp/accessible_output.pdf"))
    assert _should_skip(Path("/tmp/doc_fontfix_tagged.pdf"))
    assert _should_skip(Path("/tmp/doc.gsfix.pdf"))
    assert _should_skip(Path("/tmp/repaired_input.pdf"))


def test_should_not_skip_legitimate_source_with_accessible_substring() -> None:
    assert not _should_skip(Path("/tmp/bmuv_accessible_pdf_manual.pdf"))
