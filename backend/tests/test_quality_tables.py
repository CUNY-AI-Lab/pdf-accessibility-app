"""Grid repairs preserve content while removing contradictory spans."""

from collections import Counter

import pikepdf

from app.pipeline.tagger import tag_pdf
from app.pipeline.tagger_structure import StructTreeBuilder
from app.pipeline.tagger_tables import _complete_table_grid_cells
from app.services.structure_text import screen_reader_text
from tests.test_tagger_tables import _fixture, _tables


def test_explicit_cells_bound_conflicting_spans():
    cells = [
        dict(row=0, col=0, row_span=4, col_span=3, text="first"),
        dict(row=0, col=1, text="second"),
        dict(row=2, col=0, text="third"),
        dict(row=3, col=2, text="last"),
    ]
    completed = _complete_table_grid_cells(cells, num_rows=4, num_cols=3)
    assert cells[0]["row_span"] == 4
    assert [(c["row"], c["col"]) for c in completed if not c.get("_generated_empty")] == [
        (0, 0),
        (0, 1),
        (2, 0),
        (3, 2),
    ]
    slots = Counter(
        (r, c)
        for cell in completed
        for r in range(cell["row"], cell["row"] + cell["row_span"])
        for c in range(cell["col"], cell["col"] + cell["col_span"])
    )
    assert slots == Counter({(r, c): 1 for r in range(4) for c in range(3)})


def test_valid_full_row_span_keeps_empty_row_in_the_structure():
    pdf = pikepdf.new()
    pdf.add_blank_page()
    builder = StructTreeBuilder(pdf)
    builder.setup()
    cells = [dict(row=0, col=0, row_span=3, col_span=2, text="spanning")]
    builder._ensure_fragmented_table(dict(num_rows=3, num_cols=2, cells=cells))
    (table,) = _tables(builder.doc_elem)
    assert len(table.K) == 3
    assert [len(row.K) for row in table.K] == [1, 0, 0]


async def test_conflicting_span_retains_all_spoken_cells_and_headers(tmp_path):
    source = tmp_path / "source.pdf"
    structure = _fixture(
        source, [["Label", "Value"], ["Alpha", "One"], ["Beta", "Two"]], style="bold"
    )
    structure["elements"][0]["cells"][0]["row_span"] = 3
    tagged = tmp_path / "tagged.pdf"
    await tag_pdf(source, tagged, structure)
    heard = screen_reader_text(tagged)
    assert all(word in heard for word in ["Label", "Value", "Alpha", "One", "Beta", "Two"])
    with pikepdf.open(tagged) as pdf:
        (table,) = _tables(pdf.Root.StructTreeRoot)
        assert len(table.K) == 3
        assert all(len(row.K) == 2 for row in table.K)
        assert all(cell.get("/A", {}).get("/RowSpan", 1) == 1 for row in table.K for cell in row.K)
