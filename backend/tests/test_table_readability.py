"""Headers and layout decisions cannot conceal unreadable table cells."""

import copy
from dataclasses import replace

import pikepdf
import pytest

from app.pipeline import tagger
from app.pipeline.fidelity import assess_fidelity
from app.pipeline.page_glyphs import PdfGlyphReader
from app.pipeline.tagger_table_readability import TABLE_READABILITY_REASON, _mark_unreadable_tables
from app.services.release_policy import apply_release_policy, release_hold_reasons
from app.services.structure_text import screen_reader_text
from app.services.table_decisions import queue_table_decisions
from tests.pdf_fixtures import rendered
from tests.test_tagger_table_recovery import signature
from tests.test_tagger_tables import _fixture, _tables


def rotate(source, structure):
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page.Contents = pdf.make_stream(
            b"q 0 1 -1 0 792 0 cm " + page.Contents.read_bytes() + b" Q"
        )
        pdf.save(source)
    table = structure["elements"][0]
    for obj in [table, *table["cells"]]:
        b = obj["bbox"]
        obj["bbox"] = dict(l=792 - b["t"], b=b["l"], r=792 - b["b"], t=b["r"])


@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize(
    "decision", ["none", "headers", "layout_grid", "toc_group", "text_block", "form_region"]
)
async def test_rotated_table_keeps_td_cells_and_visible_hold(tmp_path, form, decision):
    source, before, after = [tmp_path / f"{name}.pdf" for name in ("input", "before", "after")]
    structure = _fixture(
        source,
        [["Name", "Kind", "Value"], ["Alpha", "One", "First"], ["Beta", "Two", "Last"]],
        style="bold",
        form=form,
    )
    rotate(source, structure)
    structure["elements"][0]["review_id"] = "review-0"
    proposed = structure
    if decision != "none":
        item = dict(table_review_id="review-0", page=1)
        item.update(
            dict(action="set_table_headers", header_rows=[0])
            if decision == "headers"
            else dict(action="reclassify_region", resolved_kind=decision)
        )
        proposed, _ = queue_table_decisions(structure, [item])
    original = copy.deepcopy(proposed)
    await tagger.tag_pdf(source, before, structure, [], language="en")
    result = await tagger.tag_pdf(source, after, proposed, [], language="en")
    assert original == proposed
    assert signature(before) == signature(after)
    assert rendered(source) == rendered(after)
    assert result.table_readability[0]["cause"] == "rotated_glyphs"
    assert result.table_readability[0]["rotated_glyphs"] == result.table_readability[0]["glyphs"]
    if decision != "none":
        assert result.table_intelligence_audit[0]["status"] == "blocked_unreadable_table"
    with pikepdf.open(after) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert all(cell.S == "/TD" for row in table.K for cell in row.K)
    report, tasks = assess_fidelity(
        input_pdf=source,
        output_pdf=after,
        structure_json=proposed,
        alt_entries=[],
        validation_report={"compliant": True, "violations": []},
        tagging_metrics={"tables_tagged": 1, "table_readability": result.table_readability},
        classification="digital",
    )
    assert "table_readability" in release_hold_reasons(compliant=True, checks=report["checks"])
    task = next(t for t in tasks if t["metadata"].get("check") == "table_readability")
    assert task["blocking"] and task["title"] == TABLE_READABILITY_REASON
    assert task["metadata"]["pages_to_check"] == [1]
    # Adjudication cannot dismiss the release failure by changing the task.
    task["blocking"] = False
    apply_release_policy(report, tasks, compliant=True)
    assert not report["passed"] and task["blocking"]


async def test_rotated_extractor_headers_survive_but_do_not_clear_readability_hold(tmp_path):
    source, after = tmp_path / "input.pdf", tmp_path / "after.pdf"
    structure = _fixture(
        source,
        [["Name", "Value"], ["Alpha", "First"], ["Beta", "Last"]],
        headers={(0, 0): "Column", (0, 1): "Column"},
    )
    rotate(source, structure)
    result = await tagger.tag_pdf(source, after, structure, [], language="en")
    assert result.table_readability
    with pikepdf.open(after) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert all(c.S == "/TH" and c.A.Scope == "/Column" for c in table.K[0].K)
    report, tasks = assess_fidelity(
        input_pdf=source,
        output_pdf=after,
        structure_json=structure,
        alt_entries=[],
        validation_report={"compliant": True, "violations": []},
        tagging_metrics={"tables_tagged": 1, "table_readability": result.table_readability},
        classification="digital",
    )
    assert release_hold_reasons(compliant=True, checks=report["checks"]) == ["table_readability"]
    assert any(t["blocking"] and t["title"] == TABLE_READABILITY_REASON for t in tasks)


@pytest.mark.parametrize("scrambled", [False, True])
async def test_cell_order_disagreement_blocks_deterministic_and_model_headers(tmp_path, scrambled):
    source, after = tmp_path / "input.pdf", tmp_path / "after.pdf"
    text = "alpha beta gamma delta\nepsilon zeta eta theta"
    structure = _fixture(
        source,
        [[text, "Kind"], ["One", "Two"], ["Four", "Five"]],
        style="bold",
        row_height=90,
    )
    table = structure["elements"][0]
    table["review_id"] = "review-0"
    if scrambled:
        table["cells"][0]["text"] = "theta eta zeta epsilon delta gamma beta alpha"
    proposed, _ = queue_table_decisions(
        structure,
        [dict(table_review_id="review-0", page=1, action="set_table_headers", header_rows=[0])],
    )
    result = await tagger.tag_pdf(source, after, proposed, [], language="en")
    assert bool(result.table_readability) == scrambled
    if scrambled:
        assert result.table_readability[0]["cause"] == "cell_text_order"
        assert result.table_readability[0]["disordered_cells"] == [
            dict(row=0, col=0, matched_words=8, matched_unique_words=8, inversion_fraction=1.0)
        ]
        assert result.table_intelligence_audit[0]["status"] == "blocked_unreadable_table"
    assert ("<th>" in screen_reader_text(after, markdown=True)) is not scrambled


def test_rotated_minority_and_insufficient_order_evidence_do_not_block(tmp_path):
    source = tmp_path / "input.pdf"
    structure = _fixture(source, [["Item", "Value"], ["Alpha", "First"], ["Beta", "Last"]])
    with PdfGlyphReader(source) as reader:
        glyphs = [g for ops in reader.page(0).ops.values() for op in ops for g in op.glyphs]
    glyphs[0] = replace(glyphs[0], turn=1)
    structure["elements"][0]["cells"][0]["text"] = "unknown OCR words"
    assert _mark_unreadable_tables(structure["elements"], glyphs) == []


async def test_scrambled_drawing_order_is_detected_even_with_correct_cell_text(tmp_path):
    source, after = tmp_path / "input.pdf", tmp_path / "after.pdf"
    structure = _fixture(
        source,
        [
            ["alpha beta gamma delta\nepsilon zeta eta theta", "Value"],
            ["One", "Two"],
            ["Four", "Five"],
        ],
        style="bold",
        row_height=90,
    )
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        stream = pdf.pages[0].Contents.read_bytes().split(b" ET")
        stream[0], stream[1] = stream[1], stream[0]
        pdf.pages[0].Contents = pdf.make_stream(b" ET ".join(part.strip() for part in stream))
        pdf.save(source)
    result = await tagger.tag_pdf(source, after, structure, [], language="en")
    assert result.table_readability[0]["cause"] == "cell_text_order"
    assert "<th>" not in screen_reader_text(after, markdown=True)


@pytest.mark.parametrize("rotated", [0, 12, 13, 24])
def test_rotation_requires_strict_majority_of_table_glyphs(tmp_path, rotated):
    source = tmp_path / "input.pdf"
    structure = _fixture(source, [["abcd", "efgh"], ["ijkl", "mnop"], ["qrst", "uvwx"]])
    with PdfGlyphReader(source) as reader:
        glyphs = [g for ops in reader.page(0).ops.values() for op in ops for g in op.glyphs]
    assert len(glyphs) == 24
    glyphs = [replace(g, turn=2 if i < rotated else 0) for i, g in enumerate(glyphs)]
    assert bool(_mark_unreadable_tables(structure["elements"], glyphs)) is (rotated > 12)


@pytest.mark.parametrize("matched_other_words, held", [(7, True), (8, True), (9, False)])
def test_disorder_requires_at_least_half_the_tables_matched_words(
    tmp_path, matched_other_words, held
):
    source = tmp_path / "input.pdf"
    text = "alpha beta gamma delta\nepsilon zeta eta theta"
    structure = _fixture(source, [[text, " ".join(["one"] * matched_other_words)]], row_height=90)
    structure["elements"][0]["cells"][0]["text"] = "theta eta zeta epsilon delta gamma beta alpha"
    with PdfGlyphReader(source) as reader:
        glyphs = [g for ops in reader.page(0).ops.values() for op in ops for g in op.glyphs]
    diagnostics = _mark_unreadable_tables(structure["elements"], glyphs)
    assert bool(diagnostics) is held
    if held:
        assert diagnostics[0]["matched_words"] == 8 + matched_other_words
        assert diagnostics[0]["disordered_words"] == 8


@pytest.mark.parametrize(
    "order, held",
    [
        # Moving the last word ahead of the first inverts 7/28 pairs.
        ("theta alpha beta gamma delta epsilon zeta eta", False),
        # A half-cell reversal inverts 16/28 pairs.
        ("epsilon zeta eta theta alpha beta gamma delta", True),
    ],
)
async def test_only_severe_disorder_blocks_headers_and_release(tmp_path, order, held):
    source, after = tmp_path / "input.pdf", tmp_path / "after.pdf"
    text = "alpha beta gamma delta\nepsilon zeta eta theta"
    structure = _fixture(source, [[text, "Kind"], ["One", "Two"]], style="bold", row_height=90)
    table = structure["elements"][0]
    table["review_id"] = "review-0"
    table["cells"][0]["text"] = order
    proposed, _ = queue_table_decisions(
        structure,
        [dict(table_review_id="review-0", page=1, action="set_table_headers", header_rows=[0])],
    )
    result = await tagger.tag_pdf(source, after, proposed, [], language="en")
    assert bool(result.table_readability) is held
    assert ("<th>" in screen_reader_text(after, markdown=True)) is not held
    report, _ = assess_fidelity(
        input_pdf=source, output_pdf=after, structure_json=proposed, alt_entries=[],
        validation_report={"compliant": True, "violations": []},
        tagging_metrics={"tables_tagged": 1, "table_readability": result.table_readability},
        classification="digital",
    )
    assert ("table_readability" in release_hold_reasons(compliant=True, checks=report["checks"])) is held


async def test_isolated_severe_disorder_does_not_block_model_or_deterministic_headers(tmp_path):
    source = tmp_path / "input.pdf"
    text = "alpha beta gamma delta\nepsilon zeta eta theta"
    structure = _fixture(
        source, [["Name", "Kind", "Value"], [text, "Two", "Three"], ["Four extra", "Five", "Six"]],
        style="bold", row_height=90, rule="60 690 m 580 690 l S",
    )
    table = structure["elements"][0]
    table["review_id"] = "review-0"
    table["cells"][3]["text"] = "theta eta zeta epsilon delta gamma beta alpha"
    for model in (False, True):
        proposed = structure
        if model:
            proposed, _ = queue_table_decisions(
                structure,
                [dict(table_review_id="review-0", page=1, action="set_table_headers", header_rows=[0])],
            )
        after = tmp_path / f"after-{model}.pdf"
        result = await tagger.tag_pdf(source, after, proposed, [], language="en")
        assert not result.table_readability
        assert "<th>" in screen_reader_text(after, markdown=True)
