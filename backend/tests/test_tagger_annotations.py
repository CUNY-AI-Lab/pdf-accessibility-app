import subprocess
import sys
import textwrap

import pikepdf

from app.pipeline.tagger import (
    ContentRegion,
    StructTreeBuilder,
    _add_bookmarks,
    _clean_bookmark_label,
    _emit_tagged_region,
    _ensure_annotation_baseline,
    _infer_link_contents,
    _normalize_annotation_rect,
    _prune_incidental_annotations,
    _resolve_document_language,
    _tag_generic_annotations,
    _tag_link_annotations,
    _tag_widget_annotations,
)


def test_normalize_annotation_rect_canonicalizes_reversed_coordinates():
    pdf = pikepdf.new()
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([433.143, 47.6855, 278.443, 37.6016]),
    }))

    changed = _normalize_annotation_rect(annotation)

    assert changed is True
    assert [float(value) for value in annotation["/Rect"]] == [278.443, 37.6016, 433.143, 47.6855]


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
    assert str(annotation.get("/Contents")) == "Square annotation"
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


def test_infer_link_contents_prefers_overlapping_visible_text():
    pdf = pikepdf.new()
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([10, 10, 120, 28]),
        "/Dest": pikepdf.Name("/intro"),
    }))

    page_elements = [
        {
            "type": "toc_item",
            "text": "1 Introduction 6",
            "bbox": {"l": 8, "b": 8, "r": 126, "t": 30},
        }
    ]

    assert _infer_link_contents(annotation, page_elements) == "1 Introduction 6"


def test_infer_link_contents_does_not_reconstruct_row_context_for_numeric_page_links():
    pdf = pikepdf.new()
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([551.0, 659.0, 559.0, 669.0]),
        "/Dest": pikepdf.Name("/table-1"),
    }))

    page_elements = [
        {
            "type": "paragraph",
            "text": "3",
            "bbox": {"l": 552.0, "b": 657.0, "r": 558.0, "t": 674.0},
        }
    ]
    page_lines = [
        {"bbox": {"l": 74.9, "b": 658.2, "r": 80.9, "t": 668.5}, "display_text": "1", "text": "1"},
        {
            "bbox": {"l": 99.4, "b": 658.2, "r": 530.1, "t": 668.5},
            "display_text": "Stocks reviewed at September 2022 Management Track Assessment Peer Review meeting",
            "text": "stocks reviewed at september 2022 management track assessment peer review meeting",
        },
        {"bbox": {"l": 552.1, "b": 657.4, "r": 558.0, "t": 674.2}, "display_text": "3", "text": "3"},
    ]

    assert _infer_link_contents(annotation, page_elements, page_lines) == "Link to destination"


def test_infer_link_contents_prefers_exact_word_boxes_for_small_anchor_links():
    pdf = pikepdf.new()
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([98.0, 497.0, 116.0, 513.0]),
        "/Dest": pikepdf.Name("/cite.1"),
    }))

    page_elements = [
        {
            "type": "paragraph",
            "text": (
                "Adobe's Acrobat Pro is a significant step up from Acrobat Reader and allows "
                "alternative ways to view and access the document contents while exporting to other formats."
            ),
            "bbox": {"l": 60, "b": 492, "r": 540, "t": 520},
        }
    ]
    page_words = [
        {"bbox": {"l": 98.0, "b": 497.0, "r": 106.0, "t": 513.0}, "display_text": "5,", "text": "5"},
        {"bbox": {"l": 108.0, "b": 497.0, "r": 116.0, "t": 513.0}, "display_text": "13", "text": "13"},
    ]

    assert _infer_link_contents(annotation, page_elements, page_words=page_words) == "Link to destination"


def test_infer_link_contents_rejects_nearby_paragraph_spill_for_wrapped_numeric_link():
    pdf = pikepdf.new()
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([82.2, 351.6, 89.2, 360.4]),
        "/Dest": pikepdf.Name("/spdog"),
    }))

    page_elements = [
        {
            "type": "paragraph",
            "text": "SPDUNIT (Squalus acanthia) Atlantic spiny dogfish 4,",
            "bbox": {"l": 54.0, "b": 364.6, "r": 337.4, "t": 375.2},
        },
        {
            "type": "paragraph",
            "text": "8",
            "bbox": {"l": 83.2, "b": 350.6, "r": 88.2, "t": 359.2},
        },
    ]

    assert _infer_link_contents(annotation, page_elements, page_lines=None) == "Link to destination"


def test_tag_link_annotations_replaces_generic_baseline_contents_with_visible_text():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([10, 10, 120, 28]),
        "/A": pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Action"),
            "/S": pikepdf.Name("/GoTo"),
            "/D": pikepdf.Array([page.obj, pikepdf.Name("/XYZ"), 0, 0, 0]),
        }),
    }))
    page["/Annots"] = pikepdf.Array([annotation])

    builder = StructTreeBuilder(pdf)
    builder.setup()

    _ensure_annotation_baseline(page)
    assert str(annotation.get("/Contents")) == "Link"

    tagged = _tag_link_annotations(
        page,
        page.obj,
        builder,
        page_elements=[
            {
                "type": "toc_item",
                "text": "1 Introduction 6",
                "bbox": {"l": 8, "b": 8, "r": 126, "t": 30},
            }
        ],
    )
    builder.finalize()

    assert tagged == 1
    assert str(annotation.get("/Contents")) == "1 Introduction 6"
    assert annotation.get("/StructParent") == 0
    assert page.get("/Tabs") == pikepdf.Name("/S")


def test_link_annotation_nests_under_matched_text_struct_element():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([20, 20, 80, 32]),
        "/A": pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Action"),
            "/S": pikepdf.Name("/URI"),
            "/URI": pikepdf.String("https://example.test"),
        }),
    }))
    page["/Annots"] = pikepdf.Array([annotation])
    source_element = {
        "type": "paragraph",
        "text": "Read the example link for details.",
        "bbox": {"l": 10, "b": 15, "r": 180, "t": 40},
    }

    builder = StructTreeBuilder(pdf)
    builder.setup()
    _emit_tagged_region(
        [],
        ContentRegion(
            kind="text",
            start_idx=0,
            end_idx=0,
            instructions=[],
            bbox=source_element["bbox"],
            text="read the example link for details",
        ),
        source_element,
        builder,
        0,
        page.obj,
        {},
        set(),
    )

    tagged = _tag_link_annotations(page, page.obj, builder, page_elements=[source_element])
    builder.finalize()

    assert tagged == 1
    assert annotation.get("/StructParent") == 1
    assert annotation.get("/P") == page.obj
    paragraph = builder.doc_elem["/K"][0]
    assert paragraph.get("/S") == pikepdf.Name("/P")
    paragraph_k = paragraph.get("/K")
    assert isinstance(paragraph_k, pikepdf.Array)
    assert paragraph_k[0].get("/Type") == pikepdf.Name("/MCR")
    link_elem = paragraph_k[1]
    assert link_elem.get("/S") == pikepdf.Name("/Link")
    assert link_elem.get("/P") == paragraph
    assert link_elem.get("/Pg") == page.obj
    assert builder.doc_elem["/K"][0] == paragraph
    assert len(builder.doc_elem["/K"]) == 1


def test_tag_link_annotations_keeps_generic_contents_when_row_context_is_weak():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(600, 800))
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([551.0, 659.0, 559.0, 669.0]),
        "/A": pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Action"),
            "/S": pikepdf.Name("/GoTo"),
            "/D": pikepdf.Array([page.obj, pikepdf.Name("/XYZ"), 0, 0, 0]),
        }),
    }))
    page["/Annots"] = pikepdf.Array([annotation])

    builder = StructTreeBuilder(pdf)
    builder.setup()

    _ensure_annotation_baseline(page)

    tagged = _tag_link_annotations(
        page,
        page.obj,
        builder,
        page_elements=[
            {
                "type": "paragraph",
                "text": "3",
                "bbox": {"l": 552.0, "b": 657.0, "r": 558.0, "t": 674.0},
            }
        ],
        docling_page_lines=[
            {"bbox": {"l": 74.9, "b": 658.2, "r": 80.9, "t": 668.5}, "display_text": "1", "text": "1"},
            {
                "bbox": {"l": 99.4, "b": 658.2, "r": 530.1, "t": 668.5},
                "display_text": "Stocks reviewed at September 2022 Management Track Assessment Peer Review meeting",
                "text": "stocks reviewed at september 2022 management track assessment peer review meeting",
            },
            {"bbox": {"l": 552.1, "b": 657.4, "r": 558.0, "t": 674.2}, "display_text": "3", "text": "3"},
        ],
    )
    builder.finalize()

    assert tagged == 1
    assert str(annotation.get("/Contents")) == "Link"


def test_tag_widget_annotations_uses_docling_widget_metadata_for_missing_tooltip():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 300))
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Widget"),
        "/Rect": pikepdf.Array([40, 40, 180, 58]),
    }))
    page["/Annots"] = pikepdf.Array([annotation])

    builder = StructTreeBuilder(pdf)
    builder.setup()

    tagged = _tag_widget_annotations(
        page,
        page.obj,
        builder,
        docling_page_widgets=[
            {
                "bbox": {"l": 39, "b": 39, "r": 181, "t": 59},
                "field_name": "Last Name (Family Name)",
                "description": "",
                "text": "",
            }
        ],
    )
    builder.finalize()

    assert tagged == 1
    assert str(annotation.get("/TU")) == "Last Name (Family Name)"


def test_infer_link_contents_rejects_sentence_length_paragraph_overlap():
    pdf = pikepdf.new()
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([100, 500, 112, 512]),
        "/Dest": pikepdf.Name("/cite.1"),
    }))

    page_elements = [
        {
            "type": "paragraph",
            "text": (
                "Adobe's Acrobat Pro is a significant step up from Acrobat Reader and allows "
                "alternative ways to view and access the document contents while exporting to other formats."
            ),
            "bbox": {"l": 60, "b": 492, "r": 540, "t": 520},
        }
    ]

    assert _infer_link_contents(annotation, page_elements, page_lines=None) == "Link to destination"


def test_infer_link_contents_rejects_short_broad_paragraph_overlap():
    pdf = pikepdf.new()
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([350, 690, 405, 700]),
        "/A": pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Action"),
            "/S": pikepdf.Name("/URI"),
            "/URI": pikepdf.String("https://example.test/instructions"),
        }),
    }))

    page_elements = [
        {
            "type": "paragraph",
            "text": "Employers must ensure the form instructions are available.",
            "bbox": {"l": 35, "b": 688, "r": 575, "t": 708},
        }
    ]

    assert _infer_link_contents(annotation, page_elements, page_lines=None) == (
        "Link to https://example.test/instructions"
    )


def test_infer_link_contents_keeps_generated_uri_without_hyperlink_metadata():
    pdf = pikepdf.new()
    annotation = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([20, 20, 80, 32]),
        "/Contents": pikepdf.String("Link to https://example.test/details"),
        "/A": pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Action"),
            "/S": pikepdf.Name("/URI"),
            "/URI": pikepdf.String("https://example.test/details"),
        }),
    }))

    page_words = [
        {
            "bbox": {"l": 20, "b": 20, "r": 80, "t": 32},
            "display_text": "Example details",
            "text": "example details",
        }
    ]

    assert _infer_link_contents(annotation, page_words=page_words) == (
        "Link to https://example.test/details"
    )


def test_resolve_document_language_prefers_structure_language_and_normalizes_tesseract_code():
    assert _resolve_document_language({"language": "fr-CA"}, "eng") == "fr-CA"
    assert _resolve_document_language({}, "eng") == "en"


def test_add_bookmarks_uses_explicit_toc_semantics_when_available():
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.add_blank_page(page_size=(200, 200))
    pdf.add_blank_page(page_size=(200, 200))
    pdf.add_blank_page(page_size=(200, 200))

    headings = [
        {"level": 1, "text": "1. 2022 MANAGEMENT TRACK PEER REVIEW PANEL REPORT", "page_index": 1},
        {"level": 2, "text": "1.1 Executive Summary", "page_index": 2},
        {"level": 1, "text": "Appendix A. Meeting participants", "page_index": 3},
    ]
    elements = [
        {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 0, "toc_group_ref": "toc-0"},
        {"type": "toc_item", "text": "1. 2022 MANAGEMENT TRACK PEER REVIEW PANEL REPORT", "page": 0, "toc_group_ref": "toc-0"},
        {"type": "toc_item", "text": "1.1 Executive Summary", "page": 0, "toc_group_ref": "toc-0"},
        {"type": "toc_item", "text": "2", "page": 0, "toc_group_ref": "toc-0"},
        {
            "type": "heading",
            "text": "1. 2022 MANAGEMENT TRACK PEER REVIEW PANEL REPORT",
            "page": 1,
            "level": 1,
        },
        {
            "type": "heading",
            "text": "1.1 Executive Summary",
            "page": 2,
            "level": 2,
        },
        {
            "type": "heading",
            "text": "Appendix A. Meeting participants",
            "page": 3,
            "level": 1,
            "bookmark_include": True,
        },
    ]

    added = _add_bookmarks(pdf, headings, elements)

    assert added == 4
    with pdf.open_outline() as outline:
        titles = [item.title for item in outline.root]
        child_titles = [child.title for child in outline.root[0].children]
        grandchild_titles = [child.title for child in outline.root[0].children[0].children]

    assert titles == ["TABLE OF CONTENTS", "Appendix A. Meeting participants"]
    assert child_titles == ["1. 2022 MANAGEMENT TRACK PEER REVIEW PANEL REPORT"]
    assert grandchild_titles == ["1.1 Executive Summary"]


def test_add_bookmarks_uses_only_native_toc_entries_with_exact_heading_matches():
    pdf = pikepdf.new()
    for _ in range(5):
        pdf.add_blank_page(page_size=(200, 200))

    headings = [
        {"level": 1, "text": "1. 2022 MANAGEMENT TRACK PEER REVIEW PANEL REPORT", "page_index": 2},
        {"level": 2, "text": "1.1 Executive Summary", "page_index": 3},
    ]

    native_toc = {
        "text": "<root>",
        "children": [
            {"text": "Cover", "children": []},
            {"text": "Inside-Cover page", "children": []},
            {
                "text": "1 Panel Report",
                "children": [
                    {"text": "1.1 Executive Summary", "children": []},
                ],
            },
        ],
    }

    added = _add_bookmarks(pdf, headings, elements=None, native_toc=native_toc)

    assert added == 1
    with pdf.open_outline() as outline:
        titles = [item.title for item in outline.root]

    assert titles == ["1.1 Executive Summary"]


def test_add_bookmarks_uses_explicit_toc_group_headings_when_present():
    pdf = pikepdf.new()
    for _ in range(4):
        pdf.add_blank_page(page_size=(200, 200))

    headings = [
        {"level": 1, "text": "Abbreviations and Acronyms", "page_index": 1},
        {"level": 1, "text": "Abbreviations for fish stocks reviewed", "page_index": 2},
    ]
    elements = [
        {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 0, "toc_group_ref": "toc-0"},
        {"type": "toc_item", "text": "Glossaries", "page": 0, "toc_group_ref": "toc-0", "toc_group_heading": True},
        {"type": "toc_item", "text": "Abbreviations and Acronyms . . . . . viii", "page": 0, "toc_group_ref": "toc-0"},
        {"type": "toc_item", "text": "Abbreviations for fish stocks reviewed . . . . . xi", "page": 0, "toc_group_ref": "toc-0"},
    ]

    added = _add_bookmarks(pdf, headings, elements)

    assert added == 4
    with pdf.open_outline() as outline:
        titles = [item.title for item in outline.root]
        child_titles = [child.title for child in outline.root[0].children]

    assert titles == ["TABLE OF CONTENTS"]
    assert child_titles == [
        "Glossaries",
        "Abbreviations and Acronyms",
        "Abbreviations for fish stocks reviewed",
    ]


def test_add_bookmarks_prefers_model_generated_bookmark_plan_when_available():
    pdf = pikepdf.new()
    for _ in range(4):
        pdf.add_blank_page(page_size=(200, 200))

    headings = [
        {"level": 1, "text": "1 Panel Report", "page_index": 1},
        {"level": 2, "text": "1.1 Executive Summary", "page_index": 2},
    ]
    elements = [
        {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 0, "toc_group_ref": "toc-0"},
        {"type": "toc_item", "text": "1 Panel Report", "page": 0, "toc_group_ref": "toc-0"},
        {"type": "heading", "text": "1 Panel Report", "page": 1, "level": 1},
        {"type": "heading", "text": "1.1 Executive Summary", "page": 2, "level": 2},
    ]
    bookmark_plan = [
        {"text": "Overview", "page_index": 0, "level": 1},
        {"text": "1 Panel Report", "page_index": 1, "level": 2},
        {"text": "1.1 Executive Summary", "page_index": 2, "level": 3},
    ]

    added = _add_bookmarks(pdf, headings, elements, bookmark_plan=bookmark_plan)

    assert added == 3
    with pdf.open_outline() as outline:
        titles = [item.title for item in outline.root]
        child_titles = [child.title for child in outline.root[0].children]
        grandchild_titles = [child.title for child in outline.root[0].children[0].children]

    assert titles == ["Overview"]
    assert child_titles == ["1 Panel Report"]
    assert grandchild_titles == ["1.1 Executive Summary"]


def test_clean_bookmark_label_preserves_non_page_suffix_letters():
    assert _clean_bookmark_label("Abbreviations for fish stocks reviewed") == (
        "Abbreviations for fish stocks reviewed"
    )
    assert _clean_bookmark_label("4. LONGFIN INSHORE SQUID") == "4. LONGFIN INSHORE SQUID"
    assert _clean_bookmark_label("9.6. Adobe Acrobat DC") == "9.6. Adobe Acrobat DC"
    assert _clean_bookmark_label("List of Tables ........ ii") == "List of Tables"


def test_add_bookmarks_prefers_explicit_toc_entries_over_unrelated_heading_noise():
    pdf = pikepdf.new()
    for _ in range(6):
        pdf.add_blank_page(page_size=(200, 200))

    headings = [
        {"level": 3, "text": "Management Track Assessments Spring 2023", "page_index": 0},
        {"level": 6, "text": "U.S. Department of Commerce", "page_index": 1},
        {"level": 5, "text": "Abbreviations for fish stocks reviewed", "page_index": 3},
        {"level": 5, "text": "2. ATLANTIC BLUEFISH", "page_index": 4},
        {"level": 6, "text": "Anthony Wood", "page_index": 4},
        {"level": 6, "text": "References:", "page_index": 5},
        {
            "level": 1,
            "text": "Procedures for Issuing Manuscripts in the Northeast Fisheries Science Center Reference Document (CRD) Series",
            "page_index": 5,
        },
    ]
    elements = [
        {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
        {"type": "toc_item", "text": "1 Panel Report........1", "page": 2, "toc_group_ref": "toc-0"},
        {"type": "heading", "text": "U.S. Department of Commerce", "page": 1, "level": 6},
        {"type": "heading", "text": "Abbreviations for fish stocks reviewed", "page": 3, "level": 5, "bookmark_include": True},
        {"type": "heading", "text": "2. ATLANTIC BLUEFISH", "page": 4, "level": 5, "bookmark_include": True},
        {"type": "heading", "text": "Anthony Wood", "page": 4, "level": 6, "bookmark_include": True},
        {"type": "heading", "text": "References:", "page": 5, "level": 6, "bookmark_include": True},
        {
            "type": "heading",
            "text": "Procedures for Issuing Manuscripts in the Northeast Fisheries Science Center Reference Document (CRD) Series",
            "page": 5,
            "level": 1,
            "bookmark_include": True,
        },
    ]

    added = _add_bookmarks(pdf, headings, elements)

    assert added == 7
    with pdf.open_outline() as outline:
        titles = []
        def walk(items):
            for item in items:
                titles.append(item.title)
                walk(item.children)

        walk(outline.root)

    assert "TABLE OF CONTENTS" in titles
    assert "1 Panel Report" in titles
    assert "Abbreviations for fish stocks reviewed" in titles
    assert "2. ATLANTIC BLUEFISH" in titles
    assert "Anthony Wood" in titles
    assert "References:" in titles
    assert (
        "Procedures for Issuing Manuscripts in the Northeast Fisheries Science Center Reference Document (CRD) Series"
        in titles
    )
    assert "U.S. Department of Commerce" not in titles


def test_add_bookmarks_uses_all_docling_headings_without_keyword_filtering():
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))

    headings = [
        {"level": 2, "text": "1 SUMMARY", "page_index": 0},
        {"level": 2, "text": "3 ABOUT THE ORGANIZERS", "page_index": 0},
    ]
    elements = [
        {"type": "heading", "text": "More than just digital paper-hidden features of the PDF format", "page": 0, "level": 1},
        {"type": "heading", "text": "2 TOPICS", "page": 0, "level": 2},
        {"type": "heading", "text": "3 ABOUT THE ORGANIZERS", "page": 0, "level": 2},
        {"type": "heading", "text": "ABSTRACT", "page": 0, "level": 2},
        {"type": "heading", "text": "CCS CONCEPTS", "page": 0, "level": 2},
        {"type": "heading", "text": "KEYWORDS", "page": 0, "level": 2},
        {"type": "heading", "text": "ACM Reference Format:", "page": 0, "level": 3},
        {"type": "heading", "text": "1 SUMMARY", "page": 0, "level": 2},
    ]

    added = _add_bookmarks(pdf, headings, elements)

    assert added == 8
    with pdf.open_outline() as outline:
        titles = []
        def walk(items):
            for item in items:
                titles.append(item.title)
                walk(item.children)
        walk(outline.root)

    assert titles == [
        "More than just digital paper-hidden features of the PDF format",
        "2 TOPICS",
        "3 ABOUT THE ORGANIZERS",
        "ABSTRACT",
        "CCS CONCEPTS",
        "KEYWORDS",
        "ACM Reference Format:",
        "1 SUMMARY",
    ]


def test_add_bookmarks_uses_document_title_to_collapse_first_page_title_fragments():
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.add_blank_page(page_size=(200, 200))

    headings = [
        {"level": 1, "text": "C H A P T E R", "page_index": 0},
        {"level": 2, "text": "The Brain, Body, Motor Skills, and Sexual Development", "page_index": 0},
        {"level": 6, "text": "An Overview of Maturation and Growth", "page_index": 0},
        {"level": 6, "text": "Motor Development", "page_index": 0},
    ]

    added = _add_bookmarks(
        pdf,
        headings,
        elements=None,
        native_toc=None,
        document_title="CHAPTER 5 Physical Development: The Brain, Body, Motor Skills, and Sexual Development",
    )

    assert added == 3
    with pdf.open_outline() as outline:
        titles = [item.title for item in outline.root]
        child_titles = [child.title for child in outline.root[0].children]

    assert titles == ["CHAPTER 5 Physical Development: The Brain, Body, Motor Skills, and Sexual Development"]
    assert child_titles == [
        "An Overview of Maturation and Growth",
        "Motor Development",
    ]


def _run_media_clip_cycle_script(script_body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script_body)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_normalize_media_clip_data_dicts_handles_direct_self_referential_dictionary():
    result = _run_media_clip_cycle_script(
        """
        import pikepdf
        from app.pipeline.tagger import _normalize_media_clip_data_dicts

        pdf = pikepdf.new()
        media_clip = pikepdf.Dictionary()
        media_clip["/S"] = pikepdf.Name("/MCD")
        media_clip["/D"] = pikepdf.Dictionary({"/F": pikepdf.String("movie.mp4")})
        media_clip["/Self"] = media_clip
        pdf.Root["/Names"] = media_clip
        changes = _normalize_media_clip_data_dicts(pdf)
        print("changes", changes)
        print("ct", str(media_clip["/CT"]))
        print("alt", str(media_clip["/Alt"][1]))
        del media_clip["/Self"]
        pdf.Root["/Names"] = pikepdf.Dictionary()
        """
    )

    assert result.returncode == 0, result.stderr
    assert "changes 2" in result.stdout
    assert "ct video/mp4" in result.stdout
    assert "alt movie.mp4" in result.stdout


def test_normalize_media_clip_data_dicts_handles_direct_self_referential_array():
    result = _run_media_clip_cycle_script(
        """
        import pikepdf
        from app.pipeline.tagger import _normalize_media_clip_data_dicts

        pdf = pikepdf.new()
        kids = pikepdf.Array()
        kids.append(kids)
        pdf.Root["/Names"] = pikepdf.Dictionary({"/Kids": kids})
        changes = _normalize_media_clip_data_dicts(pdf)
        print("changes", changes)
        kids[0] = pikepdf.Array()
        pdf.Root["/Names"] = pikepdf.Dictionary()
        """
    )

    assert result.returncode == 0, result.stderr
    assert "changes 0" in result.stdout
