import asyncio

from app.services.bookmark_intelligence import (
    _build_landmark_candidate_chunks,
    _local_toc_entries_for_chunk,
    _serialize_outline_candidates_for_llm,
    collect_bookmark_heading_candidates,
    enhance_bookmark_structure_with_intelligence,
)


def test_collect_bookmark_heading_candidates_uses_toc_and_post_toc_headings():
    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Panel Report", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "U.S. Department of Commerce", "page": 1, "level": 6},
            {"type": "heading", "text": "2. ATLANTIC BLUEFISH", "page": 4, "level": 2},
            {"type": "paragraph", "text": "Bluefish stock status and management.", "page": 4},
            {"type": "heading", "text": "References", "page": 5, "level": 2},
        ],
    }

    payload = collect_bookmark_heading_candidates(structure_json)

    assert [item["text"] for item in payload["toc_entries"]] == ["TABLE OF CONTENTS", "1 Panel Report"]
    assert [item["text"] for item in payload["heading_candidates"]] == [
        "2. ATLANTIC BLUEFISH",
        "References",
    ]
    assert payload["pages"] == [3, 5, 6]
    assert payload["heading_candidates"][0]["section_key"] == "2"
    assert payload["heading_candidates"][0]["parent_section_key"] == "2"
    assert payload["heading_candidates"][0]["previous_heading_text"] == "U.S. Department of Commerce"
    assert payload["heading_candidates"][0]["next_heading_text"] == "References"
    assert payload["heading_candidates"][0]["following_body_text"] == "Bluefish stock status and management"


def test_collect_bookmark_heading_candidates_tracks_neighbor_body_text():
    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "4.3.8. Footnotes and endnotes", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "4.3.8. Footnotes and endnotes", "page": 40, "level": 1},
            {"type": "paragraph", "text": "Section overview paragraph.", "page": 40},
            {"type": "heading", "text": "Adding IDs", "page": 42, "level": 1},
            {"type": "paragraph", "text": "Every footnote must have a unique ID.", "page": 42},
            {"type": "heading", "text": "Procedure:", "page": 42, "level": 1},
        ],
    }

    payload = collect_bookmark_heading_candidates(structure_json)
    adding_ids = next(entry for entry in payload["heading_candidates"] if entry["text"] == "Adding IDs")

    assert adding_ids["previous_body_text"] == "Section overview paragraph"
    assert adding_ids["following_body_text"] == "Every footnote must have a unique ID"


def test_collect_bookmark_heading_candidates_tracks_landmark_body_text():
    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "3 Links", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "3 Links", "page": 30, "level": 1},
            {"type": "paragraph", "text": "Introductory body paragraph.", "page": 30},
            {"type": "paragraph", "text": "Link texts and the Contents key", "page": 31},
            {"type": "paragraph", "text": "Meaningful link texts help users understand the purpose of the link.", "page": 31},
        ],
    }

    payload = collect_bookmark_heading_candidates(structure_json)
    landmark = next(entry for entry in payload["landmark_candidates"] if entry["text"] == "Link texts and the Contents key")

    assert landmark["previous_body_text"] == "Introductory body paragraph"
    assert landmark["following_body_text"] == "Meaningful link texts help users understand the purpose of the link"


def test_collect_bookmark_heading_candidates_tracks_toc_target_pages():
    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Introduction", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "9 Resources", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Introduction", "page": 5, "level": 1},
            {"type": "heading", "text": "9 Resources", "page": 80, "level": 1},
        ],
    }

    payload = collect_bookmark_heading_candidates(structure_json)

    assert [item["target_page"] for item in payload["toc_entries"]] == [None, 6, 81]
    assert [item["target_index"] for item in payload["toc_entries"]] == [None, 3, 4]


def test_landmark_candidate_chunks_follow_toc_and_selected_heading_anchors():
    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Introduction", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Introduction", "page": 3, "level": 1},
            {"type": "paragraph", "text": "Visible intro landmark", "page": 4},
            {"type": "heading", "text": "1.1 Background", "page": 5, "level": 2},
            {"type": "paragraph", "text": "Background checklist", "page": 6},
            {"type": "heading", "text": "2 Methods", "page": 7, "level": 1},
            {"type": "paragraph", "text": "Methods note", "page": 8},
        ],
    }

    payload = collect_bookmark_heading_candidates(structure_json)
    chunks = _build_landmark_candidate_chunks(payload, selected_indexes={4, 6})

    assert len(chunks) == 1
    chunk = chunks[0]
    assert [entry["index"] for entry in chunk["heading_candidates"]] == [2, 4, 6]
    assert [entry["index"] for entry in chunk["landmark_candidates"]] == [3, 5, 7]
    assert [entry["anchor_heading_text"] for entry in chunk["landmark_candidates"]] == [
        "1 Introduction",
        "1.1 Background",
        "2 Methods",
    ]
    assert chunk["pages"] == [4, 5, 6, 7, 8, 9]


def test_local_toc_entries_for_chunk_prefers_matching_target_pages():
    toc_entries = [
        {"text": "TABLE OF CONTENTS", "page": 2, "target_page": None},
        {"text": "1 Introduction", "page": 2, "target_page": 6},
        {"text": "2 Standards", "page": 2, "target_page": 12},
        {"text": "9 Resources", "page": 3, "target_page": 81},
        {"text": "10 Appendix", "page": 3, "target_page": 92},
    ]

    local_entries = _local_toc_entries_for_chunk(
        toc_entries,
        chunk_pages=[80, 81, 82],
        max_items=3,
    )

    assert [entry["text"] for entry in local_entries] == [
        "2 Standards",
        "9 Resources",
        "10 Appendix",
    ]


def test_serialize_outline_candidates_for_llm_omits_empty_and_redundant_fields():
    serialized = _serialize_outline_candidates_for_llm(
        [
            {
                "candidate_id": "heading:4",
                "source_kind": "heading",
                "source_index": 4,
                "preferred_label": "Adding IDs",
                "supported_labels": ["Adding IDs"],
                "target_page_index": 41,
                "source_page": 42,
                "default_level": 2,
                "previous_visible_label": "",
                "next_visible_label": "",
                "anchor_heading_text": "",
                "previous_body_text": "",
                "following_body_text": "",
            }
        ]
    )

    assert serialized == [
        {
            "candidate_id": "heading:4",
            "source_kind": "heading",
            "source_index": 4,
            "preferred_label": "Adding IDs",
            "target_page_index": 41,
            "source_page": 42,
            "default_level": 2,
        }
    ]


def test_bookmark_intelligence_marks_selected_headings(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "Promoted major section headings beyond the TOC.",
                "confidence": "high",
                "reason": "These headings add navigational landmarks not already covered by the TOC.",
                "selected_heading_indexes": [3],
                "label_overrides": {"3": "2 Atlantic Bluefish"},
            }
        assert schema_name == "bookmark_outline_plan"
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Built the final outline from TOC entries and selected headings.",
            "confidence": "high",
            "reason": "The TOC caption should stay at the root and the selected heading should supplement it.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": ""},
                {"candidate_id": "heading:3", "level": 2, "label_override": "2 Atlantic Bluefish"},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Panel Report", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "U.S. Department of Commerce", "page": 1, "level": 6},
            {"type": "heading", "text": "2. ATLANTIC BLUEFISH", "page": 4, "level": 2},
        ],
    }

    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["elements"][3]["bookmark_include"] is True
    assert updated["elements"][3]["bookmark_text_override"] == "2 Atlantic Bluefish"
    assert audit["applied"] is True
    assert audit["selected_heading_count"] == 1
    assert audit["front_matter_applied"] is False


def test_bookmark_intelligence_materializes_outline_plan(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "Promoted major section headings beyond the TOC.",
                "confidence": "high",
                "reason": "These headings add navigational landmarks not already covered by the TOC.",
                "selected_heading_indexes": [3],
                "label_overrides": {"3": "2 Atlantic Bluefish"},
            }
        assert schema_name == "bookmark_outline_plan"
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Built a clean final outline from TOC entries and selected headings.",
            "confidence": "high",
            "reason": "The TOC caption should be the root, with the section and promoted heading beneath it.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": ""},
                {"candidate_id": "heading:3", "level": 2, "label_override": "2 Atlantic Bluefish"},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Panel Report", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Panel Report", "page": 3, "level": 1},
            {"type": "heading", "text": "2. ATLANTIC BLUEFISH", "page": 4, "level": 2},
        ],
    }

    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["bookmark_plan"] == [
        {
            "candidate_id": "toc:0",
            "source_kind": "toc",
            "source_index": 0,
            "text": "TABLE OF CONTENTS",
            "page_index": 2,
            "level": 1,
        },
        {
            "candidate_id": "toc:1",
            "source_kind": "toc",
            "source_index": 1,
            "text": "1 Panel Report",
            "page_index": 3,
            "level": 2,
        },
        {
            "candidate_id": "heading:3",
            "source_kind": "heading",
            "source_index": 3,
            "text": "2 Atlantic Bluefish",
            "page_index": 4,
            "level": 2,
        },
    ]
    assert audit["outline_plan_applied"] is True
    assert audit["outline_entry_count"] == 3


def test_bookmark_intelligence_materializes_non_heading_landmark_candidates(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "No extra headings beyond the TOC.",
                "confidence": "high",
                "reason": "The TOC already covers the heading structure.",
                "selected_heading_indexes": [],
                "label_overrides": {},
            }
        if schema_name == "bookmark_landmark_selection":
            return {
                "task_type": "bookmark_landmark_selection",
                "summary": "Selected one visible landmark that Docling typed as a paragraph.",
                "confidence": "high",
                "reason": "This paragraph functions as a standalone navigational landmark.",
                "selected_candidate_indexes": [3],
                "label_overrides": {},
            }
        assert schema_name == "bookmark_outline_plan"
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Built the final outline from TOC entries and the landmark.",
            "confidence": "high",
            "reason": "The standalone landmark should sit under the same section hierarchy.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": ""},
                {"candidate_id": "landmark:3", "level": 3, "label_override": ""},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Panel Report", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Panel Report", "page": 3, "level": 1},
            {"type": "paragraph", "text": "Link texts and the Contents key", "page": 4},
        ],
    }

    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["bookmark_plan"] == [
        {
            "candidate_id": "toc:0",
            "source_kind": "toc",
            "source_index": 0,
            "text": "TABLE OF CONTENTS",
            "page_index": 2,
            "level": 1,
        },
        {
            "candidate_id": "toc:1",
            "source_kind": "toc",
            "source_index": 1,
            "text": "1 Panel Report",
            "page_index": 3,
            "level": 2,
        },
        {
            "candidate_id": "landmark:3",
            "source_kind": "landmark",
            "source_index": 3,
            "text": "Link texts and the Contents key",
            "page_index": 4,
            "level": 3,
        },
    ]
    assert audit["selected_landmark_count"] == 1
    assert audit["outline_plan_applied"] is True


def test_bookmark_intelligence_prepends_front_matter_entries(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "Identified the three pre-TOC page roles.",
                "confidence": "high",
                "reason": "The first three pages act as a cover, inside-cover, and series-information page.",
                "entries": [
                    {"page_index": 0, "label": "Cover"},
                    {"page_index": 1, "label": "Inside-Cover page"},
                    {"page_index": 2, "label": "Series Information"},
                ],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "No extra headings beyond the TOC.",
                "confidence": "high",
                "reason": "The TOC already covers the visible structure.",
                "selected_heading_indexes": [],
                "label_overrides": {},
            }
        assert schema_name == "bookmark_outline_plan"
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Built the TOC outline.",
            "confidence": "high",
            "reason": "The TOC should remain intact after the front-matter entries.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": ""},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "heading", "text": "Management Track Assessments Spring 2023", "page": 0, "level": 1},
            {"type": "heading", "text": "U.S. Department of Commerce", "page": 1, "level": 1},
            {"type": "heading", "text": "NOAA Technical Memorandum, Editorial Notes", "page": 2, "level": 1},
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 3, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Panel Report", "page": 3, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Panel Report", "page": 4, "level": 1},
        ],
    }

    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["bookmark_plan"][:3] == [
        {
            "candidate_id": "front:0",
            "source_kind": "front_matter",
            "source_index": 0,
            "text": "Cover",
            "page_index": 0,
            "level": 1,
        },
        {
            "candidate_id": "front:1",
            "source_kind": "front_matter",
            "source_index": 1,
            "text": "Inside-Cover page",
            "page_index": 1,
            "level": 1,
        },
        {
            "candidate_id": "front:2",
            "source_kind": "front_matter",
            "source_index": 2,
            "text": "Series Information",
            "page_index": 2,
            "level": 1,
        },
    ]
    assert audit["front_matter_applied"] is True
    assert audit["front_matter_entry_count"] == 3


def test_bookmark_intelligence_passes_duplicate_evidence_to_outline_llm(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    outline_contexts: list[str] = []

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "Selected one major section heading.",
                "confidence": "high",
                "reason": "This heading is a real navigation landmark.",
                "selected_heading_indexes": [2],
                "label_overrides": {},
            }
        outline_contexts.append(
            next(
                item["text"]
                for item in content
                if item.get("type") == "text" and item["text"].startswith("Bookmark outline context:")
            )
        )
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Built the final outline from evidence-backed candidates.",
            "confidence": "high",
            "reason": "The TOC item and matching heading refer to the same section.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": ""},
                {"candidate_id": "heading:2", "level": 2, "label_override": ""},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {
                "type": "toc_item",
                "text": "7.1. Automated testing with PAC: PDF Accessibility Checker........59",
                "page": 2,
                "toc_group_ref": "toc-0",
            },
            {
                "type": "heading",
                "text": "7.1. Automated testing with PAC: PDF Accessibility Checker",
                "page": 58,
                "level": 2,
            },
        ],
    }

    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["elements"][2]["bookmark_include"] is True
    assert updated["bookmark_plan"] == [
        {
            "candidate_id": "toc:0",
            "source_kind": "toc",
            "source_index": 0,
            "text": "TABLE OF CONTENTS",
            "page_index": 2,
            "level": 1,
        },
        {
            "candidate_id": "toc:1",
            "source_kind": "toc",
            "source_index": 1,
            "text": "7.1. Automated testing with PAC: PDF Accessibility Checker",
            "page_index": 58,
            "level": 2,
        },
    ]
    assert '"candidate_id": "heading:2"' in outline_contexts[0]
    assert audit["outline_entry_count"] == 3


def test_bookmark_intelligence_discards_unsupported_outline_label_override(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "Selected one major section heading.",
                "confidence": "high",
                "reason": "This heading is a real navigation landmark.",
                "selected_heading_indexes": [2],
                "label_overrides": {},
            }
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Built the final outline from evidence-backed candidates.",
            "confidence": "high",
            "reason": "The TOC item and matching heading refer to the same section.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": "7.1. Automated testing with PAC (Landmark)"},
                {"candidate_id": "heading:2", "level": 2, "label_override": ""},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {
                "type": "toc_item",
                "text": "7.1. Automated testing with PAC: PDF Accessibility Checker........59",
                "page": 2,
                "toc_group_ref": "toc-0",
            },
            {
                "type": "heading",
                "text": "7.1. Automated testing with PAC: PDF Accessibility Checker",
                "page": 58,
                "level": 2,
            },
        ],
    }

    updated, _audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["bookmark_plan"][1]["text"] == "7.1. Automated testing with PAC: PDF Accessibility Checker"


def test_bookmark_intelligence_keeps_raw_visible_heading_label_available_to_outline_llm(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "Selected the visible references heading.",
                "confidence": "high",
                "reason": "This is a real navigation landmark.",
                "selected_heading_indexes": [3],
                "label_overrides": {"3": "References (Atlantic halibut)"},
            }
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Use the raw visible references label in the final outline.",
            "confidence": "high",
            "reason": "The visible heading is just References.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": ""},
                {"candidate_id": "heading:3", "level": 2, "label_override": "References"},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "2 Atlantic halibut", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "2 Atlantic halibut", "page": 20, "level": 1},
            {"type": "heading", "text": "References", "page": 24, "level": 2},
        ],
    }

    updated, _audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["bookmark_plan"][-1]["text"] == "References"


def test_bookmark_intelligence_keeps_raw_visible_heading_label_available_for_toc_candidate(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    outline_contexts = []

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "No extra heading bookmarks needed.",
                "confidence": "high",
                "reason": "The section is already represented by the TOC.",
                "selected_heading_indexes": [],
                "label_overrides": {},
            }
        outline_contexts.append(
            next(
                item["text"]
                for item in content
                if item.get("type") == "text" and item["text"].startswith("Bookmark outline context:")
            )
        )
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Use the raw visible heading variant for the final outline.",
            "confidence": "high",
            "reason": "The raw visible heading includes the suffix used by the gold outline.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": ""},
                {"candidate_id": "heading_variant:1", "level": 2, "label_override": "6 Overview of PDF Standard Tags 15"},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "6 Overview of PDF Standard Tags....................................53", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "6 Overview of PDF Standard Tags 15", "page": 52, "level": 1},
        ],
    }

    updated, _audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["bookmark_plan"][2]["candidate_id"] == "heading_variant:1"
    assert updated["bookmark_plan"][2]["text"] == "6 Overview of PDF Standard Tags 15"
    assert '"6 Overview of PDF Standard Tags 15"' in outline_contexts[0]


def test_bookmark_intelligence_passes_landmark_context_into_outline_prompt(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    outline_contexts = []

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "No extra heading bookmarks needed.",
                "confidence": "high",
                "reason": "The main section heading is already covered by the TOC.",
                "selected_heading_indexes": [],
                "label_overrides": {},
            }
        if schema_name == "bookmark_landmark_selection":
            return {
                "task_type": "bookmark_landmark_selection",
                "summary": "Selected the paragraph landmark.",
                "confidence": "high",
                "reason": "The paragraph behaves like a subsection title.",
                "selected_candidate_indexes": [4],
                "label_overrides": {"4": "Link texts and the Contents key"},
            }
        outline_contexts.append(
            next(
                item["text"]
                for item in content
                if item.get("type") == "text" and item["text"].startswith("Bookmark outline context:")
            )
        )
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Built the final outline with the selected landmark.",
            "confidence": "high",
            "reason": "The selected landmark adds useful local navigation.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": ""},
                {"candidate_id": "landmark:4", "level": 3, "label_override": "Link texts and the Contents key"},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "3 Links", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "3 Links", "page": 30, "level": 1},
            {"type": "paragraph", "text": "Introductory body paragraph.", "page": 30},
            {"type": "paragraph", "text": "Link texts and the Contents key", "page": 31},
            {"type": "paragraph", "text": "Meaningful link texts help users understand the purpose of the link.", "page": 31},
        ],
    }

    updated, _audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["bookmark_plan"][-1]["candidate_id"] == "landmark:4"
    assert '"previous_body_text": "Introductory body paragraph"' in outline_contexts[0]
    assert '"following_body_text": "Meaningful link texts help users understand the purpose of the link"' in outline_contexts[0]


def test_bookmark_intelligence_ignores_invalid_model_indexes(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "Selected one visible heading and one invalid index.",
                "confidence": "high",
                "reason": "Only the visible heading should survive validation.",
                "selected_heading_indexes": [3, 999],
                "label_overrides": {"3": "2 Atlantic Bluefish", "999": "Ignore me"},
            }
        if schema_name == "bookmark_landmark_selection":
            return {
                "task_type": "bookmark_landmark_selection",
                "summary": "Selected one valid landmark and one hallucinated index.",
                "confidence": "high",
                "reason": "Only the provided landmark candidate should survive validation.",
                "selected_candidate_indexes": [4, 777],
                "label_overrides": {"4": "Link texts and the Contents key", "777": "Ignore me too"},
            }
        assert schema_name == "bookmark_outline_plan"
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Built the final outline from valid TOC, heading, and landmark candidates.",
            "confidence": "high",
            "reason": "Only valid candidate ids should be materialized.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": ""},
                {"candidate_id": "heading:3", "level": 2, "label_override": "2 Atlantic Bluefish"},
                {"candidate_id": "landmark:4", "level": 3, "label_override": "Link texts and the Contents key"},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Panel Report", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Panel Report", "page": 3, "level": 1},
            {"type": "heading", "text": "2. ATLANTIC BLUEFISH", "page": 4, "level": 2},
            {"type": "paragraph", "text": "Link texts and the Contents key", "page": 5},
        ],
    }

    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["elements"][3]["bookmark_include"] is True
    assert updated["elements"][3]["bookmark_text_override"] == "2 Atlantic Bluefish"
    assert all("Ignore me" not in item["text"] for item in updated["bookmark_plan"])
    assert [item["candidate_id"] for item in updated["bookmark_plan"]] == [
        "toc:0",
        "toc:1",
        "heading:3",
        "landmark:4",
    ]
    assert audit["selected_heading_count"] == 1
    assert audit["selected_landmark_count"] == 1


def test_bookmark_intelligence_splits_large_candidate_sets_into_chunks(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    calls = []

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        calls.append((schema_name, content))
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            context_text = next(
                item["text"]
                for item in content
                if item.get("type") == "text" and item["text"].startswith("Bookmark planning context:")
            )
            if '"chunk_index": 1' in context_text:
                return {
                    "task_type": "bookmark_heading_selection",
                    "summary": "Selected a late heading from the second chunk.",
                    "confidence": "medium",
                    "reason": "This heading is a real late-document section.",
                    "selected_heading_indexes": [27],
                    "label_overrides": {},
                }
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "No extra headings in this chunk.",
                "confidence": "high",
                "reason": "No durable extra landmarks here.",
                "selected_heading_indexes": [],
                "label_overrides": {},
            }
        assert schema_name == "bookmark_outline_plan"
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Built the final outline from the promoted late heading.",
            "confidence": "high",
            "reason": "The TOC caption should stay at the root and the late heading should supplement it.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": ""},
                {"candidate_id": "heading:27", "level": 2, "label_override": ""},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    elements = [
        {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 1, "toc_group_ref": "toc-0"},
        {"type": "toc_item", "text": "1 Panel Report", "page": 1, "toc_group_ref": "toc-0"},
    ]
    for idx in range(30):
        elements.append({
            "type": "heading",
            "text": f"{idx + 1}. Heading {idx + 1}",
            "page": idx + 2,
            "level": 1,
        })
    structure_json = {"elements": elements}

    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert len([call for call in calls if call[0] == "bookmark_heading_selection"]) == 4
    assert updated["elements"][27]["bookmark_include"] is True
    assert audit["applied"] is True
    assert audit["chunk_count"] == 4


def test_bookmark_intelligence_rejects_outline_plan_missing_required_toc_entries(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    outline_calls = 0

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        nonlocal outline_calls
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "Selected one extra heading.",
                "confidence": "high",
                "reason": "This heading adds navigation.",
                "selected_heading_indexes": [3],
                "label_overrides": {},
            }
        outline_calls += 1
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Incomplete outline.",
            "confidence": "high",
            "reason": "Returning only the selected heading.",
            "outline_entries": [
                {"candidate_id": "heading:3", "level": 2, "label_override": ""},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Panel Report", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Panel Report", "page": 3, "level": 1},
            {"type": "heading", "text": "2. ATLANTIC BLUEFISH", "page": 4, "level": 2},
        ],
    }

    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert outline_calls == 2
    assert "bookmark_plan" not in updated
    assert audit["outline_plan_applied"] is False
    assert audit["outline_entry_count"] == 0


def test_bookmark_intelligence_ignores_landmark_chunk_timeout(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "Selected one extra heading.",
                "confidence": "high",
                "reason": "This heading adds navigation.",
                "selected_heading_indexes": [3],
                "label_overrides": {},
            }
        if schema_name == "bookmark_landmark_selection":
            raise TimeoutError("landmark selection stalled")
        assert schema_name == "bookmark_outline_plan"
        return {
            "task_type": "bookmark_outline_plan",
            "summary": "Built the final outline from TOC and headings.",
            "confidence": "high",
            "reason": "The heading supplement still improves navigation.",
            "outline_entries": [
                {"candidate_id": "toc:0", "level": 1, "label_override": ""},
                {"candidate_id": "toc:1", "level": 2, "label_override": ""},
                {"candidate_id": "heading:3", "level": 2, "label_override": ""},
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Panel Report", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Panel Report", "page": 3, "level": 1},
            {"type": "heading", "text": "2. ATLANTIC BLUEFISH", "page": 4, "level": 2},
            {"type": "paragraph", "text": "Link texts and the Contents key", "page": 5},
        ],
    }

    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["bookmark_plan"][-1]["candidate_id"] == "heading:3"
    assert audit["landmark_chunk_failures"] == 1
    assert audit["outline_plan_applied"] is True


def test_bookmark_intelligence_survives_outline_timeout(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        if schema_name == "bookmark_front_matter":
            return {
                "task_type": "bookmark_front_matter",
                "summary": "No front-matter role bookmarks needed.",
                "confidence": "high",
                "reason": "The visible evidence does not justify any extra front-matter roles.",
                "entries": [],
            }
        if schema_name == "bookmark_heading_selection":
            return {
                "task_type": "bookmark_heading_selection",
                "summary": "Selected one extra heading.",
                "confidence": "high",
                "reason": "This heading adds navigation.",
                "selected_heading_indexes": [3],
                "label_overrides": {},
            }
        if schema_name == "bookmark_outline_plan":
            raise TimeoutError("outline stalled")
        return {
            "task_type": "bookmark_landmark_selection",
            "summary": "No extra landmarks.",
            "confidence": "high",
            "reason": "No extra landmark is needed.",
            "selected_candidate_indexes": [],
            "label_overrides": {},
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Panel Report", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Panel Report", "page": 3, "level": 1},
            {"type": "heading", "text": "2. ATLANTIC BLUEFISH", "page": 4, "level": 2},
        ],
    }

    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert "bookmark_plan" not in updated
    assert updated["elements"][3]["bookmark_include"] is True
    assert audit["outline_plan_applied"] is False
