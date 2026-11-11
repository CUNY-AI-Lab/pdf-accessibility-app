from types import SimpleNamespace

import pytest

from app.pipeline import structure
from app.pipeline.structure import docling_table_cell_matching


@pytest.mark.parametrize(
    ("classification", "expected"),
    [
        ("digital", True),
        ("mixed", True),
        ("scanned", False),
        ("ocr_scan", False),
        (None, True),
    ],
)
def test_cell_matching_is_off_only_for_scanned_documents(classification, expected):
    assert docling_table_cell_matching(classification) is expected


async def _submitted_form(tmp_path, monkeypatch, **kwargs) -> dict:
    submitted: dict = {}

    async def convert(_self, _client, base_url, timeout, **request_kwargs):
        submitted.update(request_kwargs["data"])
        return {"document": {"json_content": {}}}

    monkeypatch.setattr(structure.DoclingConversion, "run", convert)
    pdf = tmp_path / "page.pdf"
    pdf.write_bytes(b"%PDF-1.7 page")
    await structure._convert_via_docling_serve(
        pdf, tmp_path, "http://docling", 60, include_figure_images=False, **kwargs
    )
    return submitted


async def test_scanned_documents_ask_docling_not_to_match_cells(tmp_path, monkeypatch):
    form = await _submitted_form(tmp_path, monkeypatch, table_cell_matching=False)
    assert form["table_cell_matching"] == "false"
    assert form["do_table_structure"] == "true"


async def test_other_documents_keep_docling_cell_matching(tmp_path, monkeypatch):
    form = await _submitted_form(tmp_path, monkeypatch)
    assert "table_cell_matching" not in form


async def test_extract_structure_passes_the_setting_to_docling_serve(tmp_path, monkeypatch):
    seen = {}

    async def convert(*args, table_cell_matching=True, **kwargs):
        seen["table_cell_matching"] = table_cell_matching
        raise RuntimeError("stop")

    monkeypatch.setattr(structure, "_convert_via_docling_serve", convert)
    monkeypatch.setattr(
        structure,
        "get_settings",
        lambda: SimpleNamespace(
            docling_serve_url="http://docling",
            docling_serve_timeout=60,
            docling_serve_ocr_engine="rapidocr",
            docling_serve_token="",
        ),
    )
    with pytest.raises(RuntimeError, match="stop"):
        await structure.extract_structure(tmp_path / "page.pdf", tmp_path, table_cell_matching=False)
    assert seen == {"table_cell_matching": False}
