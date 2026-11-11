import pikepdf
import pytest

from app.pipeline.tagger import StructTreeBuilder


def test_reading_order_scope_restores_nested_positions_and_preserves_page_order():
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        builder = StructTreeBuilder(pdf)
        builder.setup()
        with builder.at_reading_order(2):
            builder.add_paragraph(0, page.obj)
            with pytest.raises(ValueError, match="allocation failed"):
                with builder.at_reading_order(1):
                    builder.add_heading(1, 0, page.obj, "First")
                    raise ValueError("allocation failed")
            assert builder.reading_order == 2
            builder.add_paragraph(0, page.obj)
        assert builder.reading_order is None
        builder.order_page_kids(0)
        assert [str(kid.S) for kid in builder.doc_elem.K] == ["/H1", "/P", "/P"]
        assert builder.struct_elems_created == 3
        assert builder.headings == [{"level": 1, "text": "First", "page_index": 0}]
        headings = builder.headings
        headings.clear()
        assert len(builder.headings) == 1
