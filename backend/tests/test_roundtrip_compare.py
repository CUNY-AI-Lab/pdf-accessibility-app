import pikepdf
import pytest

from app.pipeline.structure import StructureResult
from app.services import roundtrip_compare
from app.services.roundtrip_compare import compare_roundtrip_pdfs, render_roundtrip_markdown
from scripts.strip_accessibility import strip_accessibility


def _build_gold_pdf(path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 300))

    font = pdf.make_indirect(
        pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Font"),
            "/Subtype": pikepdf.Name("/Type1"),
            "/BaseFont": pikepdf.Name("/Helvetica"),
        })
    )
    page["/Resources"] = pikepdf.Dictionary({
        "/Font": pikepdf.Dictionary({
            "/F1": font,
        })
    })
    page["/Contents"] = pdf.make_stream(
        pikepdf.unparse_content_stream(
            [
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
                pikepdf.ContentStreamInstruction([pikepdf.Name("/F1"), 12], pikepdf.Operator("Tf")),
                pikepdf.ContentStreamInstruction([20, 240], pikepdf.Operator("Td")),
                pikepdf.ContentStreamInstruction([pikepdf.String("Introduction")], pikepdf.Operator("Tj")),
                pikepdf.ContentStreamInstruction([0, -24], pikepdf.Operator("Td")),
                pikepdf.ContentStreamInstruction(
                    [pikepdf.String("This paragraph preserves the intended meaning of the document.")],
                    pikepdf.Operator("Tj"),
                ),
                pikepdf.ContentStreamInstruction([0, -24], pikepdf.Operator("Td")),
                pikepdf.ContentStreamInstruction([pikepdf.String("Open the guide")], pikepdf.Operator("Tj")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
            ]
        )
    )

    link = pdf.make_indirect(
        pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Annot"),
            "/Subtype": pikepdf.Name("/Link"),
            "/Rect": pikepdf.Array([18, 188, 120, 205]),
            "/Border": pikepdf.Array([0, 0, 0]),
            "/Contents": pikepdf.String("Open the guide"),
            "/A": pikepdf.Dictionary({
                "/S": pikepdf.Name("/URI"),
                "/URI": pikepdf.String("https://example.com/guide"),
            }),
        })
    )
    widget = pdf.make_indirect(
        pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Annot"),
            "/Subtype": pikepdf.Name("/Widget"),
            "/FT": pikepdf.Name("/Tx"),
            "/Rect": pikepdf.Array([20, 130, 180, 150]),
            "/T": pikepdf.String("name"),
            "/TU": pikepdf.String("Name"),
            "/V": pikepdf.String("Ada"),
        })
    )
    page["/Annots"] = pikepdf.Array([link, widget])
    page["/Tabs"] = pikepdf.Name("/S")

    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({
        "/Marked": True,
    })
    pdf.Root["/Lang"] = pikepdf.String("en-US")
    pdf.Root["/AcroForm"] = pikepdf.Dictionary({
        "/Fields": pikepdf.Array([widget]),
    })
    pdf.docinfo["/Title"] = pikepdf.String("Accessible Test Document")

    with pdf.open_outline() as outline:
        outline.root.append(pikepdf.OutlineItem("Introduction", 0))

    pdf.save(path)


def _manifest() -> dict:
    return {
        "recoverable_assertions": [
            {"id": "lang", "kind": "document_lang", "expected": "en-US"},
            {
                "id": "title",
                "kind": "title_contains",
                "expected": "Accessible Test Document",
            },
            {
                "id": "body",
                "kind": "text_contains",
                "expected": "preserves the intended meaning of the document",
            },
            {
                "id": "order",
                "kind": "ordered_text",
                "expected": [
                    "Introduction",
                    "preserves the intended meaning of the document",
                ],
            },
            {
                "id": "field_present",
                "kind": "field_present",
                "field_name": "name",
                "field_type": "text",
            },
            {
                "id": "field_type",
                "kind": "field_type",
                "field_name": "name",
                "field_type": "text",
            },
            {
                "id": "field",
                "kind": "field_accessible_name",
                "field_name": "name",
                "expected": "Name",
            },
            {
                "id": "field_semantic",
                "kind": "field_accessible_name",
                "field_name": "name",
                "required_terms_all": ["name"],
            },
            {
                "id": "link",
                "kind": "link_contents",
                "uri": "https://example.com/guide",
                "expected": "Open the guide",
            },
            {
                "id": "bookmark",
                "kind": "bookmark_title",
                "expected": "Introduction",
            },
        ]
    }


def _structure_json() -> dict:
    return {
        "elements": [
            {"type": "heading", "text": "Introduction"},
            {
                "type": "paragraph",
                "text": "This paragraph preserves the intended meaning of the document.",
            },
            {"type": "paragraph", "text": "Open the guide"},
        ]
    }


@pytest.mark.asyncio
async def test_roundtrip_compare_gold_against_itself_passes(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    _build_gold_pdf(gold_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=gold_pdf,
        manifest=_manifest(),
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["metadata"]["page_count_match"] is True
    assert report["metadata"]["document_lang_match"] is True
    assert report["metadata"]["title_match"] is True
    assert report["comparisons"]["structure"]["type_metrics"]["recoverable_type_recall"] == 1.0
    assert report["comparisons"]["fields"]["field_presence_match_rate"] == 1.0
    assert report["comparisons"]["fields"]["field_type_match_rate"] == 1.0
    assert report["comparisons"]["fields"]["named_field_match_rate"] == 1.0
    assert report["comparisons"]["links"]["descriptive_link_match_rate"] == 1.0
    assert report["comparisons"]["bookmarks"]["bookmark_match_rate"] == 1.0
    assert report["comparisons"]["bookmarks"]["recoverable_bookmark_match_rate"] == 1.0
    assert report["assertions"]["recoverable"]["passed"] == 10
    assert report["assertions"]["recoverable"]["failed"] == 0

    markdown = render_roundtrip_markdown(report)
    assert "Recoverable assertions: `10/10` passed" in markdown
    assert "Recoverable structure-type recall: `1.0`" in markdown
    assert "Field presence match rate: `1.0`" in markdown
    assert "Recoverable bookmark match rate: `1.0`" in markdown


@pytest.mark.asyncio
async def test_roundtrip_compare_gold_against_stripped_candidate_flags_missing_semantics(
    tmp_path,
) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    stripped_pdf = tmp_path / "stripped.pdf"
    _build_gold_pdf(gold_pdf)
    strip_accessibility(input_path=gold_pdf, output_path=stripped_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=stripped_pdf,
        manifest=_manifest(),
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["metadata"]["page_count_match"] is True
    assert report["metadata"]["document_lang_match"] is False
    assert report["metadata"]["title_match"] is False
    assert report["metadata"]["raw_text_similarity"] >= 0.9
    assert report["comparisons"]["fields"]["field_presence_match_rate"] == 1.0
    assert report["comparisons"]["fields"]["field_type_match_rate"] == 1.0
    assert report["comparisons"]["fields"]["named_field_match_rate"] == 0.0
    assert report["comparisons"]["links"]["descriptive_link_match_rate"] == 0.0
    assert report["comparisons"]["bookmarks"]["bookmark_match_rate"] == 0.0
    assert report["comparisons"]["bookmarks"]["recoverable_bookmark_match_rate"] == 0.0
    assert report["assertions"]["recoverable"]["passed"] == 4
    assert report["assertions"]["recoverable"]["failed"] == 6


@pytest.mark.asyncio
async def test_roundtrip_compare_document_language_matches_primary_subtag(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    with pikepdf.Pdf.open(candidate_pdf, allow_overwriting_input=True) as pdf:
        pdf.Root["/Lang"] = pikepdf.String("en-GB")
        pdf.save(candidate_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest={"recoverable_assertions": [{"id": "lang", "kind": "document_lang", "expected": "en"}]},
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["metadata"]["document_lang_match"] is True
    assert report["assertions"]["recoverable"]["passed"] == 1
    assert report["assertions"]["recoverable"]["failed"] == 0


@pytest.mark.asyncio
async def test_roundtrip_compare_title_contains_accepts_ordered_visible_terms(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    with pikepdf.Pdf.open(gold_pdf, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Title"] = pikepdf.String("Management Track Assessments Completed in Fall 2022")
        pdf.save(gold_pdf)
    with pikepdf.Pdf.open(candidate_pdf, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Title"] = pikepdf.String("Management Track Assessments Fall 2022")
        pdf.save(candidate_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest={
            "recoverable_assertions": [
                {
                    "id": "title",
                    "kind": "title_contains",
                    "expected": "Management Track Assessments Fall 2022",
                }
            ]
        },
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["metadata"]["title_match"] is True
    assert report["assertions"]["recoverable"]["passed"] == 1
    assert report["assertions"]["recoverable"]["failed"] == 0


@pytest.mark.asyncio
async def test_roundtrip_compare_title_contains_ignores_punctuation_differences(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    with pikepdf.Pdf.open(gold_pdf, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Title"] = pikepdf.String("Fully Accessible PDFUA documents")
        pdf.save(gold_pdf)
    with pikepdf.Pdf.open(candidate_pdf, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Title"] = pikepdf.String("Fully Accessible PDF/UA documents")
        pdf.save(candidate_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest={
            "recoverable_assertions": [
                {
                    "id": "title",
                    "kind": "title_contains",
                    "expected": "Fully Accessible PDFUA documents",
                }
            ]
        },
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["metadata"]["title_match"] is True
    assert report["assertions"]["recoverable"]["passed"] == 1
    assert report["assertions"]["recoverable"]["failed"] == 0


@pytest.mark.asyncio
async def test_roundtrip_compare_bookmark_title_ignores_punctuation_differences(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    with pikepdf.Pdf.open(gold_pdf, allow_overwriting_input=True) as pdf:
        with pdf.open_outline() as outline:
            outline.root[:] = [pikepdf.OutlineItem("2 Atlantic wolffish", 0)]
        pdf.save(gold_pdf)
    with pikepdf.Pdf.open(candidate_pdf, allow_overwriting_input=True) as pdf:
        with pdf.open_outline() as outline:
            outline.root[:] = [pikepdf.OutlineItem("2. Atlantic wolffish", 0)]
        pdf.save(candidate_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest={"recoverable_assertions": [{"id": "bookmark", "kind": "bookmark_title", "expected": "2 Atlantic wolffish"}]},
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["assertions"]["recoverable"]["passed"] == 1
    assert report["assertions"]["recoverable"]["failed"] == 0


@pytest.mark.asyncio
async def test_roundtrip_compare_link_contents_ignores_toc_leader_punctuation(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    with pikepdf.Pdf.open(candidate_pdf, allow_overwriting_input=True) as pdf:
        link = pdf.pages[0]["/Annots"][0]
        link["/Contents"] = pikepdf.String("Open the................................guide")
        pdf.save(candidate_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest=_manifest(),
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["comparisons"]["links"]["descriptive_link_match_rate"] == 1.0


@pytest.mark.asyncio
async def test_roundtrip_compare_ignores_generated_gold_link_contents(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    with pikepdf.Pdf.open(gold_pdf, allow_overwriting_input=True) as pdf:
        link = pdf.pages[0]["/Annots"][0]
        link["/Contents"] = pikepdf.String("jump to destination section.1")
        pdf.save(gold_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest=_manifest(),
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["comparisons"]["links"]["gold_descriptive_links"] == 0
    assert report["comparisons"]["links"]["descriptive_link_match_rate"] is None


@pytest.mark.asyncio
async def test_roundtrip_compare_ignores_generated_bibitem_gold_link_contents(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    with pikepdf.Pdf.open(gold_pdf, allow_overwriting_input=True) as pdf:
        link = pdf.pages[0]["/Annots"][0]
        link["/A"] = pikepdf.Dictionary({
            "/S": pikepdf.Name("/GoTo"),
            "/D": pikepdf.Name("/cite.AcroRead"),
        })
        link["/Contents"] = pikepdf.String("jump to bibitem cite.AcroRead")
        pdf.save(gold_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest=_manifest(),
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["comparisons"]["links"]["gold_descriptive_links"] == 0
    assert report["comparisons"]["links"]["descriptive_link_match_rate"] is None


@pytest.mark.asyncio
async def test_roundtrip_compare_bookmarks_allow_ordered_title_supersets(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    with pikepdf.Pdf.open(gold_pdf, allow_overwriting_input=True) as pdf:
        with pdf.open_outline() as outline:
            outline.root[:] = [pikepdf.OutlineItem("Appendix A AOP Meeting Summary", 0)]
        pdf.save(gold_pdf)
    with pikepdf.Pdf.open(candidate_pdf, allow_overwriting_input=True) as pdf:
        with pdf.open_outline() as outline:
            outline.root[:] = [
                pikepdf.OutlineItem(
                    "Appendix A. AOP Meeting Summary. . Appendix A.1. Meeting participants . . . 12",
                    0,
                )
            ]
        pdf.save(candidate_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest={"recoverable_assertions": [{"id": "bookmark", "kind": "bookmark_title", "expected": "Appendix A AOP Meeting Summary"}]},
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["comparisons"]["bookmarks"]["bookmark_match_rate"] == 1.0
    assert report["assertions"]["recoverable"]["passed"] == 1
    assert report["assertions"]["recoverable"]["failed"] == 0


@pytest.mark.asyncio
async def test_roundtrip_compare_bookmarks_ignore_punctuation_inside_ordered_terms(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    with pikepdf.Pdf.open(gold_pdf, allow_overwriting_input=True) as pdf:
        with pdf.open_outline() as outline:
            outline.root[:] = [pikepdf.OutlineItem("Procedures for CRD Series", 0)]
        pdf.save(gold_pdf)
    with pikepdf.Pdf.open(candidate_pdf, allow_overwriting_input=True) as pdf:
        with pdf.open_outline() as outline:
            outline.root[:] = [
                pikepdf.OutlineItem(
                    "Procedures for Issuing Manuscripts in the Northeast Fisheries Science Center Reference Document (CRD) Series",
                    0,
                )
            ]
        pdf.save(candidate_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest={"recoverable_assertions": [{"id": "bookmark", "kind": "bookmark_title", "expected": "Procedures for CRD Series"}]},
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["comparisons"]["bookmarks"]["bookmark_match_rate"] == 1.0


@pytest.mark.asyncio
async def test_roundtrip_compare_splits_recoverable_and_higher_order_bookmark_gaps(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    with pikepdf.Pdf.open(gold_pdf, allow_overwriting_input=True) as pdf:
        with pdf.open_outline() as outline:
            outline.root[:] = [
                pikepdf.OutlineItem("Introduction", 0),
                pikepdf.OutlineItem("Cover", 0),
            ]
        pdf.save(gold_pdf)

    with pikepdf.Pdf.open(candidate_pdf, allow_overwriting_input=True) as pdf:
        with pdf.open_outline() as outline:
            outline.root[:] = [pikepdf.OutlineItem("Introduction", 0)]
        pdf.save(candidate_pdf)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest={"recoverable_assertions": [{"id": "bookmark", "kind": "bookmark_title", "expected": "Introduction"}]},
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    bookmarks = report["comparisons"]["bookmarks"]
    assert bookmarks["bookmark_match_rate"] == 0.5
    assert bookmarks["recoverable_gold_total"] == 1
    assert bookmarks["recoverable_matched_titles"] == 1
    assert bookmarks["recoverable_bookmark_match_rate"] == 1.0
    assert bookmarks["higher_order_gold_total"] == 1
    assert bookmarks["higher_order_matched_titles"] == 0
    assert bookmarks["higher_order_bookmark_match_rate"] == 0.0
    assert bookmarks["missing_recoverable_titles"] == []
    assert bookmarks["missing_higher_order_titles"] == ["Cover"]


def test_compare_bookmarks_requires_contiguous_visible_phrase_for_recoverability() -> None:
    bookmarks = roundtrip_compare._compare_bookmarks(
        [
            "Cover",
            "Inside-Cover page",
            "Series Information",
            "Glossaries",
        ],
        ["Glossaries"],
        gold_visible_sources=[
            (
                "This section was covered later. "
                "The inside front and outside back covers include series-wide guidance and "
                "information quality notes. Glossaries list abbreviations."
            )
        ],
    )

    assert bookmarks["recoverable_gold_total"] == 1
    assert bookmarks["recoverable_matched_titles"] == 1
    assert bookmarks["recoverable_bookmark_match_rate"] == 1.0
    assert bookmarks["higher_order_gold_total"] == 3
    assert bookmarks["missing_higher_order_titles"] == [
        "Cover",
        "Inside-Cover page",
        "Series Information",
    ]


def test_compare_bookmarks_allows_letter_digit_spacing_variants() -> None:
    bookmarks = roundtrip_compare._compare_bookmarks(
        ["6 Overview of PDF Standard Tags15"],
        ["6 Overview of PDF Standard Tags 15"],
        gold_visible_sources=[],
    )

    assert bookmarks["bookmark_match_rate"] == 1.0
    assert bookmarks["matched_titles"] == 1
    assert bookmarks["missing_titles"] == []


def test_bookmark_probe_sources_only_use_title_like_structure_elements() -> None:
    sources = roundtrip_compare._bookmark_probe_sources({
        "elements": [
            {"type": "paragraph", "text": "This section covers the topic in detail."},
            {"type": "heading", "text": "Introduction"},
            {"type": "toc_item", "text": "Glossaries . . . v"},
            {"type": "paragraph", "text": "Series information appears in prose here."},
        ]
    })

    assert sources == ["Introduction", "Glossaries . . . v"]


@pytest.mark.asyncio
async def test_roundtrip_compare_internal_link_matching_ignores_cross_pdf_page_object_ids(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    for path in (gold_pdf, candidate_pdf):
        with pikepdf.Pdf.open(path, allow_overwriting_input=True) as pdf:
            link = pdf.pages[0]["/Annots"][0]
            link["/A"] = pikepdf.Dictionary({
                "/S": pikepdf.Name("/GoTo"),
                "/D": pikepdf.Array([pdf.pages[0].obj, pikepdf.Name("/XYZ"), 20, 240, 0]),
            })
            pdf.save(path)

    manifest = {
        "recoverable_assertions": [
            {
                "id": "link",
                "kind": "link_contents",
                "page": 1,
                "expected": "Open the guide",
            }
        ]
    }

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest=manifest,
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["comparisons"]["links"]["descriptive_link_match_rate"] == 1.0
    assert report["assertions"]["recoverable"]["passed"] == 1
    assert report["assertions"]["recoverable"]["failed"] == 0


@pytest.mark.asyncio
async def test_roundtrip_compare_field_assertions_support_semantic_variants(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    with pikepdf.Pdf.open(candidate_pdf, allow_overwriting_input=True) as pdf:
        widget = pdf.Root["/AcroForm"]["/Fields"][0]
        widget["/TU"] = pikepdf.String("Section 1: Name")
        pdf.save(candidate_pdf)

    manifest = {
        "recoverable_assertions": [
            {
                "id": "field_exact_any",
                "kind": "field_accessible_name",
                "field_name": "name",
                "expected_any": ["Name", "Section 1: Name"],
            },
            {
                "id": "field_contains",
                "kind": "field_accessible_name",
                "field_name": "name",
                "expected": "Name",
                "match_mode": "contains",
            },
            {
                "id": "field_terms",
                "kind": "field_accessible_name",
                "field_name": "name",
                "required_terms_all": ["name"],
                "required_terms_any": ["name", "section 1"],
            },
        ]
    }

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest=manifest,
        gold_structure_json=_structure_json(),
        candidate_structure_json=_structure_json(),
    )

    assert report["comparisons"]["fields"]["named_field_match_rate"] == 0.0
    assert report["assertions"]["recoverable"]["passed"] == 3
    assert report["assertions"]["recoverable"]["failed"] == 0


@pytest.mark.asyncio
async def test_roundtrip_compare_structure_type_recall_flags_missing_heading_semantics(tmp_path) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    gold_structure = {
        "elements": [
            {"type": "heading", "text": "Introduction"},
            {"type": "paragraph", "text": "This paragraph preserves the intended meaning of the document."},
            {"type": "list_item", "text": "First bullet"},
            {"type": "note", "text": "Footnote 1"},
            {"type": "table", "text": "Table 1"},
        ]
    }
    candidate_structure = {
        "elements": [
            {"type": "paragraph", "text": "Introduction"},
            {"type": "paragraph", "text": "This paragraph preserves the intended meaning of the document."},
            {"type": "note", "text": "Footnote 1"},
        ]
    }

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest={"recoverable_assertions": []},
        gold_structure_json=gold_structure,
        candidate_structure_json=candidate_structure,
    )

    type_metrics = report["comparisons"]["structure"]["type_metrics"]
    assert type_metrics["recoverable_type_recall"] == 0.4
    assert type_metrics["tracked_types"]["heading"]["recall"] == 0.0
    assert type_metrics["tracked_types"]["paragraph"]["recall"] == 1.0
    assert type_metrics["tracked_types"]["list_item"]["recall"] == 0.0
    assert type_metrics["tracked_types"]["note"]["recall"] == 1.0
    assert type_metrics["tracked_types"]["table"]["recall"] == 0.0


@pytest.mark.asyncio
async def test_roundtrip_compare_extracts_structure_without_figure_images(tmp_path, monkeypatch) -> None:
    gold_pdf = tmp_path / "gold.pdf"
    candidate_pdf = tmp_path / "candidate.pdf"
    _build_gold_pdf(gold_pdf)
    _build_gold_pdf(candidate_pdf)

    calls: list[tuple[str, bool]] = []

    async def fake_extract_structure(pdf_path, job_dir, *, include_figure_images=True):
        calls.append((pdf_path.name, include_figure_images))
        return StructureResult(document_json=_structure_json())

    monkeypatch.setattr(roundtrip_compare, "extract_structure", fake_extract_structure)

    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest={"recoverable_assertions": []},
        work_dir=tmp_path / "compare_work",
    )

    assert report["comparisons"]["structure"]["transcript_similarity"] == 1.0
    assert calls == [("gold.pdf", False), ("candidate.pdf", False)]
