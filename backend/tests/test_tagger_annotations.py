import pikepdf

from app.pipeline.tagger import (
    StructTreeBuilder,
    _ensure_annotation_baseline,
    _tag_generic_annotations,
)


def test_generic_annotations_are_tagged_as_annot_struct_elems():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Square"),
        "/Rect": pikepdf.Array([10, 10, 50, 50]),
    }))
    page["/Annots"] = pikepdf.Array([annotation])

    builder = StructTreeBuilder(pdf)
    builder.setup()

    _ensure_annotation_baseline(page)
    tagged = _tag_generic_annotations(page, page.obj, builder)
    builder.finalize()

    assert tagged == 1
    assert str(annotation.get("/Contents")) == "Annotation"
    assert annotation.get("/StructParent") == 0
    assert len(builder.doc_elem["/K"]) == 1
    assert builder.doc_elem["/K"][0].get("/S") == pikepdf.Name("/Annot")
