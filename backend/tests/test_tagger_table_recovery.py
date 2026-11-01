"""Structure-only recovery keeps visible text and marked-content owners."""

import copy
import textwrap

import pikepdf
import pytest

from app.pipeline import tagger
from app.pipeline.page_frame import PageFrame
from app.pipeline.page_glyphs import PdfGlyphReader, union_bbox
from app.pipeline.tagger_table_structure import _continued_table, _mark_table_continuation
from app.services.structure_text import screen_reader_text
from app.services.table_decisions import queue_table_decisions
from tests.pdf_fixtures import rendered
from tests.test_tagger_tables import _assert_table, _fixture, _tables, _tag


def signature(path):
    with pikepdf.open(path) as pdf, PdfGlyphReader(path) as reader:
        return [reader.page(i).marked for i in range(len(pdf.pages))]


async def paired(tmp_path, source, structure, monkeypatch):
    before, after = tmp_path / "before.pdf", tmp_path / "after.pdf"
    original = copy.deepcopy(structure)
    with monkeypatch.context() as patch:
        patch.setattr(tagger, "_plan_external_table_headers", lambda *a: None)
        patch.setattr(tagger, "_finish_table_structure", lambda *a: 0)
        await tagger.tag_pdf(source, before, structure, [], language="en")
    await tagger.tag_pdf(source, after, structure, [], language="en")
    assert structure == original
    assert signature(before) == signature(after)
    assert rendered(before) == rendered(after)
    return before, after


@pytest.mark.parametrize(
    "change", ["join", "shift", "starts", "columns", "heading", "gap", "headers", "chain", "blank", "unreadable"]
)
@pytest.mark.parametrize("form", [False, True])
async def test_cross_page_rows_keep_mcrs_and_header_associations(
    tmp_path, monkeypatch, change, form
):
    source, pdf, elements = tmp_path / "input.pdf", pikepdf.new(), []
    for page in range(3 if change in {"chain", "blank"} else 2):
        if change == "blank" and page == 1:
            pdf.add_blank_page(page_size=(612, 792))
            continue
        part = tmp_path / f"p{page}.pdf"
        rows = (
            [["Item", "Detail"], ["First", "one"], ["Last", "two"]]
            if page == 0
            else [["Next", "three"], ["End", "four"]]
        )
        if page and change == "unreadable":
            rows[0][0] = "alpha beta gamma delta\nepsilon zeta eta theta"
        if page and change == "columns":
            rows = [r + ["extra"] for r in rows]
        headers = (
            {(0, c): "Column" for c in range(len(rows[0]))}
            if page == 0 or change == "headers"
            else {}
        )
        elem = _fixture(part, rows, form=form, headers=headers)["elements"][0]
        if page and change == "unreadable":
            elem["cells"][0]["text"] = "theta eta zeta epsilon delta gamma beta alpha"
        dx = 24 if page and change in {"shift", "starts"} else 0
        dy = (
            -580 if page == 0 or change == "chain" and page == 1 else -160 if change == "gap" else 0
        )
        # A middle fragment fills the page and can continue onto page three.
        if change == "chain" and page == 1:
            dy = 0
            elem["bbox"]["b"] = 60
        with pikepdf.open(part) as piece:
            pg = piece.pages[0]
            pg.Contents = piece.make_stream(
                f"q 1 0 0 1 {dx} {dy} cm ".encode() + pg.Contents.read_bytes() + b" Q"
            )
            if page and change == "heading":
                if form:
                    pg.Resources.Font = pg.Resources.XObject.OCR.Resources.Font
                pg.Contents = piece.make_stream(
                    pg.Contents.read_bytes() + b" BT /F1 10 Tf 72 755 Td (New section) Tj ET"
                )
                elements.append(
                    dict(
                        type="heading",
                        page=page,
                        text="New section",
                        level=1,
                        bbox=dict(l=71, b=752, r=150, t=766),
                    )
                )
            pdf.pages.append(pg)
        for b in [elem["bbox"], *[c["bbox"] for c in elem["cells"]]]:
            b["l"] += dx
            b["r"] += dx
            b["t"] += dy
            b["b"] += dy
        if change == "starts":
            elem["bbox"]["l"] -= dx
            elem["bbox"]["r"] -= dx
        elem["page"] = page
        elements.append(elem)
    pdf.save(source)
    before, after = await paired(
        tmp_path, source, dict(title="Tables", elements=elements), monkeypatch
    )
    expected = 1 if change in {"join", "chain"} else 2
    with pikepdf.open(after) as tagged:
        tables = list(_tables(tagged.Root.StructTreeRoot))
        assert len(tables) == expected
        if expected == 1:
            table = tables[0]
            assert len(table.K) == (7 if change == "chain" else 5)
            assert "Table with " + str(len(table.K)) + " rows" in str(table.A.Summary)
            for row in list(table.K)[3:]:
                assert row.P == table
                for col, cell in enumerate(row.K):
                    assert cell.S == "/TD"
                    assert table.K[0].K[col].ID in cell.A.Headers
    assert ("alpha" if change == "unreadable" else "Next") in screen_reader_text(after)
    with pikepdf.open(before) as tagged:
        assert len(list(_tables(tagged.Root.StructTreeRoot))) == (3 if change == "chain" else 2)


@pytest.mark.parametrize(
    "change", ["recover", "plain", "far", "misaligned", "missing", "extractor", "unreadable", "isolated"]
)
@pytest.mark.parametrize("form", [False, True])
async def test_bold_headings_outside_grid_become_column_headers(
    tmp_path, monkeypatch, change, form
):
    source = tmp_path / "input.pdf"
    rows = [
        ["Learning first topic", "Discussion first topic"],
        ["Learning other topic", "Discussion other topic"],
    ]
    if change in {"unreadable", "isolated"}:
        rows[0][0] = "a b c d e f g h"
    if change == "unreadable":
        rows[0][1] = "i j k l m n o p"
    structure = _fixture(
        source, rows, form=form, headers={(0, 0): "Row"} if change == "extractor" else {}
    )
    if change in {"unreadable", "isolated"}:
        structure["elements"][0]["cells"][0]["text"] = "h g f e d c b a"
    if change == "unreadable":
        structure["elements"][0]["cells"][1]["text"] = "p o n m l k j i"
    headings = ["Learning outcomes", "Assessment"]
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        pg = pdf.pages[0]
        owner = pg.Resources.XObject.OCR if form else pg
        stream = owner.read_bytes() if form else pg.Contents.read_bytes()
        for col, text in enumerate(headings[:1] if change == "missing" else headings):
            x, y = (
                72 + 160 * col + (40 if change == "misaligned" else 0),
                760 if change == "far" else 730,
            )
            face = "F1" if change == "plain" else "F2"
            stream += f" BT /{face} 10 Tf {x} {y} Td ({text}) Tj ET".encode()
        if form:
            owner.write(stream)
        else:
            pg.Contents = pdf.make_stream(stream)
        pdf.save(source)
    with PdfGlyphReader(source) as reader:
        ops = reader.page(0).ops[("OCR",) if form else ()]
        for text, op in zip(headings, ops[4:]):
            structure["elements"].append(
                dict(
                    type="heading",
                    page=0,
                    text=text,
                    level=2,
                    bbox=union_bbox(g.bbox for g in op.glyphs),
                )
            )
    _, after = await paired(tmp_path, source, structure, monkeypatch)
    with pikepdf.open(after) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert len(table.K) == (3 if change in {"recover", "isolated"} else 2)
        if change in {"recover", "isolated"}:
            with pdf.open_outline() as outline:
                assert not any(str(item.title) in headings for item in outline.root)
        if change in {"recover", "isolated"}:
            for cell in table.K[0].K:
                assert cell.S == "/TH"
                assert cell.A.Scope == "/Column"
            for col, cell in enumerate(table.K[1].K):
                assert table.K[0].K[col].ID in cell.A.Headers
    if change in {"recover", "isolated"}:
        heard = screen_reader_text(after, markdown=True)
        assert "<th>Learning outcomes</th>" in heard
        assert "<th>Assessment</th>" in heard
        assert heard.count("Learning outcomes") == 1


@pytest.mark.parametrize("kind", ["points", "percent", "definition", "bold", "sections"])
async def test_two_column_labels_are_row_headers(tmp_path, kind):
    kwargs = {}
    if kind == "points":
        rows = [
            ["Understanding and knowledge of the subject", "15 points"],
            ["Quality of selected visual aids", "10 points"],
        ]
    elif kind == "percent":
        rows = [
            ["Class attendance and active participation", "25%"],
            ["Communications assignment", "75%"],
        ]
    elif kind == "definition":
        rows = [
            ["Index", "A numerical identifier for an individual failure condition"],
            ["Section", "A reference to the relevant section of the protocol"],
        ]
    elif kind == "bold":
        rows = [
            ["Interests and questions about this subject", "Reply"],
            ["Differences and questions about this subject", "Reply"],
        ]
        kwargs["label_style"] = True
    else:
        rows = [
            ["Goal 1", None],
            ["Description", "Participation in research under faculty supervision"],
            ["Evidence", "Collection of experiments and summary of work"],
            ["Goal 2", None],
            ["Evaluation", "Review summary of work with the faculty member"],
        ]
        kwargs["spans"] = (0, 3)
    rows = [[textwrap.fill(t, 27) if t else t for t in r] for r in rows]
    _assert_table(
        await _tag(tmp_path, rows, row_height=90, **kwargs),
        rows,
        {(r, 0): "Row" for r in range(len(rows))},
    )


@pytest.mark.parametrize("kind", ["three", "records", "blank", "contents", "extractor"])
async def test_row_headers_leave_ambiguous_tables_unchanged(tmp_path, kind):
    rows, kwargs, expected = [["Apple", "red"], ["Pear", "green"]], {}, {}
    if kind == "three":
        rows = [
            ["Term", "A detailed explanation of this term", "extra"],
            ["Other", "A detailed explanation of another term", "extra"],
        ]
    elif kind == "blank":
        rows = [["Label", ""], ["Other", ""]]
    elif kind == "contents":
        rows = [
            ["An introduction to apples", "4"],
            ["Growing the best pears", "8"],
            ["How to harvest plums", "12"],
        ]
    elif kind == "extractor":
        rows = [
            ["Term", "A detailed explanation of this term"],
            ["Other", "A detailed explanation of another term"],
        ]
        kwargs["headers"], expected = {(0, 1): "Column"}, {(0, 1): "Column"}
    rows = [[textwrap.fill(t, 27) if t else t for t in r] for r in rows]
    _assert_table(await _tag(tmp_path, rows, row_height=60, **kwargs), rows, expected)


@pytest.mark.parametrize("emphasis", ["lead", "single", "tail"])
async def test_row_label_uses_bold_lead_run_not_incidental_emphasis(tmp_path, emphasis):
    source, output = tmp_path / "input.pdf", tmp_path / "tagged.pdf"
    rows = [["Interests question", "Reply"], ["Differences question", "Reply"]]
    structure = _fixture(source, rows)
    with pikepdf.open(source, allow_overwriting_input=True) as pdf:
        content = pdf.pages[0].Contents.read_bytes()
        for label, _ in rows:
            if emphasis == "lead":
                lead, tail = label.split(" ", 1)
                replacement = f"/F2 10 Tf ({lead}) Tj /F1 10 Tf ( {tail}) Tj"
            elif emphasis == "single":
                replacement = f"/F2 10 Tf ({label[0]}) Tj /F1 10 Tf ({label[1:]}) Tj"
            else:
                lead, tail = label.split(" ", 1)
                replacement = f"({lead} ) Tj /F2 10 Tf ({tail}) Tj"
            content = content.replace(f"({label}) Tj".encode(), replacement.encode())
        pdf.pages[0].Contents = pdf.make_stream(content)
        pdf.save(source)
    with PdfGlyphReader(source) as reader:
        # Each row has two label operators and one value operator.
        ops = reader.page(0).ops[()]
        for row in range(2):
            structure["elements"][0]["cells"][row * 2]["bbox"] = union_bbox(
                g.bbox for op in ops[row * 3 : row * 3 + 2] for g in op.glyphs
            )
    await tagger.tag_pdf(source, output, structure, [], language="en")
    expected = {(r, 0): "Row" for r in range(2)} if emphasis == "lead" else {}
    _assert_table(output, rows, expected)


LETTER = PageFrame(rotation=0, left=0, bottom=0, right=612, top=792)
STEPS = [["No.", "Step"], ["1", "Open the site"], ["2", "Sign in"]]
MORE = [["3", "Choose a file"], ["4", "Upload it"]]


def part(rows, *, page, top, flagged=(), shift=0.0, **extra):
    """A two-column table on ``page`` whose first row's top is at ``top``
    (user space, 20 pt rows), with the rows in ``flagged`` marked as column
    headers by the extractor."""
    cells = [
        dict(
            row=r,
            col=c,
            text=text,
            row_span=1,
            col_span=1,
            is_header=r in flagged,
            column_header=r in flagged,
            row_header=False,
            bbox=dict(
                l=72 + 200 * c + shift,
                b=top - 20 * r - 12,
                r=72 + 200 * c + 150 + shift,
                t=top - 20 * r,
            ),
        )
        for r, texts in enumerate(rows)
        for c, text in enumerate(texts)
    ]
    box = dict(l=70 + shift, b=top - 20 * len(rows), r=430 + shift, t=top + 2)
    return dict(
        dict(
            type="table",
            page=page,
            num_rows=len(rows),
            num_cols=len(rows[0]),
            cells=cells,
            bbox=box,
            caption=None,
            reading_order=0,
            _docling_order=page,
        ),
        **extra,
    )


HEADERS = dict(suggested_action="set_table_headers", header_rows=[0], row_header_columns=[0])


@pytest.mark.parametrize(
    "change",
    [
        "model",
        "extractor",
        "external",
        "footer",
        "continued",
        "repeat",
        "repeat_flagged",
        "none",
        "row_headers",
        "own_headers",
        "caption",
        "heading",
        "note",
        "middle",
        "lower",
        "columns",
        "shift",
        "layout",
        "unreadable",
    ],
)
def test_continuation_needs_last_and_first_blocks_with_the_same_columns(change):
    """A table continues on the next page only when it ends the page, the
    next page starts with a table of the same columns, and the first gets a
    header row the second does not replace."""
    decisions = [] if change in {"extractor", "external", "none", "row_headers"} else [HEADERS]
    if change == "row_headers":
        decisions = [dict(HEADERS, header_rows=[])]
    first = part(
        STEPS,
        page=0,
        top=100,
        flagged=(0,) if change == "extractor" else (),
        table_llm_decisions=decisions,
    )
    if change == "external":
        first["_external_table_headers"] = [dict(text="No."), dict(text="Step")]
    if change == "layout":
        first["_layout_table"] = {"blocks": []}
    previous = [
        dict(type="heading", page=0, text="Steps", bbox=dict(l=70, b=500, r=300, t=520)),
        first,
    ]
    rows = [STEPS[0], *MORE] if change.startswith("repeat") else MORE
    if change == "own_headers":
        rows = [["Part", "Task"], *MORE]
    second = part(
        rows,
        page=1,
        top={"middle": 500, "lower": 650}.get(change, 720),
        flagged=(0,) if change in {"repeat_flagged", "own_headers"} else (),
        shift=20 if change == "shift" else 0.0,
        caption={"caption": "Table 2. Fees", "continued": "Table 1 (continued)"}.get(change),
    )
    if change == "columns":
        for cell in second["cells"]:
            cell["col"] = 2 * cell["col"]
        second["num_cols"] = 3
    if change == "unreadable":
        second["_table_readability"] = {"page": 2}
    current = [second]
    if change == "footer":
        previous.append(
            dict(
                type="artifact",
                artifact_type="page_footer",
                page=0,
                text="Page 1",
                bbox=dict(l=280, b=20, r=330, t=32),
            )
        )
    if change == "heading":
        current.insert(
            0, dict(type="heading", page=1, text="Fees", bbox=dict(l=70, b=740, r=300, t=760))
        )
    if change == "note":
        previous.append(
            dict(
                type="paragraph",
                page=0,
                text="Note: steps vary.",
                bbox=dict(l=70, b=30, r=300, t=40),
            )
        )
    found = _continued_table(previous, current, (LETTER, LETTER))
    joined = {"model", "extractor", "external", "footer", "continued", "repeat", "repeat_flagged"}
    if change not in joined:
        assert found is None
        return
    assert found == (first, second)
    assert _mark_table_continuation(*found, current)
    assert second["_continues_table"] is first
    if change.startswith("repeat"):
        # The repeated header row becomes a pagination artifact over its cells.
        (artifact,) = (e for e in current if e.get("type") == "artifact")
        assert artifact["artifact_type"] == "pagination"
        assert artifact["bbox"]["t"] == 720 and artifact["bbox"]["b"] == 708
        assert second["num_rows"] == 2
        assert [(c["row"], c["text"]) for c in second["cells"] if c["col"] == 0] == [
            (0, "3"),
            (1, "4"),
        ]
    else:
        assert current == [second] and second["num_rows"] == 2


def test_inferred_header_row_keeps_the_next_page_table_its_own():
    first = part(STEPS, page=0, top=100, table_llm_decisions=[HEADERS])
    second = part(MORE, page=1, top=720)
    found = _continued_table([first], [second], (LETTER, LETTER))
    assert found == (first, second)
    # The tagger's inference then found a header row of its own.
    second["cells"][0].update(is_header=True, column_header=True)
    second["cells"][1].update(is_header=True, column_header=True)
    assert not _mark_table_continuation(*found, [second])
    assert "_continues_table" not in second


async def two_pages(tmp_path, first, second, decisions, *, form=False):
    """A table at the foot of page one and the next page's table at its head."""
    pdf, elements = pikepdf.new(), []
    for page, rows in enumerate((first, second)):
        piece_path = tmp_path / f"p{page}.pdf"
        element = _fixture(piece_path, rows, form=form)["elements"][0]
        with pikepdf.open(piece_path) as piece:
            pg = piece.pages[0]
            if page == 0:
                pg.Contents = piece.make_stream(
                    b"q 1 0 0 1 0 -580 cm " + pg.Contents.read_bytes() + b" Q"
                )
                for box in [element["bbox"], *[c["bbox"] for c in element["cells"]]]:
                    box["t"] -= 580
                    box["b"] -= 580
            pdf.pages.append(pg)
        element.update(page=page, review_id=f"review-{page}")
        elements.append(element)
    source = tmp_path / "input.pdf"
    pdf.save(source)
    structure, _ = queue_table_decisions(dict(title="Steps", elements=elements), decisions)
    return source, structure


@pytest.mark.parametrize("second", ["form_region", "layout_grid", "headers", "repeat", "none"])
@pytest.mark.parametrize("form", [False, True])
async def test_continued_rows_join_the_table_and_take_its_model_headers(tmp_path, second, form):
    """The model gave headers to the table on page one; its rows on page two
    are tagged under the same Table, whatever the model said of them alone."""
    decisions = [dict(HEADERS, table_review_id="review-0", page=1, confidence="low")]
    if second in {"form_region", "layout_grid"}:
        decisions.append(
            dict(
                table_review_id="review-1",
                page=2,
                confidence="low",
                suggested_action="reclassify_region",
                resolved_kind=second,
            )
        )
    elif second == "headers":
        decisions.append(
            dict(
                table_review_id="review-1",
                page=2,
                suggested_action="set_table_headers",
                header_rows=[0],
            )
        )
    rows = [STEPS[0], *MORE] if second == "repeat" else MORE
    source, structure = await two_pages(tmp_path, STEPS, rows, decisions, form=form)
    output = tmp_path / "output.pdf"
    original = copy.deepcopy(structure)
    result = await tagger.tag_pdf(source, output, structure, [], language="en")
    assert structure == original
    with pikepdf.open(output) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        pages = [page.obj.objgen for page in pdf.pages]
        assert len(table.K) == 5
        header = table.K[0]
        assert [str(c.S) for c in header.K] == ["/TH", "/TH"]
        assert all(c.A.Scope == "/Column" for c in header.K)
        for index, row in enumerate(table.K):
            assert row.P == table
            assert {mcr.Pg.objgen for cell in row.K for mcr in cell.K} == {
                pages[0] if index < 3 else pages[1]
            }
            if index:
                label, value = row.K
                assert label.S == "/TH" and label.A.Scope == "/Row"
                assert value.S == "/TD"
                assert set(value.A.Headers) == {header.K[1].ID, label.ID}
    statuses = [d["status"] for d in result.table_intelligence_audit]
    assert statuses[0] == "applied_headers"
    assert all(status == "joined_previous_table" for status in statuses[1:])
    assert result.tables_joined == [
        dict(
            page=2,
            table_review_id="review-1",
            continues_page=1,
            continues_table_review_id="review-0",
        )
    ]
    heard = screen_reader_text(output, markdown=True)
    assert heard.count("<th>No.</th><th>Step</th>") == 1
    assert "<tr><th>3</th><td>Choose a file</td></tr>" in heard
    if second == "repeat":
        # The header row the second page repeats is still drawn, as an artifact.
        assert rendered(source) == rendered(output)
        with pikepdf.open(output) as pdf:
            page = pdf.pages[1]
            owner = page.Resources.XObject.OCR if form else page
            marks, shown = [], {}
            for operands, operator in pikepdf.parse_content_stream(owner):
                if str(operator) in {"BDC", "BMC"}:
                    marks.append(operands)
                elif str(operator) == "EMC":
                    marks.pop()
                elif str(operator) == "Tj":
                    shown[str(operands[0])] = marks[-1]
        for text in STEPS[0]:
            tag, properties = shown[text]
            assert tag == "/Artifact" and properties.Type == "/Pagination"
        assert "/MCID" in shown["3"][1]
