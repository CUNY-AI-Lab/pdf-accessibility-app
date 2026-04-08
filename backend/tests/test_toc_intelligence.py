import asyncio
from types import SimpleNamespace

from app.services.intelligence_gemini_toc import generate_toc_group_intelligence
from app.services.toc_intelligence import (
    apply_toc_intelligence,
    collect_toc_candidates,
    enhance_toc_structure_with_intelligence,
)


def test_collect_toc_candidates_detects_heading_and_table_entries():
    structure_json = {
        "elements": [
            {"type": "heading", "text": "Contents", "page": 0},
            {"type": "table", "page": 0, "num_rows": 2, "num_cols": 3, "cells": [
                {"row": 0, "col": 0, "text": "1"},
                {"row": 0, "col": 1, "text": "Introduction"},
                {"row": 0, "col": 2, "text": "3"},
                {"row": 1, "col": 0, "text": "2"},
                {"row": 1, "col": 1, "text": "Installation"},
                {"row": 1, "col": 2, "text": "6"},
            ]},
            {"type": "heading", "text": "PDFlib GmbH ........ 7", "page": 0},
            {"type": "paragraph", "text": "Regular body text", "page": 1},
        ],
    }

    groups = collect_toc_candidates(structure_json)

    assert len(groups) == 1
    assert groups[0]["caption_index"] == 0
    assert [item["index"] for item in groups[0]["candidate_elements"][:2]] == [1, 2]
    assert groups[0]["candidate_elements"][0]["type"] == "table"
    assert groups[0]["candidate_elements"][1]["type"] == "heading"


def test_collect_toc_candidates_skips_when_toc_already_present():
    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "Contents", "page": 0},
            {"type": "toc_item", "text": "Introduction ........ 3", "page": 0},
        ],
    }

    assert collect_toc_candidates(structure_json) == []


def test_apply_toc_intelligence_marks_caption_and_entries():
    structure_json = {
        "elements": [
            {"type": "heading", "text": "Contents", "page": 0},
            {"type": "table", "page": 0, "num_rows": 2, "num_cols": 3, "cells": [
                {"row": 0, "col": 0, "text": "1"},
                {"row": 0, "col": 1, "text": "Introduction"},
                {"row": 0, "col": 2, "text": "3"},
                {"row": 1, "col": 0, "text": "2"},
                {"row": 1, "col": 1, "text": "Installation"},
                {"row": 1, "col": 2, "text": "6"},
            ]},
            {"type": "heading", "text": "PDFlib GmbH ........ 7", "page": 0},
        ],
    }
    suggestion = {
        "groups": [
            {
                "caption_index": 0,
                "is_toc": True,
                "confidence": "high",
                "entry_indexes": [1, 2],
                "entry_types": {
                    "1": "toc_item_table",
                    "2": "toc_item",
                },
                "caption_text_override": "TABLE OF CONTENTS",
                "entry_text_overrides": {
                    "2": "Installation",
                },
            }
        ]
    }

    audit = apply_toc_intelligence(structure_json, suggestion)

    assert audit["applied"] is True
    assert structure_json["elements"][0]["type"] == "toc_caption"
    assert structure_json["elements"][0]["text"] == "TABLE OF CONTENTS"
    assert [element["type"] for element in structure_json["elements"]] == [
        "toc_caption",
        "toc_item",
        "toc_item",
        "toc_item",
    ]
    assert structure_json["elements"][1]["text"] == "1 Introduction 3"
    assert structure_json["elements"][2]["text"] == "2 Installation 6"
    assert structure_json["elements"][3]["text"] == "Installation"
    assert structure_json["elements"][0]["toc_group_ref"] == structure_json["elements"][1]["toc_group_ref"]


def test_collect_toc_candidates_can_include_existing_toc_groups():
    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 0, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1. Intro . . . 1", "page": 0, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "2. Methods . . . 3", "page": 0, "toc_group_ref": "toc-0"},
        ],
    }

    groups = collect_toc_candidates(structure_json, include_existing=True)

    assert len(groups) == 1
    assert groups[0]["caption_text"] == "TABLE OF CONTENTS"
    assert [item["index"] for item in groups[0]["candidate_elements"]] == [1, 2]


def test_collect_toc_candidates_keeps_late_existing_toc_entries():
    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 0, "toc_group_ref": "toc-0"},
            *[
                {
                    "type": "toc_item",
                    "text": f"{index}. Section {index} .... {index + 2}",
                    "page": index // 10,
                    "toc_group_ref": "toc-0",
                }
                for index in range(1, 26)
            ],
        ],
    }

    groups = collect_toc_candidates(structure_json, include_existing=True)

    assert len(groups) == 1
    assert len(groups[0]["candidate_elements"]) == 25
    assert groups[0]["candidate_elements"][-1]["index"] == 25


def test_apply_toc_intelligence_repairs_existing_toc_item_text():
    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "Contents", "page": 0, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "LISTS List of Tables .... ii", "page": 0, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "Appendix A. AOP Meeting Summary .... 12 3", "page": 0, "toc_group_ref": "toc-0"},
        ],
    }
    suggestion = {
        "groups": [
            {
                "caption_index": 0,
                "is_toc": True,
                "confidence": "high",
                "entry_indexes": [1, 2],
                "entry_types": {"1": "toc_item", "2": "toc_item"},
                "entry_text_overrides": {
                    "1": "List of Tables",
                    "2": "Appendix A. AOP Meeting Summary",
                },
            }
        ]
    }

    audit = apply_toc_intelligence(structure_json, suggestion)

    assert audit["applied"] is True
    assert structure_json["elements"][1]["text"] == "List of Tables"
    assert structure_json["elements"][2]["text"] == "Appendix A. AOP Meeting Summary"


def test_enhance_toc_structure_chunks_large_existing_toc_groups(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    calls: list[list[int]] = []

    async def _fake_generate_toc_group_intelligence(*, pdf_path, original_filename, candidate_group, llm_client):
        indexes = [int(item["index"]) for item in candidate_group["candidate_elements"]]
        calls.append(indexes)
        last_index = indexes[-1]
        entry_text_overrides = {}
        if last_index >= 25:
            entry_text_overrides[str(last_index)] = "6.1 Reviewer Comments: Scup"
        return {
            "caption_index": 0,
            "is_toc": True,
            "confidence": "high",
            "reason": f"Processed indexes {indexes[0]}-{indexes[-1]}",
            "entry_indexes": indexes,
            "entry_types": {str(index): "toc_item" for index in indexes},
            "caption_text_override": "TABLE OF CONTENTS",
            "entry_text_overrides": entry_text_overrides,
        }

    monkeypatch.setattr(
        "app.services.toc_intelligence.generate_toc_group_intelligence",
        _fake_generate_toc_group_intelligence,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "Contents", "page": 0, "toc_group_ref": "toc-0"},
            *[
                {
                    "type": "toc_item",
                    "text": f"{index}. Section {index}",
                    "page": index // 10,
                    "toc_group_ref": "toc-0",
                }
                for index in range(1, 26)
            ],
        ],
    }

    updated, audit = asyncio.run(
        enhance_toc_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=SimpleNamespace(model="test-model"),
        )
    )

    assert len(calls) == 2
    assert calls[0][0] == 1
    assert calls[1][-1] == 25
    assert updated["elements"][25]["text"] == "6.1 Reviewer Comments: Scup"
    assert audit["applied"] is True
    assert audit["chunk_count"] == 2


def test_generate_toc_group_intelligence_can_use_direct_gemini(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_direct_request(**kwargs):
        assert kwargs["page_numbers"] == [1, 2]
        assert kwargs["context_payload"]["semantic_unit"]["caption_index"] == 0
        return {
            "task_type": "toc_group_intelligence",
            "summary": "Visible TOC confirmed.",
            "confidence": "high",
            "reason": "The pages show a standard TOC.",
            "is_toc": True,
            "entry_indexes": [1, 2],
            "entry_types": {"1": "toc_item", "2": "toc_item_table"},
            "caption_text_override": "TABLE OF CONTENTS",
            "entry_text_overrides": {"1": "Introduction"},
        }

    monkeypatch.setattr("app.services.intelligence_gemini_toc.direct_gemini_pdf_enabled", lambda: True)
    monkeypatch.setattr(
        "app.services.intelligence_gemini_toc.request_direct_gemini_pdf_json",
        _fake_direct_request,
    )

    result = asyncio.run(
        generate_toc_group_intelligence(
            pdf_path=pdf_path,
            original_filename="report.pdf",
            candidate_group={
                "caption_index": 0,
                "caption_text": "Contents",
                "pages": [1, 2],
                "candidate_elements": [
                    {"index": 1, "type": "paragraph", "text": "Introduction"},
                    {"index": 2, "type": "table", "text": ""},
                ],
            },
            llm_client=SimpleNamespace(model="test-model"),
        )
    )

    assert result["is_toc"] is True
    assert result["entry_indexes"] == [1, 2]
    assert result["entry_types"] == {"1": "toc_item", "2": "toc_item_table"}
    assert result["caption_text_override"] == "TABLE OF CONTENTS"
