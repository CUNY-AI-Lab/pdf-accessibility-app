"""Structure-only recovery keeps visible text and marked-content owners."""

import copy
import textwrap

import pikepdf
import pytest

from app.pipeline import tagger
from app.pipeline.page_glyphs import PdfGlyphReader, union_bbox
from app.services.structure_text import screen_reader_text
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
    "change", ["join", "shift", "starts", "columns", "heading", "gap", "headers", "chain", "blank"]
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
        if page and change == "columns":
            rows = [r + ["extra"] for r in rows]
        headers = (
            {(0, c): "Column" for c in range(len(rows[0]))}
            if page == 0 or change == "headers"
            else {}
        )
        elem = _fixture(part, rows, form=form, headers=headers)["elements"][0]
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
    assert "Next" in screen_reader_text(after)
    with pikepdf.open(before) as tagged:
        assert len(list(_tables(tagged.Root.StructTreeRoot))) == (3 if change == "chain" else 2)


@pytest.mark.parametrize(
    "change", ["recover", "plain", "far", "misaligned", "missing", "extractor"]
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
    structure = _fixture(
        source, rows, form=form, headers={(0, 0): "Row"} if change == "extractor" else {}
    )
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
        assert len(table.K) == (3 if change == "recover" else 2)
        if change == "recover":
            with pdf.open_outline() as outline:
                assert not any(str(item.title) in headings for item in outline.root)
        if change == "recover":
            for cell in table.K[0].K:
                assert cell.S == "/TH"
                assert cell.A.Scope == "/Column"
            for col, cell in enumerate(table.K[1].K):
                assert table.K[0].K[col].ID in cell.A.Headers
    if change == "recover":
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
