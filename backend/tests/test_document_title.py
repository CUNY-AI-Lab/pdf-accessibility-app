"""Which title the remediated PDF shows and a screen reader announces.

The PDF's own document-info title used to win even when an authoring tool had
filled it with "Microsoft Word - x.docx", a file name, or "untitled".
"""

from pathlib import Path

import pikepdf
import pytest

from app.pipeline.tagger import tag_pdf

DETECTED = "Graduate Enrollment Report 2025"


def _pdf_titled(path: Path, title: str | None) -> Path:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    if title is not None:
        pdf.docinfo["/Title"] = title
    pdf.save(path)
    return path


async def _shipped_title(tmp_path: Path, existing: str | None, detected: str | None) -> tuple[str, str]:
    output = tmp_path / "tagged.pdf"
    structure: dict = {"elements": []}
    if detected:
        structure["title"] = detected
    await tag_pdf(
        input_path=_pdf_titled(tmp_path / "enrollment_report.pdf", existing),
        output_path=output,
        structure_json=structure,
        alt_texts=[],
        original_filename="enrollment_report.pdf",
    )
    with pikepdf.open(output) as pdf:
        with pdf.open_metadata() as meta:
            xmp_title = str(meta.get("dc:title"))
        return str(pdf.docinfo["/Title"]), xmp_title


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "existing",
    [
        "Microsoft Word - enrollment_report.docx",
        "Microsoft PowerPoint - deck",
        "final_v3.pdf",
        "report.docx",
        "untitled",
        "Untitled-1",
        "Document1",
        "enrollment_report",
        "ENROLLMENT_REPORT.PDF",
        "   ",
    ],
)
async def test_placeholder_title_gives_way_to_the_detected_title(tmp_path, existing):
    assert await _shipped_title(tmp_path, existing, DETECTED) == (DETECTED, DETECTED)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "existing",
    [
        "Annual Report on Graduate Enrollment",
        "Word Problems in Algebra",
        "Documenting Oral Histories",
        "Untitled Film Stills: A Study",
    ],
)
async def test_a_real_existing_title_is_kept(tmp_path, existing):
    assert await _shipped_title(tmp_path, existing, DETECTED) == (existing, existing)


@pytest.mark.asyncio
async def test_placeholder_title_without_a_detected_title_falls_back_to_the_file_name(tmp_path):
    shipped = await _shipped_title(tmp_path, "Microsoft Word - Document1", None)

    assert shipped == ("enrollment report", "enrollment report")


def _visible_pdf(path, lines, *, info=None, xmp=None, repeat_header=None):
    """Draw actual text; each line is (text, effective size, baseline)."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica,
    ))
    for page_lines in [lines, *([[repeat_header]] if repeat_header else [])]:
        page = pdf.add_blank_page(page_size=(612, 792))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        # A nominal 1pt font scaled by Tm, as Office PDFs commonly use.
        stream = []
        for text, size, y in page_lines:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream.append(f"BT /F1 1 Tf {size} 0 0 {size} 72 {y} Tm ({escaped}) Tj ET")
        page.Contents = pdf.make_stream("\n".join(stream).encode())
    if info:
        pdf.docinfo["/Title"] = info
    if xmp:
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
            meta["dc:title"] = xmp
    pdf.save(path)
    return path


async def _ship_visible(tmp_path, source, structure):
    output = tmp_path / "visible-tagged.pdf"
    await tag_pdf(
        input_path=source, output_path=output, structure_json=structure,
        alt_texts=[], original_filename="download_123.pdf",
    )
    with pikepdf.open(output) as pdf:
        with pdf.open_metadata() as meta:
            title = str(meta.get("dc:title"))
        assert str(pdf.docinfo["/Title"]) == title
        assert pdf.Root.ViewerPreferences.DisplayDocTitle is True
    return title


@pytest.mark.parametrize("info", [None, "CST-1101-CLASS_SYLLABUS_2023", "Syllabus Draft - Old Course"])
async def test_visible_title_beats_later_h1_and_stale_metadata(tmp_path, info):
    source = _visible_pdf(tmp_path / "source.pdf", [
        ("Incorporating Experiential Learning", 28, 650),
        ("and Digital Badging in STEM", 28, 615),
        ("Presenter Name", 12, 570), ("Body text", 12, 300),
    ], info=info)
    structure = {"title": "What is a Digital Badge?", "elements": [
        {"type": "heading", "page": 1, "level": 1, "text": "What is a Digital Badge?"},
    ]}
    assert await _ship_visible(tmp_path, source, structure) == (
        "Incorporating Experiential Learning and Digital Badging in STEM"
    )


async def test_xmp_only_title_survives_metadata_reset(tmp_path):
    source = _visible_pdf(tmp_path / "source.pdf", [], info="untitled", xmp="Oral History Handbook")
    assert await _ship_visible(tmp_path, source, {"title": "Wrong section", "elements": []}) == "Oral History Handbook"


async def test_running_head_date_and_course_code_do_not_become_title(tmp_path):
    header = ("Department Newsletter", 30, 760)
    source = _visible_pdf(tmp_path / "source.pdf", [
        header, ("Fall 2023", 32, 720), ("CST 1101", 32, 680),
        ("Sociological Imagination", 24, 630), ("Body text", 12, 300),
    ], repeat_header=header)
    assert await _ship_visible(tmp_path, source, {"title": "Calendar", "elements": []}) == "Sociological Imagination"


async def test_real_text_prevents_filename_fallback(tmp_path):
    source = _visible_pdf(tmp_path / "source.pdf", [("Workshop handout", 12, 300)])
    assert await _ship_visible(tmp_path, source, {"elements": []}) == "Workshop handout"


async def test_first_h1_fallback_is_written_to_both_metadata_titles(tmp_path):
    source = _visible_pdf(tmp_path / "source.pdf", [])
    structure = {"title": "download_123.pdf", "elements": [
        {"type": "heading", "level": 2, "page": 0, "text": "A subheading"},
        {"type": "heading", "level": 1, "page": 0, "text": "A Real Heading"},
    ]}
    assert await _ship_visible(tmp_path, source, structure) == "A Real Heading"


async def test_title_drops_small_raised_footnote_marker(tmp_path):
    source = _visible_pdf(tmp_path / "source.pdf", [
        ("Main Document Title", 26, 650), ("Body text", 12, 300),
    ])
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Contents = pikepdf.Array([
            pdf.pages[0].Contents,
            pdf.make_stream(b"BT /F1 1 Tf 16 0 0 16 302 660 Tm (1) Tj ET"),
        ])
        pdf.save(source)
    assert await _ship_visible(tmp_path, source, {
        "title": "Main Document Title 1", "elements": [],
    }) == "Main Document Title"


async def test_visible_title_can_match_the_uploaded_filename(tmp_path):
    source = _visible_pdf(tmp_path / "Main Document Title.pdf", [
        ("Main Document Title", 26, 650), ("Body text", 12, 300),
    ])
    output = tmp_path / "output.pdf"
    await tag_pdf(
        source, output, {"title": "Later Section", "elements": []}, [],
        original_filename=source.name,
    )
    with pikepdf.open(output) as pdf:
        assert str(pdf.docinfo["/Title"]) == "Main Document Title"


def test_structure_title_uses_first_page_typography_before_docling_labels(tmp_path):
    from app.pipeline.structure import _extract_title_from_docling

    source = _visible_pdf(tmp_path / "source.pdf", [
        ("Main Document Title", 26, 650), ("Body text", 12, 300),
    ])
    docling = {"texts": [{"label": "title", "text": "Later Section", "prov": [{"page_no": 3}]}]}
    assert _extract_title_from_docling(docling, [], pdf_path=source) == "Main Document Title"


async def test_clear_title_bypasses_standalone_model(tmp_path, monkeypatch):
    from app.services.title_intelligence import enhance_document_title_with_intelligence

    async def forbidden(**kwargs):
        pytest.fail("A clear visible title must not need a model request")

    monkeypatch.setattr("app.services.title_intelligence.request_pdf_pages_json", forbidden)
    source = _visible_pdf(tmp_path / "source.pdf", [
        ("Main Document Title", 26, 650), ("Body text", 12, 300),
    ])
    structure, audit = await enhance_document_title_with_intelligence(
        pdf_path=source, structure_json={"title": "Later Section", "elements": [
            {"type": "heading", "level": 1, "page": 1, "text": "Later Section"},
        ]}, original_filename="download_123.pdf", llm_client=object(),
    )
    assert structure["title"] == "Main Document Title"
    assert audit["attempted"] is False


async def test_combined_model_cannot_override_clear_title(tmp_path, monkeypatch):
    from app.services.early_pages_intelligence import (
        enhance_document_title_and_front_matter_with_intelligence,
    )

    async def fake_request(**kwargs):
        evidence = kwargs["context_payload"]["deterministic_title"]
        assert evidence["clear"] is True
        assert evidence["title"] == "Main Document Title"
        assert {c["source"] for c in evidence["candidates"]} >= {"first_page", "first_h1"}
        return {
            "title": {"confidence": "high", "title": "Later Section"},
            "front_matter": {"confidence": "high", "entries": [{"page_index": 0, "label": "Cover"}]},
        }

    monkeypatch.setattr("app.services.early_pages_intelligence.request_pdf_pages_json", fake_request)
    source = _visible_pdf(tmp_path / "source.pdf", [
        ("Main Document Title", 26, 650), ("Body text", 12, 300),
    ])
    structure = {"title": "Later Section", "elements": [
        {"type": "heading", "text": "Main Document Title", "page": 0, "level": 1},
        {"type": "heading", "text": "Publication details", "page": 1, "level": 1},
        {"type": "toc_caption", "text": "Contents", "page": 2, "toc_group_ref": "toc"},
        {"type": "toc_item", "text": "Later Section", "page": 2, "toc_group_ref": "toc"},
    ]}
    structure, audit, entries, _ = await enhance_document_title_and_front_matter_with_intelligence(
        pdf_path=source, structure_json=structure,
        original_filename="download_123.pdf", llm_client=object(),
    )
    assert structure["title"] == "Main Document Title"
    assert audit["title"] == "Main Document Title"
    assert audit["applied"] is False
    assert entries[0]["text"] == "Cover"
