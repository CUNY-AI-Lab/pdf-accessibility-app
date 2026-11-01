"""Recorded table decisions reach spoken output without replacing known headers."""

from types import SimpleNamespace

import pikepdf
import pytest

from app.pipeline import tagger
from app.services import intelligence_semantics
from app.services.semantic_units import SemanticUnit
from app.services.structure_text import screen_reader_text
from app.services.table_decisions import queue_table_decisions
from tests.pdf_fixtures import rendered
from tests.test_tagger_table_recovery import signature
from tests.test_tagger_tables import _fixture, _tables

ROWS = [["Alpha", "one", "first"], ["Beta", "two", "second"], ["Gamma", "three", "third"]]


async def output(tmp_path, monkeypatch, decision, *, form=False, headers=None, rows=None):
    source = tmp_path / "input.pdf"
    structure = _fixture(source, rows or ROWS, form=form, headers=headers)
    structure["elements"][0]["review_id"] = "review-0"
    proposed, queued = queue_table_decisions(
        structure, [dict(table_review_id="review-0", page=1, **decision)]
    )
    before, after = tmp_path / "before.pdf", tmp_path / "after.pdf"
    await tagger.tag_pdf(source, before, structure, [], language="en")
    result = await tagger.tag_pdf(source, after, proposed, [], language="en")
    assert signature(before) == signature(after)
    assert rendered(before) == rendered(after)
    return before, after, result, proposed, queued


@pytest.mark.parametrize("field", ["action", "decision", "suggested_action"])
async def test_recorded_table_action_aliases_keep_payload_without_confidence(monkeypatch, field):
    async def request(**kwargs):
        return {
            field: "set_table_headers",
            "header_rows": [0],
            "row_header_columns": [0],
            "explanation": "Visible labels.",
        }

    monkeypatch.setattr(intelligence_semantics, "request_llm_json", request)
    d = await intelligence_semantics.adjudicate_semantic_unit(
        job=None,
        unit=SemanticUnit(
            unit_id="review-3", unit_type="table", page=1, accessibility_goal="Read table"
        ),
        llm_client=SimpleNamespace(model="offline"),
    )
    assert d.suggested_action == "set_table_headers"
    assert d.header_rows == [0] and d.row_header_columns == [0]
    assert d.reason == "Visible labels."


@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize("scope", ["Column", "Row"])
async def test_missing_confidence_adds_headers_and_associations(tmp_path, monkeypatch, form, scope):
    decision = dict(
        action="set_table_headers",
        header_rows=[0] if scope == "Column" else [],
        row_header_columns=[0] if scope == "Row" else [],
    )
    _, after, result, _, queued = await output(tmp_path, monkeypatch, decision, form=form)
    assert len(queued) == 1
    assert result.table_intelligence_audit[0]["status"] == "applied_headers"
    with pikepdf.open(after) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        header = table.K[0].K[0]
        assert header.S == "/TH" and str(header.A.Scope) == f"/{scope}"
        target = table.K[1].K[0] if scope == "Column" else table.K[0].K[1]
        assert header.ID in target.A.Headers
    assert "<th>Alpha</th>" in screen_reader_text(after, markdown=True)


@pytest.mark.parametrize("kind", ["layout_grid", "text_block", "toc_group"])
@pytest.mark.parametrize("form", [False, True])
async def test_model_layout_uses_existing_layout_owners_and_beats_model_headers(
    tmp_path, monkeypatch, kind, form
):
    source = tmp_path / "input.pdf"
    structure = _fixture(source, ROWS, form=form)
    structure["elements"][0]["review_id"] = "review-0"
    items = [
        dict(
            table_review_id="review-0",
            page=1,
            suggested_action="set_table_headers",
            header_rows=[0],
        ),
        dict(
            table_review_id="review-0",
            page=1,
            suggested_action="reclassify_region",
            resolved_kind=kind,
        ),
    ]
    queued, _ = queue_table_decisions(structure, items)
    before, after = tmp_path / "before.pdf", tmp_path / "after.pdf"
    await tagger.tag_pdf(source, before, structure, [], language="en")
    result = await tagger.tag_pdf(source, after, queued, [], language="en")
    assert signature(before) == signature(after)
    assert rendered(before) == rendered(after)
    with pikepdf.open(after) as pdf:
        assert list(_tables(pdf.Root.StructTreeRoot)) == []
        assert pdf.Root.StructTreeRoot.K[0].K[0].S == "/Div"
    text = screen_reader_text(after, markdown=True)
    assert "<table>" not in text
    assert all(word in text for row in ROWS for word in row)
    assert {x["status"] for x in result.table_intelligence_audit} == {
        "applied_layout",
        "superseded_by_layout",
    }
    if kind == "toc_group":
        with pikepdf.open(after) as pdf:
            listing = pdf.Root.StructTreeRoot.K[0].K[0].K[0]
            assert listing.S == "/L" and all(k.S == "/LI" for k in listing.K)


@pytest.mark.parametrize(
    "decision",
    [
        {"header_rows": [3]},
        {"header_rows": [-1, 0]},
        {"header_rows": [True]},
        {"row_header_columns": [3]},
        {"header_rows": ["0"]},
        {"header_rows": []},
    ],
)
async def test_invalid_or_empty_selection_does_not_change_table(tmp_path, monkeypatch, decision):
    _, after, result, _, queued = await output(
        tmp_path, monkeypatch, dict(suggested_action="set_table_headers", **decision)
    )
    assert not queued and result.table_intelligence_audit == []
    assert "<th>" not in screen_reader_text(after, markdown=True)


@pytest.mark.parametrize("action", ["set_table_headers", "reclassify_region"])
async def test_extractor_headers_are_never_removed_or_replaced(tmp_path, monkeypatch, action):
    decision = dict(suggested_action=action, header_rows=[2], resolved_kind="layout_grid")
    _, after, result, _, queued = await output(
        tmp_path, monkeypatch, decision, headers={(0, 1): "Column"}
    )
    assert not queued
    with pikepdf.open(after) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert table.K[0].K[1].S == "/TH"
        assert table.K[2].K[0].S == "/TD"


async def test_deterministic_row_headers_win_over_model_column_headers(tmp_path, monkeypatch):
    rows = [
        ["Index", "A numerical identifier for an individual failure condition"],
        ["Section", "A reference to the relevant section of the protocol"],
    ]
    _, after, result, _, _ = await output(
        tmp_path,
        monkeypatch,
        dict(suggested_action="set_table_headers", header_rows=[0]),
        rows=rows,
    )
    with pikepdf.open(after) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert table.K[0].K[0].A.Scope == "/Row"
        assert table.K[0].K[1].S == "/TD"
    assert result.table_intelligence_audit[0]["status"] == "preserved_existing_headers"


async def test_form_region_keeps_table_and_uses_other_header_answer(tmp_path, monkeypatch):
    source = tmp_path / "input.pdf"
    structure = _fixture(source, ROWS)
    structure["elements"][0]["review_id"] = "review-0"
    structure, _ = queue_table_decisions(
        structure,
        [
            dict(
                table_review_id="review-0",
                suggested_action="reclassify_region",
                resolved_kind="form_region",
            ),
            dict(
                table_review_id="review-0",
                suggested_action="set_table_headers",
                row_header_columns=[0],
            ),
        ],
    )
    result = await tagger.tag_pdf(source, tmp_path / "after.pdf", structure, [], language="en")
    assert [x["status"] for x in result.table_intelligence_audit] == [
        "kept_table",
        "applied_headers",
    ]
    assert "<th>Alpha</th>" in screen_reader_text(tmp_path / "after.pdf", markdown=True)


async def test_page_text_intelligence_does_not_drop_table_payload(monkeypatch):
    from app.services import intelligence_pages
    from app.services.semantic_units import SemanticDecision

    async def decide(**kwargs):
        return [
            SemanticDecision(
                unit_id="review-0",
                unit_type="table",
                summary="",
                confidence="low",
                confidence_score=0.4,
                suggested_action="set_table_headers",
                reason="labels",
                header_rows=[0],
            )
        ]

    monkeypatch.setattr(intelligence_pages, "adjudicate_semantic_units", decide)
    result = await intelligence_pages.generate_suspicious_text_intelligence(
        job=None,
        page_numbers=[1],
        suspicious_blocks=[dict(page=1, review_id="review-0", role="table")],
        llm_client=None,
    )
    assert result["table_intelligence"][0]["header_rows"] == [0]
    assert result["blocks"] == []


@pytest.mark.parametrize("shape", ["single", "keyed", "list"])
async def test_batch_answer_shapes_are_bound_to_the_requested_table(monkeypatch, shape):
    from app.services import intelligence_tables

    raw = dict(action="set_table_headers", header_rows=[0], row_header_columns=[])
    answer = (
        raw
        if shape == "single"
        else {"review-0": raw}
        if shape == "keyed"
        else {"decisions": [dict(table_review_id="review-0", **raw)]}
    )

    async def request(**kwargs):
        return answer, {}

    monkeypatch.setattr(intelligence_tables, "request_llm_json_with_response", request)
    result = await intelligence_tables.generate_table_intelligence_for_page(
        job=None,
        page_number=1,
        targets=[dict(table_review_id="review-0", num_rows=3, num_cols=3, cells=[])],
        page_structure_fragments=[],
        llm_client=None,
    )
    assert result[0]["suggested_action"] == "set_table_headers"
    assert result[0]["header_rows"] == [0]
    assert result[0]["confidence"] == "low"


def test_anonymous_batch_answer_never_guesses_between_multiple_tables():
    from app.services.intelligence_tables import table_batch_decisions

    assert (
        table_batch_decisions(
            dict(action="set_table_headers", header_rows=[0]),
            [dict(table_review_id="a"), dict(table_review_id="b")],
        )
        == {}
    )


async def test_pretag_batch_without_confidence_reaches_output_and_job_audit(tmp_path, monkeypatch):
    from app.config import Settings
    from app.pipeline import orchestrator
    from app.services import intelligence_tables

    source = tmp_path / "input.pdf"
    structure = _fixture(source, ROWS)

    async def request(**kwargs):
        return dict(action="set_table_headers", header_rows=[0]), {}

    class Client:
        async def close(self):
            pass

    monkeypatch.setattr(intelligence_tables, "request_llm_json_with_response", request)
    monkeypatch.setattr(orchestrator, "_make_pretag_llm_client", lambda *a: Client())
    queued, audit = await orchestrator._apply_pretag_table_intelligence(
        job=SimpleNamespace(
            input_path=str(source), output_path=None, original_filename="input.pdf"
        ),
        settings=Settings(llm_api_key="check-only", llm_base_url="http://127.0.0.1:8000/v1"),
        structure_json=structure,
    )
    assert audit["queued_count"] == 1 and not audit["applied"]
    result = await tagger.tag_pdf(source, tmp_path / "after.pdf", queued, [], language="en")
    assert result.table_intelligence_audit[0]["status"] == "applied_headers"
    details = dict(
        pretag_grounded_text={},
        pretag_table_intelligence=audit,
        pretag_widget_rationalization={},
        pretag_form_intelligence={},
        pretag_llm_usage={},
        docling_pretag_plan={},
    )
    job_audit = orchestrator._tagging_step_result(result, details)
    assert job_audit["table_intelligence_auto_applied_count"] == 1
    assert job_audit["table_intelligence_audit"]["decisions"][0]["status"] == "applied_headers"


async def test_postfidelity_table_decisions_apply_even_on_advisory_tasks(tmp_path, monkeypatch):
    from app.services import auto_structure_apply

    source = tmp_path / "input.pdf"
    structure = _fixture(source, ROWS)

    class Client:
        async def close(self):
            pass

    async def generate(**kw):
        return dict(
            suggested_action="reclassify_region",
            table_intelligence=[
                dict(
                    table_review_id="review-0",
                    page=1,
                    suggested_action="reclassify_region",
                    resolved_kind="layout_grid",
                )
            ],
        )

    monkeypatch.setattr(
        auto_structure_apply, "make_llm_client_with_overrides", lambda *a, **k: Client()
    )
    monkeypatch.setattr(auto_structure_apply, "generate_remediation_intelligence", generate)
    remaining, queued, specs = await auto_structure_apply.auto_apply_structure_tasks(
        job=SimpleNamespace(),
        settings=SimpleNamespace(llm_pretag_timeout=45, llm_pretag_max_retries=0),
        structure_json=structure,
        review_tasks=[dict(task_type="table_semantics", blocking=False, metadata={})],
    )
    assert not remaining and len(specs) == 1
    assert specs[0]["after"]["structure_json"] == queued
    await tagger.tag_pdf(source, tmp_path / "after.pdf", queued, [], language="en")
    assert "<table>" not in screen_reader_text(tmp_path / "after.pdf", markdown=True)


async def test_continuation_uses_original_headers_before_model_decision(tmp_path):
    pdf = pikepdf.new()
    elements = []
    for page in range(2):
        part = tmp_path / f"p{page}.pdf"
        # The second page's rows do not repeat the header row.
        rows = ROWS if page == 0 else [["Delta", "four", "fourth"], *ROWS[1:]]
        element = _fixture(
            part, rows, headers={(0, c): "Column" for c in range(3)} if page == 0 else None
        )["elements"][0]
        with pikepdf.open(part) as piece:
            source = piece.pages[0]
            if page == 0:
                source.Contents = piece.make_stream(
                    b"q 1 0 0 1 0 -580 cm " + source.Contents.read_bytes() + b" Q"
                )
                for box in [element["bbox"], *[c["bbox"] for c in element["cells"]]]:
                    box["t"] -= 580
                    box["b"] -= 580
            pdf.pages.append(source)
        element["page"] = page
        element["review_id"] = f"review-{page}"
        elements.append(element)
    source, output = tmp_path / "input.pdf", tmp_path / "output.pdf"
    pdf.save(source)
    queued, _ = queue_table_decisions(
        dict(elements=elements),
        [dict(table_review_id="review-1", page=2, action="set_table_headers", header_rows=[0])],
    )
    result = await tagger.tag_pdf(source, output, queued, [], language="en")
    with pikepdf.open(output) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert len(table.K) == 6
        assert all(c.S == "/TH" for c in table.K[0].K)
        assert all(c.S == "/TD" for c in table.K[3].K)
    assert result.table_intelligence_audit[0]["status"] == "joined_previous_table"


async def test_table_decision_from_page_review_is_not_lost_on_order_confirmation(
    tmp_path, monkeypatch
):
    from app.services import auto_structure_apply

    source = tmp_path / "input.pdf"
    structure = _fixture(source, ROWS)

    class Client:
        async def close(self):
            pass

    async def generate(**kwargs):
        return dict(
            suggested_action="confirm_current_order",
            page_text_intelligence={
                "table_intelligence": [
                    dict(
                        table_review_id="review-0",
                        page=1,
                        suggested_action="set_table_headers",
                        header_rows=[0],
                    )
                ]
            },
        )

    monkeypatch.setattr(
        auto_structure_apply, "make_llm_client_with_overrides", lambda *a, **k: Client()
    )
    monkeypatch.setattr(auto_structure_apply, "generate_remediation_intelligence", generate)
    remaining, proposed, specs = await auto_structure_apply.auto_apply_structure_tasks(
        job=SimpleNamespace(),
        settings=SimpleNamespace(llm_pretag_timeout=45, llm_pretag_max_retries=0),
        structure_json=structure,
        review_tasks=[dict(task_type="reading_order", blocking=True, metadata={})],
    )
    assert not remaining and len(specs) == 1
    result = await tagger.tag_pdf(source, tmp_path / "after.pdf", proposed, [], language="en")
    assert result.table_intelligence_audit[0]["status"] == "applied_headers"
