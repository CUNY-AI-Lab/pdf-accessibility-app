"""Headers and layout decisions cannot conceal unreadable table cells."""

import copy
from dataclasses import replace

import pikepdf
import pytest

from app.pipeline import tagger
from app.pipeline.fidelity import assess_fidelity
from app.pipeline.page_frame import PageFrame
from app.pipeline.page_glyphs import PdfGlyphReader
from app.pipeline.tagger_table_readability import TABLE_READABILITY_REASON, _mark_unreadable_tables
from app.services.release_policy import apply_release_policy, release_hold_reasons
from app.services.structure_text import screen_reader_text
from app.services.table_decisions import queue_table_decisions
from tests.pdf_fixtures import rendered
from tests.test_tagger_table_recovery import signature
from tests.test_tagger_tables import _fixture, _tables

# Content turned a quarter turn counterclockwise (text runs up the page) or
# clockwise (down), kept inside the page, and how each moves a box.
TURNS = {
    1: (b"0 1 -1 0 792 0", lambda b: dict(l=792 - b["t"], b=b["l"], r=792 - b["b"], t=b["r"])),
    3: (
        b"0 -1 1 0 -560 612",
        lambda b: dict(l=b["b"] - 560, b=612 - b["r"], r=b["t"] - 560, t=612 - b["l"]),
    ),
}


def rotate(source, structure, turn=1, page_rotate=0):
    """Draw the page's content sideways, the structure's boxes with it, as an
    extractor that read the table upright gives them. With ``page_rotate``
    the page is shown turned, so its text reads upright, and the boxes are in
    the frame of the page as shown, as Docling's are."""
    matrix, move = TURNS[turn]
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page.Contents = pdf.make_stream(
            b"q " + matrix + b" cm " + page.Contents.read_bytes() + b" Q"
        )
        if page_rotate:
            page.Rotate = page_rotate
        pdf.save(source)
    shown = PageFrame(page_rotate, 0, 0, 612, 792)
    table = structure["elements"][0]
    for obj in [table, *table["cells"]]:
        obj["bbox"] = shown.to_docling(move(obj["bbox"]))


def read_across(structure, turn):
    """Label a table's cells in a grid whose rows are the table's columns.
    A sideways table read as the page is displayed (TableFormer on the page
    image) gets this grid: rows top to bottom on the page, columns left to
    right."""
    table = structure["elements"][0]
    rows, cols = table["num_rows"], table["num_cols"]
    for cell in table["cells"]:
        r, c = cell["row"], cell["col"]
        cell["row"], cell["col"] = {0: (c, r), 1: (cols - 1 - c, r), 3: (c, rows - 1 - r)}[turn]
        cell["column_header"] = cell["row_header"] = cell["is_header"] = False
    table["num_rows"], table["num_cols"] = cols, rows


SIDEWAYS_ROWS = [["Name", "Kind", "Value"], ["Alpha", "One", "First"], ["Beta", "Two", "Last"]]


@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize("turn", [1, 3])
@pytest.mark.parametrize("decision", ["headers", "layout_grid"])
async def test_sideways_table_read_in_its_text_frame_takes_decisions(
    tmp_path, form, turn, decision
):
    source, after = tmp_path / "input.pdf", tmp_path / "after.pdf"
    structure = _fixture(source, SIDEWAYS_ROWS, form=form)
    rotate(source, structure, turn)
    structure["elements"][0]["review_id"] = "review-0"
    item: dict = dict(table_review_id="review-0", page=1)
    item.update(
        dict(action="set_table_headers", header_rows=[0])
        if decision == "headers"
        else dict(action="reclassify_region", resolved_kind=decision)
    )
    proposed, _ = queue_table_decisions(structure, [item])
    result = await tagger.tag_pdf(source, after, proposed, [], language="en")
    assert rendered(source) == rendered(after)
    assert result.table_readability == []
    status = result.table_intelligence_audit[0]["status"]
    assert status == ("applied_headers" if decision == "headers" else "applied_layout")
    heard = screen_reader_text(after, markdown=True)
    if decision == "headers":
        assert "<tr><th>Name</th><th>Kind</th><th>Value</th></tr>" in heard
        assert "<tr><td>Alpha</td><td>One</td><td>First</td></tr>" in heard
    else:
        assert "<table>" not in heard


@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize("turn", [0, 1, 3])
@pytest.mark.parametrize(
    "decision", ["none", "headers", "layout_grid", "toc_group", "text_block", "form_region"]
)
async def test_table_gridded_across_its_text_keeps_td_cells_and_visible_hold(
    tmp_path, form, turn, decision
):
    source, before, after = [tmp_path / f"{name}.pdf" for name in ("input", "before", "after")]
    structure = _fixture(source, SIDEWAYS_ROWS, style="bold", form=form)
    if turn:
        rotate(source, structure, turn)
    read_across(structure, turn)
    structure["elements"][0]["review_id"] = "review-0"
    proposed = structure
    if decision != "none":
        item: dict = dict(table_review_id="review-0", page=1)
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
    (diagnostic,) = result.table_readability
    assert diagnostic["cause"] == "grid_across_text"
    assert diagnostic["text_turn"] == turn
    assert diagnostic["rotated_glyphs"] == (diagnostic["glyphs"] if turn else 0)
    # Rows of the across grid are the table's columns: a third of each pair
    # of neighboring cells is in order, a third reversed, a third level.
    assert (diagnostic["grid_pairs"], diagnostic["inverted_grid_pairs"]) == (12, 6)
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


@pytest.mark.parametrize("page_rotate", [0, 90])
@pytest.mark.parametrize("scrambled", [False, True])
async def test_sideways_cell_text_order_is_measured_in_the_texts_frame(
    tmp_path, page_rotate, scrambled
):
    """Sideways on the page, or upright as shown by /Rotate: either way a
    cell read out of order is held and one read in order is not."""
    source, after = tmp_path / "input.pdf", tmp_path / "after.pdf"
    text = "alpha beta gamma delta\nepsilon zeta eta theta"
    structure = _fixture(
        source, [[text, "Kind"], ["One", "Two"], ["Four", "Five"]], style="bold", row_height=90
    )
    rotate(source, structure, 1, page_rotate)
    table = structure["elements"][0]
    table["review_id"] = "review-0"
    if scrambled:
        table["cells"][0]["text"] = "theta eta zeta epsilon delta gamma beta alpha"
    proposed, _ = queue_table_decisions(
        structure,
        [dict(table_review_id="review-0", page=1, action="set_table_headers", header_rows=[0])],
    )
    result = await tagger.tag_pdf(source, after, proposed, [], language="en")
    assert bool(result.table_readability) is scrambled
    if scrambled:
        assert result.table_readability[0]["cause"] == "cell_text_order"
        drawn = result.table_readability[0]["glyphs"]
        assert result.table_readability[0]["rotated_glyphs"] == (0 if page_rotate else drawn)
        assert result.table_readability[0]["disordered_cells"] == [
            dict(row=0, col=0, matched_words=8, matched_unique_words=8, inversion_fraction=1.0)
        ]
    assert ("<th>" in screen_reader_text(after, markdown=True)) is not scrambled


async def test_sideways_extractor_headers_survive_without_a_hold(tmp_path):
    source, after = tmp_path / "input.pdf", tmp_path / "after.pdf"
    structure = _fixture(
        source,
        [["Name", "Value"], ["Alpha", "First"], ["Beta", "Last"]],
        headers={(0, 0): "Column", (0, 1): "Column"},
    )
    rotate(source, structure)
    result = await tagger.tag_pdf(source, after, structure, [], language="en")
    assert result.table_readability == []
    with pikepdf.open(after) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert all(c.S == "/TH" and c.A.Scope == "/Column" for c in table.K[0].K)


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


@pytest.mark.parametrize(
    "turns, display_turn, held",
    [
        ((0, 0, 0, 0, 0, 0), 0, False),
        ((0, 0, 0, 1, 1, 1), 0, False),  # half upright as shown
        ((0, 0, 1, 1, 2, 2), 0, True),  # no direction holds a majority
        ((1, 1, 2, 2, 3, 3), 0, True),
        # On a page shown a quarter turn, its own text is upright.
        ((1, 1, 1, 0, 0, 2), 1, False),
        ((1, 1, 1, 0, 0, 2), 0, True),
    ],
)
def test_a_table_without_a_majority_direction_is_held(tmp_path, turns, display_turn, held):
    source = tmp_path / "input.pdf"
    structure = _fixture(source, [["abcd", "efgh"], ["ijkl", "mnop"], ["qrst", "uvwx"]])
    with PdfGlyphReader(source) as reader:
        glyphs = [g for ops in reader.page(0).ops.values() for op in ops for g in op.glyphs]
    assert len(glyphs) == 24
    # Each cell's four glyphs run one way.
    glyphs = [replace(g, turn=turns[i // 4]) for i, g in enumerate(glyphs)]
    diagnostics = _mark_unreadable_tables(structure["elements"], glyphs, display_turn)
    assert bool(diagnostics) is held
    if held:
        assert diagnostics[0]["cause"] == "mixed_text_direction"


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
