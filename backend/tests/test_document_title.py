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
