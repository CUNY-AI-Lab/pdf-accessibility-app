import pikepdf

from app.pipeline.tagger import (
    StructTreeBuilder,
    _ensure_annotation_baseline,
    _prune_incidental_annotations,
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


def test_incidental_annotations_are_pruned_before_tagging():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    trapnet = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/TrapNet"),
        "/Rect": pikepdf.Array([0, 0, 10, 10]),
    }))
    printer_mark = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/PrinterMark"),
        "/Rect": pikepdf.Array([10, 10, 20, 20]),
    }))
    generic = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Square"),
        "/Rect": pikepdf.Array([20, 20, 40, 40]),
    }))
    page["/Annots"] = pikepdf.Array([trapnet, printer_mark, generic])

    removed = _prune_incidental_annotations(page)

    assert removed == 2
    annots = page["/Annots"]
    assert len(annots) == 1
    assert annots[0].get("/Subtype") == pikepdf.Name("/Square")


def test_pruning_removes_annots_key_when_only_incidental_annotations_exist():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page["/Annots"] = pikepdf.Array([
        pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Annot"),
            "/Subtype": pikepdf.Name("/TrapNet"),
            "/Rect": pikepdf.Array([0, 0, 10, 10]),
        })),
        pdf.make_indirect(pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Annot"),
            "/Subtype": pikepdf.Name("/PrinterMark"),
            "/Rect": pikepdf.Array([10, 10, 20, 20]),
        })),
    ])

    removed = _prune_incidental_annotations(page)

    assert removed == 2
    assert "/Annots" not in page
