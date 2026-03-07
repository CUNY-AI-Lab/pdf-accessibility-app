from pathlib import Path

import pikepdf
import pytest

from app.pipeline.tagger import tag_pdf


@pytest.mark.asyncio
async def test_tag_pdf_sets_markinfo_suspects_false(tmp_path):
    input_pdf = Path("backend/test_sample.pdf")
    output_pdf = tmp_path / "tagged.pdf"

    await tag_pdf(
        input_path=input_pdf,
        output_path=output_pdf,
        structure_json={"elements": [], "title": "Test Sample"},
        alt_texts=[],
        language="en",
        original_filename=input_pdf.name,
    )

    with pikepdf.open(output_pdf) as pdf:
        mark_info = pdf.Root["/MarkInfo"]
        assert mark_info["/Marked"] is True
        assert mark_info["/Suspects"] is False
