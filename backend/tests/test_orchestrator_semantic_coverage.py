from pathlib import Path

import pikepdf

from app.pipeline.orchestrator import _semantic_coverage_summary
from app.schemas import ValidationReportResponse


def _struct_elem(pdf: pikepdf.Pdf, tag: str, **extra) -> pikepdf.Object:
    payload = {
        "/Type": pikepdf.Name("/StructElem"),
        "/S": pikepdf.Name(f"/{tag}"),
    }
    payload.update(extra)
    return pdf.make_indirect(pikepdf.Dictionary(payload))


def test_semantic_coverage_summary_counts_tags_and_list_numbering(tmp_path: Path):
    path = tmp_path / "tagged.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))

    list_elem = _struct_elem(
        pdf,
        "L",
        **{
            "/A": pikepdf.Dictionary({
                "/O": pikepdf.Name("/List"),
                "/ListNumbering": pikepdf.Name("/Decimal"),
            })
        },
    )
    caption_elem = _struct_elem(pdf, "Caption")
    bib_elem = _struct_elem(pdf, "BibEntry")
    illegal_heading = _struct_elem(pdf, "H9")
    document = _struct_elem(
        pdf,
        "Document",
        **{"/K": pikepdf.Array([list_elem, caption_elem, bib_elem, illegal_heading])},
    )
    pdf.Root["/StructTreeRoot"] = pdf.make_indirect(
        pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/K": pikepdf.Array([document]),
        })
    )
    pdf.save(path)

    summary = _semantic_coverage_summary(path)

    assert summary["available"] is True
    assert summary["total_struct_elems"] == 5
    assert summary["interesting_tags"]["Caption"] == 1
    assert summary["interesting_tags"]["BibEntry"] == 1
    assert summary["list_numbering"] == {"Decimal": 1}
    assert summary["illegal_heading_tags"] == {"H9": 1}


def test_semantic_coverage_summary_handles_missing_struct_tree(tmp_path: Path):
    path = tmp_path / "untagged.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.save(path)

    assert _semantic_coverage_summary(path) == {
        "available": False,
        "reason": "missing_struct_tree",
    }


def test_validation_report_response_accepts_semantic_coverage_payload():
    report = ValidationReportResponse(
        compliant=True,
        semantic_coverage={
            "available": True,
            "total_struct_elems": 2,
            "interesting_tags": {"Document": 1, "Caption": 1},
        },
    )

    assert report.semantic_coverage["interesting_tags"]["Caption"] == 1
