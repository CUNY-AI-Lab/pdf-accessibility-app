import asyncio

import pikepdf

from app.services.bookmark_intelligence import (
    BOOKMARK_DOCUMENT_CANDIDATE_PLAN_PROMPT,
    BOOKMARK_DOCUMENT_HEADING_SUPPLEMENT_PROMPT,
    BOOKMARK_DOCUMENT_LANDMARK_PLAN_PROMPT,
    _materialize_outline_entries_from_plan,
    _serialize_outline_candidates_for_direct_llm,
    _serialize_selected_outline_for_landmark_llm,
    collect_bookmark_heading_candidates,
    enhance_bookmark_structure_with_intelligence,
)


def _make_pdf(pdf_path, page_count=6):
    pdf = pikepdf.Pdf.new()
    for _ in range(page_count):
        pdf.add_blank_page(page_size=(200, 200))
    pdf.save(str(pdf_path))


def test_bookmark_prompts_stay_evidence_grounded():
    prompts = [
        BOOKMARK_DOCUMENT_CANDIDATE_PLAN_PROMPT,
        BOOKMARK_DOCUMENT_HEADING_SUPPLEMENT_PROMPT,
        BOOKMARK_DOCUMENT_LANDMARK_PLAN_PROMPT,
    ]

    for prompt in prompts:
        lowered = prompt.lower()
        assert "procedural waypoint" not in lowered
        assert "callout title" not in lowered
        assert "do not aggressively compress" not in lowered
        assert "cached pdf" in lowered or "visible document evidence" in lowered


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


def test_serialize_outline_candidates_for_direct_llm_keeps_label_variants_and_raw_text():
    serialized = _serialize_outline_candidates_for_direct_llm(
        [
            {
                "candidate_id": "heading:4",
                "source_kind": "heading",
                "source_index": 4,
                "preferred_label": "The concept of tagged PDF",
                "supported_labels": ["The concept of tagged PDF", "The concept of t agged PDF"],
                "raw_text": "The concept of t agged PDF",
                "target_page_index": 41,
                "source_page": 42,
                "default_level": 2,
            }
        ]
    )

    assert serialized == [
        {
            "candidate_id": "heading:4",
            "source_kind": "heading",
            "source_index": 4,
            "preferred_label": "The concept of tagged PDF",
            "supported_labels": ["The concept of tagged PDF", "The concept of t agged PDF"],
            "raw_text": "The concept of t agged PDF",
            "target_page_index": 41,
            "source_page": 42,
            "default_level": 2,
        }
    ]


def test_selected_outline_for_landmark_llm_keeps_outline_evidence_fields():
    outline_entries, _ = _materialize_outline_entries_from_plan(
        [{"candidate_id": "heading:4", "level": 2, "label_override": ""}],
        outline_candidates=[
            {
                "candidate_id": "heading:4",
                "source_kind": "heading",
                "source_index": 4,
                "preferred_label": "Adding IDs",
                "supported_labels": ["Adding IDs", "ADDING IDs"],
                "raw_text": "ADDING IDs",
                "target_page_index": 41,
                "source_page": 42,
                "default_level": 2,
                "previous_visible_label": "4.3.8. Footnotes and endnotes",
                "next_visible_label": "Procedure:",
                "previous_body_text": "Section overview paragraph",
                "following_body_text": "Every footnote must have a unique ID",
            }
        ],
    )

    serialized = _serialize_selected_outline_for_landmark_llm(outline_entries)

    assert serialized == [
        {
            "candidate_id": "heading:4",
            "source_kind": "heading",
            "label": "Adding IDs",
            "raw_text": "ADDING IDs",
            "supported_labels": ["Adding IDs", "ADDING IDs"],
            "level": 2,
            "page": 42,
            "previous_visible_label": "4.3.8. Footnotes and endnotes",
            "next_visible_label": "Procedure:",
            "previous_body_text": "Section overview paragraph",
            "following_body_text": "Every footnote must have a unique ID",
        }
    ]


def test_materialize_outline_entries_accepts_supported_label_field():
    outline_entries, seen_ids = _materialize_outline_entries_from_plan(
        [{"candidate_id": "heading:4", "level": 2, "supported_label": "Adding IDs"}],
        outline_candidates=[
            {
                "candidate_id": "heading:4",
                "source_kind": "heading",
                "source_index": 4,
                "preferred_label": "Adding IDs",
                "supported_labels": ["Adding IDs", "ADDING IDs"],
                "raw_text": "ADDING IDs",
                "target_page_index": 41,
                "source_page": 42,
                "default_level": 2,
            }
        ],
    )

    assert seen_ids == {"heading:4"}
    assert outline_entries == [
        {
            "candidate_id": "heading:4",
            "source_kind": "heading",
            "source_index": 4,
            "text": "Adding IDs",
            "raw_text": "ADDING IDs",
            "preferred_label": "Adding IDs",
            "supported_labels": ["Adding IDs", "ADDING IDs"],
            "page_index": 41,
            "level": 2,
            "previous_visible_label": None,
            "next_visible_label": None,
            "anchor_heading_text": None,
            "previous_body_text": None,
            "following_body_text": None,
        }
    ]


def test_bookmark_intelligence_uses_direct_gemini_cached_path(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, page_count=5)
    seen_calls: list[tuple[str, dict[str, object]]] = []
    deleted: list[str] = []

    async def _fake_create_cache(**kwargs):
        assert kwargs["pdf_path"] == pdf_path
        return type("CacheHandle", (), {"cache_name": "cache-1", "uploaded_file_name": "file-1"})()

    async def _fake_delete_cache(cache_handle, **kwargs):
        deleted.append(cache_handle.cache_name)

    async def _fake_cached_request(**kwargs):
        task_type = kwargs["response_schema"]["properties"]["task_type"]["enum"][0]
        seen_calls.append((task_type, kwargs["context_payload"]))
        if task_type == "bookmark_document_candidate_plan":
            assert "front_matter_page_candidates" in kwargs["context_payload"]
            return {
                "task_type": "bookmark_document_candidate_plan",
                "summary": "Built the bookmark skeleton.",
                "confidence": "high",
                "reason": "Grounded visible outline.",
                "front_matter_entries": [{"page": 1, "label": "Cover"}],
                "outline_entries": [
                    {"candidate_id": "toc:0", "supported_label": "TABLE OF CONTENTS", "level": 1},
                    {"candidate_id": "toc:1", "supported_label": "1 Intro", "level": 2},
                ],
            }
        if task_type == "bookmark_document_heading_supplement":
            return {
                "task_type": "bookmark_document_heading_supplement",
                "summary": "Added one useful visible subsection heading.",
                "confidence": "high",
                "reason": "The heading is visibly navigational.",
                "outline_entries": [
                    {"candidate_id": "heading:4", "supported_label": "Adding IDs", "level": 2},
                ],
            }
        assert task_type == "bookmark_document_landmark_plan"
        return {
            "task_type": "bookmark_document_landmark_plan",
            "summary": "Added one useful visible landmark.",
            "confidence": "high",
            "reason": "The paragraph behaves like a visible subsection title.",
            "selected_landmarks": [
                {
                    "page": 4,
                    "label": "Link texts and the Contents key",
                    "anchor_candidate_id": "heading:4",
                    "level": 3,
                }
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.create_direct_gemini_pdf_cache",
        _fake_create_cache,
    )
    monkeypatch.setattr(
        "app.services.bookmark_intelligence.delete_direct_gemini_pdf_cache",
        _fake_delete_cache,
    )
    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_direct_gemini_cached_json",
        _fake_cached_request,
    )

    structure_json = {
        "elements": [
            {"type": "heading", "text": "Manual Title", "page": 0, "level": 1},
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Intro", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Intro", "page": 2, "level": 1},
            {"type": "heading", "text": "Adding IDs", "page": 3, "level": 2},
            {"type": "paragraph", "text": "Link texts and the Contents key", "page": 3},
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

    assert [task for task, _payload in seen_calls] == [
        "bookmark_document_candidate_plan",
        "bookmark_document_heading_supplement",
        "bookmark_document_landmark_plan",
    ]
    assert deleted == ["cache-1"]
    assert [entry["text"] for entry in updated["bookmark_plan"]] == [
        "Cover",
        "TABLE OF CONTENTS",
        "1 Intro",
        "Adding IDs",
        "Link texts and the Contents key",
    ]
    assert audit["applied"] is True
    assert audit["selected_heading_count"] == 1
    assert audit["selected_landmark_count"] == 1
    assert audit["front_matter_applied"] is True


def test_bookmark_intelligence_uses_local_semantic_preview_path(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, page_count=5)
    seen_calls: list[tuple[str, list[dict[str, object]]]] = []

    async def _should_not_create_cache(**kwargs):
        raise AssertionError("local bookmark path should not create a Gemini cache")

    async def _should_not_delete_cache(*args, **kwargs):
        raise AssertionError("local bookmark path should not delete a Gemini cache")

    async def _should_not_request_cached(**kwargs):
        raise AssertionError("local bookmark path should not use cached Gemini requests")

    async def _fake_request_llm_json(
        *,
        llm_client,
        content,
        schema_name=None,
        response_schema=None,
        cache_breakpoint_index=None,
        conversation_prefix=None,
    ):
        seen_calls.append((schema_name, content))
        if schema_name == "bookmark_document_candidate_plan":
            return {
                "task_type": "bookmark_document_candidate_plan",
                "summary": "Built the bookmark skeleton.",
                "confidence": "high",
                "reason": "Grounded visible outline.",
                "front_matter_entries": [{"page": 1, "label": "Cover"}],
                "outline_entries": [
                    {"candidate_id": "toc:0", "supported_label": "TABLE OF CONTENTS", "level": 1},
                    {"candidate_id": "toc:1", "supported_label": "1 Intro", "level": 2},
                ],
            }
        if schema_name == "bookmark_document_heading_supplement":
            return {
                "task_type": "bookmark_document_heading_supplement",
                "summary": "Added one useful visible subsection heading.",
                "confidence": "high",
                "reason": "The heading is visibly navigational.",
                "outline_entries": [
                    {"candidate_id": "heading:4", "supported_label": "Adding IDs", "level": 2},
                ],
            }
        return {
            "task_type": "bookmark_document_landmark_plan",
            "summary": "Added one useful visible landmark.",
            "confidence": "high",
            "reason": "The paragraph behaves like a visible subsection title.",
            "selected_landmarks": [
                {
                    "page": 4,
                    "label": "Link texts and the Contents key",
                    "anchor_candidate_id": "heading:4",
                    "level": 3,
                }
            ],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.local_semantic_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.services.bookmark_intelligence.create_direct_gemini_pdf_cache",
        _should_not_create_cache,
    )
    monkeypatch.setattr(
        "app.services.bookmark_intelligence.delete_direct_gemini_pdf_cache",
        _should_not_delete_cache,
    )
    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_direct_gemini_cached_json",
        _should_not_request_cached,
    )
    monkeypatch.setattr(
        "app.services.bookmark_intelligence.page_preview_parts",
        lambda job, page_numbers: [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,page-{page_numbers[0]}"}}  # noqa: E501
        ],
    )
    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_llm_json",
        _fake_request_llm_json,
    )

    structure_json = {
        "elements": [
            {"type": "heading", "text": "Manual Title", "page": 0, "level": 1},
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Intro", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Intro", "page": 2, "level": 1},
            {"type": "heading", "text": "Adding IDs", "page": 3, "level": 2},
            {"type": "paragraph", "text": "Link texts and the Contents key", "page": 3},
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

    assert [schema_name for schema_name, _content in seen_calls] == [
        "bookmark_document_candidate_plan",
        "bookmark_document_heading_supplement",
        "bookmark_document_landmark_plan",
    ]
    assert all(any(item.get("type") == "image_url" for item in content) for _schema_name, content in seen_calls)
    assert [entry["text"] for entry in updated["bookmark_plan"]] == [
        "Cover",
        "TABLE OF CONTENTS",
        "1 Intro",
        "Adding IDs",
        "Link texts and the Contents key",
    ]
    assert audit["applied"] is True
    assert audit["selected_heading_count"] == 1
    assert audit["selected_landmark_count"] == 1


def test_bookmark_intelligence_uses_prefetched_front_matter_entries(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, page_count=4)
    seen_candidate_contexts: list[dict[str, object]] = []

    async def _fake_create_cache(**kwargs):
        return type("CacheHandle", (), {"cache_name": "cache-2", "uploaded_file_name": "file-2"})()

    async def _fake_delete_cache(cache_handle, **kwargs):
        return None

    async def _fake_cached_request(**kwargs):
        task_type = kwargs["response_schema"]["properties"]["task_type"]["enum"][0]
        if task_type == "bookmark_document_candidate_plan":
            seen_candidate_contexts.append(kwargs["context_payload"])
            return {
                "task_type": "bookmark_document_candidate_plan",
                "summary": "Built the bookmark skeleton.",
                "confidence": "high",
                "reason": "Grounded visible outline.",
                "front_matter_entries": [],
                "outline_entries": [
                    {"candidate_id": "toc:0", "supported_label": "TABLE OF CONTENTS", "level": 1},
                    {"candidate_id": "toc:1", "supported_label": "1 Intro", "level": 2},
                ],
            }
        if task_type == "bookmark_document_heading_supplement":
            return {
                "task_type": "bookmark_document_heading_supplement",
                "summary": "No extra headings.",
                "confidence": "high",
                "reason": "The skeleton already covers visible headings.",
                "outline_entries": [],
            }
        return {
            "task_type": "bookmark_document_landmark_plan",
            "summary": "No extra landmarks.",
            "confidence": "high",
            "reason": "No extra landmarks improve navigation.",
            "selected_landmarks": [],
        }

    monkeypatch.setattr(
        "app.services.bookmark_intelligence.create_direct_gemini_pdf_cache",
        _fake_create_cache,
    )
    monkeypatch.setattr(
        "app.services.bookmark_intelligence.delete_direct_gemini_pdf_cache",
        _fake_delete_cache,
    )
    monkeypatch.setattr(
        "app.services.bookmark_intelligence.request_direct_gemini_cached_json",
        _fake_cached_request,
    )

    structure_json = {
        "elements": [
            {"type": "heading", "text": "Manual Title", "page": 0, "level": 1},
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Intro", "page": 1, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Intro", "page": 2, "level": 1},
        ],
    }
    prefetched_entries = [
        {
            "candidate_id": "front:prefetched:0",
            "source_kind": "front_matter",
            "source_index": 0,
            "text": "Cover",
            "page_index": 0,
            "level": 1,
        }
    ]
    prefetched_audit = {
        "attempted": True,
        "applied": True,
        "reason": "prefetched_front_matter",
        "entry_count": 1,
    }

    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
            prefetched_front_matter_entries=prefetched_entries,
            prefetched_front_matter_audit=prefetched_audit,
        )
    )

    assert "front_matter_page_candidates" not in seen_candidate_contexts[0]
    assert updated["bookmark_plan"][0]["text"] == "Cover"
    assert audit["front_matter_applied"] is True
    assert "prefetched_front_matter" in audit["reason"]


def test_bookmark_intelligence_returns_early_without_toc():
    updated, audit = asyncio.run(
        enhance_bookmark_structure_with_intelligence(
            pdf_path="/tmp/unused.pdf",
            structure_json={"elements": [{"type": "heading", "text": "Title", "page": 0, "level": 1}]},
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated == {"elements": [{"type": "heading", "text": "Title", "page": 0, "level": 1}]}
    assert audit["attempted"] is False
    assert audit["reason"] == "no_toc_entries"
