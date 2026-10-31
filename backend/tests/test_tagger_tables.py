"""Table fallbacks exercised through tag_pdf and the screen-reader reader."""

import copy
import textwrap

import pikepdf
import pytest

from app.pipeline.page_glyphs import PdfGlyphReader, union_bbox
from app.pipeline.tagger import tag_pdf
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import helvetica, rendered


def _fixture(
    path, rows, *, style=None, rule=None, ocr=False, strokes=(), spans=(), form=False, headers=None, label_style=False, section_style=False, offsets=(), font_size=10, row_height=30
):
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    font = helvetica(pdf)
    bold = pikepdf.Dictionary(font)
    bold.BaseFont = pikepdf.Name("/Helvetica-Bold")
    other = pikepdf.Dictionary(font)
    other.BaseFont = pikepdf.Name("/Courier")
    resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(
            F1=font,
            F2=pdf.make_indirect(bold),
            F3=pdf.make_indirect(other),
        )
    )
    page.Resources = resources
    ops, slots = [], []
    cells = []
    for row, texts in enumerate(rows):
        for col, text in enumerate(texts):
            if text is None:
                continue
            x, y = 72 + col * 160 + (offsets[row] if offsets else 0), 700 - row * row_height
            cell = dict(
                row=row,
                col=col,
                text=text,
                row_span=1,
                col_span=len(texts) if row in spans and col == 0 else 1,
                is_header=False,
                column_header=False,
                row_header=False,
                bbox=dict(l=x - 1, b=y - 3, r=x + 20, t=y + 11),
            )
            cells.append(cell)
            scope = (headers or {}).get((row, col))
            if scope:
                cell.update(
                    is_header=True, row_header=scope == "Row", column_header=scope == "Column"
                )
            if text:
                face, size = ("F1", font_size)
                if (row == 0 and style) or (label_style and col == 0) or (section_style and col == 0 and all(not t for t in texts[1:])):
                    face, size = {
                        "bold": ("F2", 10),
                        "font": ("F3", 10),
                        "size": ("F1", 12),
                        "small-size": ("F1", 10.5),
                    }[style or "bold"]
                for line, part in enumerate(text.split("\n")):
                    ops.append(f"BT {3 if ocr else 0} Tr /{face} {size} Tf {x} {y - line * (size + 2)} Td ({part}) Tj ET")
                    slots.append(cell)
    for text, x, row in strokes:
        ops.append(f"BT {3 if ocr else 0} Tr /F1 10 Tf {x} {700 - row * 30} Td ({text}) Tj ET")
        slots.append(None)
    if strokes:
        ops.append(f"BT {3 if ocr else 0} Tr /F1 10 Tf 72 500 Td (Outside I 12) Tj ET")
        slots.append(None)
    if rule:
        ops.insert(0, rule)
    content = " ".join(ops).encode()
    if form:
        stream = pdf.make_stream(content)
        stream.Type, stream.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
        stream.BBox, stream.Resources = [0, 0, 612, 792], resources
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(OCR=stream))
        page.Contents = pdf.make_stream(b"/OCR Do")
    else:
        page.Contents = pdf.make_stream(content)
    pdf.save(path)
    with PdfGlyphReader(path) as reader:
        measured = next(iter(reader.page(0).ops.values()))
        # An OCR form leaves the page's own op list empty.
        if form:
            measured = reader.page(0).ops[("OCR",)]
        boxes = {}
        for cell, op in zip(slots, measured, strict=True):
            if cell is not None:
                boxes.setdefault(id(cell), []).extend(g.bbox for g in op.glyphs)
                box = union_bbox(boxes[id(cell)])
                cell["bbox"] = {s: box[s] + (1 if s in "rt" else -1) for s in "lbrt"}
    elements = [
        dict(
            type="table",
            page=0,
            num_rows=len(rows),
            num_cols=len(rows[0]),
            bbox=dict(l=60, b=690 - row_height * (len(rows) - 1), r=580, t=720),
            cells=cells,
        )
    ]
    if strokes:
        # An overlapping layout region can otherwise claim the OCR noise.
        elements.append(
            dict(
                type="paragraph", page=0, text="Outside I 12", bbox=dict(l=60, b=490, r=580, t=720)
            )
        )
    return dict(title="Table test", elements=elements)


async def _tag(tmp_path, rows, **kwargs):
    source, tagged = tmp_path / "input.pdf", tmp_path / "tagged.pdf"
    structure = _fixture(source, rows, **kwargs)
    original = copy.deepcopy(structure)
    await tag_pdf(source, tagged, structure, [], language="en", original_filename=source.name)
    assert structure == original, "captured structure must remain reusable"
    assert rendered(source) == rendered(tagged)
    return tagged


def _tables(node):
    if isinstance(node, pikepdf.Array):
        for kid in node:
            yield from _tables(kid)
    elif isinstance(node, pikepdf.Dictionary):
        if str(node.get("/S")) == "/Table":
            yield node
        else:
            yield from _tables(node.get("/K"))


def _assert_table(path, rows, headers):
    """Check actual roles, scopes, rectangular coverage, and spoken cell order."""
    expected = []
    with pikepdf.open(path) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert len(table.K) == len(rows)
        for row, (texts, tr) in enumerate(zip(rows, table.K, strict=True)):
            expected.append("<tr>")
            col = 0
            for cell in tr.K:
                text = " ".join((texts[col] or "").split())
                scope = headers.get((row, col))
                tag = "th" if scope else "td"
                assert str(cell.S) == "/" + tag.upper()
                if scope:
                    assert str(cell.A.Scope) == "/" + scope
                attrs = cell.get("/A", {})
                colspan = int(attrs.get("/ColSpan", 1))
                expected.append(
                    f"<{tag}"
                    + (f' colspan="{colspan}"' if colspan > 1 else "")
                    + f">{text}</{tag}>"
                )
                col += colspan
            assert col == len(texts)
            expected.append("</tr>")
    heard = screen_reader_text(path, markdown=True)
    assert "".join(expected) in heard.replace("\n", "")


RULE = "60 690 m 400 690 l S"


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["bold", "font", "size", "fill", "rule", "caps"])
async def test_short_schedule_labels_need_only_one_strong_signal(tmp_path, signal):
    rows = [["Week", "Date", "Topic"], ["1", "9/1", "Course introduction"],
            ["2", "9/8", "Research and discussion"]]
    if signal == "caps":
        rows[0] = [t.upper() for t in rows[0]]
    kwargs = ({"style": signal} if signal in {"bold", "font", "size"} else
              {"rule": "q .85 g 60 690 500 25 re f Q"} if signal == "fill" else
              {"rule": "60 690 m 580 690 l S"} if signal == "rule" else {})
    tagged = await _tag(tmp_path, rows, **kwargs)
    _assert_table(tagged, rows, {(0, c): "Column" for c in range(3)})
    with pikepdf.open(tagged) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        for tr in list(table.K)[1:]:
            for col, cell in enumerate(tr.K):
                assert str(table.K[0].K[col].ID) in [str(h) for h in cell.A.Headers]
    # Shape alone, an unpainted band, and a tiny style change are insufficient.
    rows[0] = ["Week", "Date", "Topic"]
    for weak in [{}, {"style": "small-size"}, {"rule": "60 690 500 25 re n"}]:
        _assert_table(await _tag(tmp_path, rows, **weak), rows, {})


@pytest.mark.asyncio
async def test_schedule_allows_one_missing_trailing_heading_but_no_interior_hole(tmp_path):
    rows = [["Week", "Content", None], ["1", "Introduction to web design", "Homework 1"],
            ["2", "HTML tags and lists", "Homework 2"]]
    _assert_table(await _tag(tmp_path, rows, style="bold"), rows,
                  {(0, 0): "Column", (0, 1): "Column"})
    rows[0] = ["Week", None, "Content"]
    _assert_table(await _tag(tmp_path, rows, style="bold"), rows, {})


@pytest.mark.asyncio
async def test_wrapped_fish_labels_over_prose_use_the_printed_rule(tmp_path):
    rows = [["Fishing Stock", "Lead Analyst or Presenter", "Peer review conclusion from this meeting September 2021"],
            ["Gulf of Maine cod -Enhanced review", "Charles Perretti", "Under retro-adjusted M=0.2 model stock is overfished and overfishing is occurring. Under M-ramp model stock is overfished but overfishing is not occurring."],
            ["Georges Bank cod -Expedited review", "Kathy Sosebee", "Stock is overfished but overfishing unknown"]]
    rows = [[textwrap.fill(t, width=32) for t in row] for row in rows]
    _assert_table(await _tag(tmp_path, rows, rule="60 680 m 580 680 l S", font_size=8, row_height=90), rows,
                  {(0, c): "Column" for c in range(3)})


@pytest.mark.asyncio
async def test_caps_sql_labels_over_varied_values_infer_headers(tmp_path):
    rows = [["TABLE_NAME --------", "NUM_ROWS --------", "LAST_ANALYZED --------"],
            ["DEPARTMENT", "200", "07/29/1999 00:59:51"],
            ["EMPLOYEE", "4000", "07/29/1999 01:16:09"]]
    _assert_table(await _tag(tmp_path, rows), rows, {(0, c): "Column" for c in range(3)})


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["bold", "rule", "caps"])
async def test_single_signal_never_promotes_lists_definitions_or_repeated_records(tmp_path, signal):
    cases = [
        [["TERM", "DESCRIPTION"], ["Apple", "This is a long description of an apple in prose"],
         ["Pear", "This is another long description of a pear in prose"]],
        [["- Task", "- Due"], ["Write a long response", "September 1"],
         ["Read the next chapter", "September 8"]],
        [["TOPIC", "TOPIC", "DATE"], ["Introduction to design", "Read the first chapter", "9/1"],
         ["Discussion of research", "Read the second chapter", "9/8"]],
        [["MARCH", "APRIL", "MAY"], ["June", "July", "August"], ["September", "October", "November"]],
        [["#7", "martes", "2/24"], ["#8", "jueves", "2/26"], ["#9", "martes", "3/3"]],
    ]
    for rows in cases:
        if signal != "caps":
            rows[0] = [t.title() for t in rows[0]]
        kwargs = {"style": "bold"} if signal == "bold" else {"rule": RULE} if signal == "rule" else {}
        _assert_table(await _tag(tmp_path, rows, **kwargs), rows, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["bold", "font", "size"])
async def test_column_headers_need_two_independent_signals(tmp_path, style):
    rows = [["Code", "Color"], ["A", "red"], ["B", "green"]]
    _assert_table(await _tag(tmp_path, rows, style=style, rule=RULE), rows,
                  {(0, c): "Column" for c in range(2)})
    _assert_table(await _tag(tmp_path, rows, style=style), rows, {})
    _assert_table(await _tag(tmp_path, rows, rule=RULE), rows, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", [RULE, "60 689 340 1 re f",
                                  "60 690 m 230 690 l S 230 690 m 400 690 l S",
                                  "60 689 m 400 689 l 400 690 l 60 690 l h f"])
async def test_header_rule_must_be_painted_wide_and_in_the_gap(tmp_path, rule):
    rows = [["Code", "Color"], ["A", "red"], ["B", "green"]]
    _assert_table(await _tag(tmp_path, rows, style="bold", rule=rule), rows,
                  {(0, c): "Column" for c in range(2)})
    for bad in ["60 690 m 400 690 l n", "60 690 m 80 690 l S",
                "60 710 m 400 710 l S", "60 680 m 400 680 l S",
                "60 690 m 400 690 l f"]:
        _assert_table(await _tag(tmp_path, rows, style="bold", rule=bad), rows, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("columns", [2, 3])
async def test_blank_corner_requires_a_second_signal(tmp_path, columns):
    rows = [["", "North", "South"], ["Apple", "red", "green"], ["Pear", "green", "red"]]
    rows = [row[:columns] for row in rows]
    _assert_table(await _tag(tmp_path, rows, style="bold"), rows,
                  {(0, c): "Column" for c in range(columns)})
    _assert_table(await _tag(tmp_path, rows), rows, {})


@pytest.mark.asyncio
async def test_numeric_body_is_independent_header_evidence(tmp_path):
    rows = [["Item", "Count", "Price"], ["Apple", "10", "20"], ["Pear", "30", "40"]]
    # Most BODY CELLS must be numeric, not just the value columns.
    rows = [r + ["Total"] if i == 0 else r + ["50"] for i, r in enumerate(rows)]
    headers = {(0, c): "Column" for c in range(4)} | {(1, 0): "Row", (2, 0): "Row"}
    _assert_table(await _tag(tmp_path, rows, style="bold"), rows, headers)
    # The first row has nonnumeric values, so only 2/3 of value cells are numeric:
    # no row labels are inferred until there is an actual column header.
    no_style = {}
    _assert_table(await _tag(tmp_path, rows), rows, no_style)


@pytest.mark.asyncio
@pytest.mark.parametrize("numeric", [True, False])
async def test_row_headers_require_dense_short_labels_and_numeric_or_style_evidence(tmp_path, numeric):
    rows = [["Apple", "10"], ["Pear", "20"], ["Plum", "30"]] if numeric else [
        ["Apple", "red"], ["Pear", "green"], ["Plum", "purple"]]
    _assert_table(await _tag(tmp_path, rows, label_style=not numeric), rows,
                  {(r, 0): "Row" for r in range(3)})
    bad = [["1", "10"], ["2", "20"], ["3", "30"]] if numeric else rows
    _assert_table(await _tag(tmp_path, bad), bad, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("numeric", [True, False])
@pytest.mark.parametrize("spanning", [False, True])
async def test_projected_headers_require_dense_sections_and_numeric_or_style_evidence(tmp_path, numeric, spanning):
    rows = [["Fruit", ""], ["Apple", "10" if numeric else "red"],
            ["Vegetable", ""], ["Pea", "20" if numeric else "green"]]
    if spanning:
        rows[0][1] = rows[2][1] = None
    headers = {(0, 0): "Row", (2, 0): "Row"}
    _assert_table(await _tag(tmp_path, rows, label_style=not numeric,
                             spans=(0, 2) if spanning else ()), rows, headers)
    long = rows + [[f"Item {r}", "10"] for r in range(8)]
    tagged = await _tag(tmp_path, long, label_style=not numeric)
    # A sparse section is never a projected header, even beside numeric data.
    with pikepdf.open(tagged) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert str(table.K[0].K[0].S) == "/TD"
        assert str(table.K[2].K[0].S) == "/TD"
    prose = [["Fruit", ""], ["Apple", "red"], ["Vegetable", ""], ["Pea", "green"]]
    _assert_table(await _tag(tmp_path, prose), prose, {})


@pytest.mark.asyncio
async def test_bb9fad9e_real_header_wins_over_section_rows(tmp_path):
    rows = [["Type of Applicant", "Fall Deadline", "Spring Deadline"],
            ["Domestic Applicant", "", ""], ["US Citizens", "Visit website", "Visit website"],
            ["International Applicant", "", ""], ["Student visa", "Visit website", "Visit website"]]
    # Segmented rules, as in the actual page.
    rule = "60 690 m 232 690 l S 232 690 m 392 690 l S 392 690 m 580 690 l S"
    _assert_table(await _tag(tmp_path, rows, style="bold", section_style=True, rule=rule), rows,
                  {(0, c): "Column" for c in range(3)})


@pytest.mark.asyncio
async def test_definition_and_resolution_lists_never_infer_headers(tmp_path):
    positive = [["Code", "Color"], ["A", "red"], ["B", "green"]]
    _assert_table(await _tag(tmp_path, positive, style="bold", rule=RULE), positive,
                  {(0, c): "Column" for c in range(2)})
    definitions = [["Term", "This is a long description explaining what the term means"],
                   ["Object", "This is another long description explaining an object in prose"],
                   ["Content", "This is a third long description of the content and its meaning"]]
    _assert_table(await _tag(tmp_path, definitions, style="bold", rule=RULE), definitions, {})
    contents = [["An introduction to apples", "4"], ["Growing the best pears", "8"],
                ["How to harvest plums", "12"]]
    _assert_table(await _tag(tmp_path, contents), contents, {})
    resolutions = [["300", "High"], ["150", "Medium"], ["72", "Low"]]
    _assert_table(await _tag(tmp_path, resolutions, style="bold", rule=RULE), resolutions, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("flag,scope", [("row_header", "Row"), ("column_header", "Column")])
async def test_docling_flags_win_and_every_th_has_scope(tmp_path, flag, scope):
    rows = [["", "North"], ["Apple", "10"], ["Pear", "20"]]
    source, tagged = tmp_path / "input.pdf", tmp_path / "tagged.pdf"
    structure = _fixture(source, rows, style="bold", rule=RULE)
    structure["elements"][0]["cells"][-1][flag] = True
    await tag_pdf(source, tagged, structure, [], language="en")
    _assert_table(tagged, rows, {(2, 1): scope})


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["|", "l", "I", "!"])
@pytest.mark.parametrize("edge", ["start", "end"])
@pytest.mark.parametrize("form", [False, True])
async def test_owned_ocr_rule_tokens_removed_only_at_cell_edges(tmp_path, token, edge, form):
    clean = [["Apple", "10"], ["Grape", "20"], ["Melon", "30"]]
    rows = [[f"{token} {r[0]}" if edge == "start" else f"{r[0]} {token}", r[1]] for r in clean]
    tagged = await _tag(tmp_path, rows, ocr=True, form=form)
    heard = screen_reader_text(tagged, markdown=True)
    for text, _ in clean:
        assert f">{text}</" in heard
    # The same characters inside words or numbers and between words survive.
    words = [["lily I Ibis 1.1 A|B Wow!", "10"], ["lily I Ibis 1.1 A|B Wow!", "20"],
             ["lily I Ibis 1.1 A|B Wow!", "30"]]
    heard = screen_reader_text(await _tag(tmp_path, words, ocr=True, form=form))
    assert heard.count("lily I Ibis 1.1 A|B Wow!") == 3
    for ocr, count in [(False, 3), (True, 2)]:
        heard = screen_reader_text(await _tag(tmp_path, rows[:count], ocr=ocr, form=form), markdown=True)
        for cell in rows[:count]:
            assert f">{cell[0]}</" in heard


@pytest.mark.asyncio
async def test_ocr_rule_alignment_is_bounded_and_counts_distinct_rows(tmp_path):
    rows = [["| Apple", "10"], ["| Grape", "20"], ["| Melon", "30"]]
    heard = screen_reader_text(await _tag(tmp_path, rows, ocr=True, offsets=(0, 2, 4)))
    assert "|" not in heard  # all three positions fit within +/-2 pt
    for offsets in [(0, 2, 4.01), (0, 4, 8)]:
        heard = screen_reader_text(await _tag(tmp_path, rows, ocr=True, offsets=offsets))
        assert heard.count("|") == 3
    # Three strokes on one grid row do not establish a vertical rule.
    one_row = [["| Apple", "| Grape", "| Melon"]]
    heard = screen_reader_text(await _tag(tmp_path, one_row, ocr=True))
    assert heard.count("|") == 3


@pytest.mark.asyncio
async def test_aligned_pronouns_remain_content_beside_ocr_rules(tmp_path):
    rows = [["I thought otherwise", "10"], ["I did not know", "20"], ["I was worried", "30"]]
    tagged = await _tag(tmp_path, rows, ocr=True)
    heard = screen_reader_text(tagged, markdown=True)
    for cell in rows:
        assert f">{cell[0]}</" in heard
    rules = [["I Apple", "10"], ["I Grape", "20"], ["I Melon", "30"]]
    heard = screen_reader_text(await _tag(tmp_path, rules, ocr=True), markdown=True)
    assert ">I " not in heard
