"""Step 5: orchestrate PDF/UA tagging; retain the historical tagger imports."""

import asyncio
import logging
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

import pikepdf
from docling_parse.pdf_parser import DoclingPdfParser
from PIL import Image

from app.pipeline.element_text import accounts_for as accounts_for
from app.pipeline.element_text import readable as readable
from app.pipeline.fonts.resources import TAGGER_RESOURCES as TAGGER_RESOURCES
from app.pipeline.fonts.resources import _has_embedded_font as _has_embedded_font
from app.pipeline.fonts.resources import _iter_pdf_fonts as _iter_pdf_fonts
from app.pipeline.form_xobjects import unique_form_instances
from app.pipeline.page_frame import PageFrame
from app.pipeline.page_glyphs import MeasuredGlyph as MeasuredGlyph
from app.pipeline.page_glyphs import MeasuredTextOp as MeasuredTextOp
from app.pipeline.page_glyphs import PdfGlyphReader
from app.pipeline.page_glyphs import union_bbox as union_bbox
from app.pipeline.pdf_repair import add_missing_icc_components as add_missing_icc_components
from app.pipeline.pdfium_text import PdfiumText
from app.pipeline.reading_order import choose_order as choose_order
from app.pipeline.tagger_annotations import (
    _build_docling_parse_page_hyperlinks as _build_docling_parse_page_hyperlinks,
)
from app.pipeline.tagger_annotations import (
    _build_docling_parse_page_widgets as _build_docling_parse_page_widgets,
)
from app.pipeline.tagger_annotations import (
    _build_docling_parse_page_words as _build_docling_parse_page_words,
)
from app.pipeline.tagger_annotations import (
    _ensure_annotation_baseline as _ensure_annotation_baseline,
)
from app.pipeline.tagger_annotations import (
    _extract_docling_parse_text_cells as _extract_docling_parse_text_cells,
)
from app.pipeline.tagger_annotations import _get_docling_parse_page as _get_docling_parse_page
from app.pipeline.tagger_annotations import _infer_annotation_contents as _infer_annotation_contents
from app.pipeline.tagger_annotations import _prepare_link_targets as _prepare_link_targets
from app.pipeline.tagger_annotations import (
    _prune_incidental_annotations as _prune_incidental_annotations,
)
from app.pipeline.tagger_annotations import _tag_generic_annotations as _tag_generic_annotations
from app.pipeline.tagger_annotations import _tag_link_annotations as _tag_link_annotations
from app.pipeline.tagger_annotations import _tag_widget_annotations as _tag_widget_annotations
from app.pipeline.tagger_annotations import _widget_label_order as _widget_label_order
from app.pipeline.tagger_bookmarks import APPENDIX_BOOKMARK_KEY_RE as APPENDIX_BOOKMARK_KEY_RE
from app.pipeline.tagger_bookmarks import NUMERIC_BOOKMARK_KEY_RE as NUMERIC_BOOKMARK_KEY_RE
from app.pipeline.tagger_bookmarks import TOC_TRAILING_PAGE_RE as TOC_TRAILING_PAGE_RE
from app.pipeline.tagger_bookmarks import _add_bookmarks as _add_bookmarks
from app.pipeline.tagger_bookmarks import _bookmark_plan_entries as _bookmark_plan_entries
from app.pipeline.tagger_bookmarks import _bookmark_section_key as _bookmark_section_key
from app.pipeline.tagger_bookmarks import _clean_bookmark_label as _clean_bookmark_label
from app.pipeline.tagger_bookmarks import (
    _combine_bookmark_source_entries as _combine_bookmark_source_entries,
)
from app.pipeline.tagger_bookmarks import (
    _docling_heading_bookmark_entries as _docling_heading_bookmark_entries,
)
from app.pipeline.tagger_bookmarks import _heading_bookmark_entries as _heading_bookmark_entries
from app.pipeline.tagger_bookmarks import (
    _maybe_synthesize_title_bookmark as _maybe_synthesize_title_bookmark,
)
from app.pipeline.tagger_bookmarks import (
    _native_toc_bookmark_entries as _native_toc_bookmark_entries,
)
from app.pipeline.tagger_bookmarks import (
    _sanitize_outline_leaf_counts as _sanitize_outline_leaf_counts,
)
from app.pipeline.tagger_bookmarks import (
    _select_heading_bookmark_target as _select_heading_bookmark_target,
)
from app.pipeline.tagger_bookmarks import (
    _sort_heading_bookmark_entries as _sort_heading_bookmark_entries,
)
from app.pipeline.tagger_bookmarks import _toc_bookmark_entries as _toc_bookmark_entries
from app.pipeline.tagger_content import _ARTIFACT_MARK as _ARTIFACT_MARK
from app.pipeline.tagger_content import _PASSTHROUGH_MARK as _PASSTHROUGH_MARK
from app.pipeline.tagger_content import PreparedContent as PreparedContent
from app.pipeline.tagger_content import _allocate_fragment_mcid as _allocate_fragment_mcid
from app.pipeline.tagger_content import (
    _artifact_properties_for_element as _artifact_properties_for_element,
)
from app.pipeline.tagger_content import _figure_of as _figure_of
from app.pipeline.tagger_content import _fragment_tag_for_element as _fragment_tag_for_element
from app.pipeline.tagger_content import _instruction_paints_content as _instruction_paints_content
from app.pipeline.tagger_content import (
    _is_full_page_scan_image_region as _is_full_page_scan_image_region,
)
from app.pipeline.tagger_content import _make_bdc as _make_bdc
from app.pipeline.tagger_content import _make_bmc_artifact as _make_bmc_artifact
from app.pipeline.tagger_content import (
    _make_clipped_image_figure_instructions as _make_clipped_image_figure_instructions,
)
from app.pipeline.tagger_content import _make_emc as _make_emc
from app.pipeline.tagger_content import _page_reading_order as _page_reading_order
from app.pipeline.tagger_content import _prepare_content_stream as _prepare_content_stream
from app.pipeline.tagger_content import _remap_word_owners as _remap_word_owners
from app.pipeline.tagger_content import (
    _resolved_figure_alt_for_tagging as _resolved_figure_alt_for_tagging,
)
from app.pipeline.tagger_content import _rewrite_content_stream as _rewrite_content_stream
from app.pipeline.tagger_content import _scan_figure_overlay_targets as _scan_figure_overlay_targets
from app.pipeline.tagger_content_geometry import ContentRegion as ContentRegion
from app.pipeline.tagger_content_geometry import FormInvocation as FormInvocation
from app.pipeline.tagger_content_geometry import TextShowRun as TextShowRun
from app.pipeline.tagger_content_geometry import (
    _apply_measured_geometry as _apply_measured_geometry,
)
from app.pipeline.tagger_content_geometry import (
    _extract_content_regions as _extract_content_regions,
)
from app.pipeline.tagger_content_geometry import (
    _extract_form_invocations as _extract_form_invocations,
)
from app.pipeline.tagger_content_geometry import _extract_text_show_runs as _extract_text_show_runs
from app.pipeline.tagger_content_geometry import _is_ocr_form_xobject as _is_ocr_form_xobject
from app.pipeline.tagger_content_geometry import (
    _measured_by_instruction as _measured_by_instruction,
)
from app.pipeline.tagger_content_geometry import _page_content_bbox as _page_content_bbox
from app.pipeline.tagger_content_geometry import (
    _split_text_ops_across_targets as _split_text_ops_across_targets,
)
from app.pipeline.tagger_content_geometry import _strip_existing_markers as _strip_existing_markers
from app.pipeline.tagger_content_geometry import (
    _text_runs_look_like_hidden_ocr_layer as _text_runs_look_like_hidden_ocr_layer,
)
from app.pipeline.tagger_content_geometry import _update_text_pos as _update_text_pos
from app.pipeline.tagger_geometry import BLANK_PAGE_MAX_INK_RATIO as BLANK_PAGE_MAX_INK_RATIO
from app.pipeline.tagger_geometry import (
    BLANK_PAGE_NONWHITE_THRESHOLD as BLANK_PAGE_NONWHITE_THRESHOLD,
)
from app.pipeline.tagger_geometry import BOOKMARK_SPACED_CAPS_RE as BOOKMARK_SPACED_CAPS_RE
from app.pipeline.tagger_geometry import CONTENT_PAINT_OPERATORS as CONTENT_PAINT_OPERATORS
from app.pipeline.tagger_geometry import IDENTITY as IDENTITY
from app.pipeline.tagger_geometry import LINK_SAFE_MAX_CHARS as LINK_SAFE_MAX_CHARS
from app.pipeline.tagger_geometry import LINK_SAFE_MAX_WORDS as LINK_SAFE_MAX_WORDS
from app.pipeline.tagger_geometry import LINK_TEXT_ELEMENT_TYPES as LINK_TEXT_ELEMENT_TYPES
from app.pipeline.tagger_geometry import OCR_NOISE_ONLY_OPERATORS as OCR_NOISE_ONLY_OPERATORS
from app.pipeline.tagger_geometry import PATH_CONSTRUCTION_OPERATORS as PATH_CONSTRUCTION_OPERATORS
from app.pipeline.tagger_geometry import PATH_PAINTING_OPERATORS as PATH_PAINTING_OPERATORS
from app.pipeline.tagger_geometry import TEXT_SHOW_OPERATORS as TEXT_SHOW_OPERATORS
from app.pipeline.tagger_geometry import WEAK_FIGURE_ALT_RE as WEAK_FIGURE_ALT_RE
from app.pipeline.tagger_geometry import ContentTarget as ContentTarget
from app.pipeline.tagger_geometry import _as_positive_int as _as_positive_int
from app.pipeline.tagger_geometry import _bbox_area as _bbox_area
from app.pipeline.tagger_geometry import _bbox_center as _bbox_center
from app.pipeline.tagger_geometry import _bbox_from_center as _bbox_from_center
from app.pipeline.tagger_geometry import _bbox_from_points as _bbox_from_points
from app.pipeline.tagger_geometry import _bbox_intersection as _bbox_intersection
from app.pipeline.tagger_geometry import _bbox_intersection_box as _bbox_intersection_box
from app.pipeline.tagger_geometry import _bbox_iou as _bbox_iou
from app.pipeline.tagger_geometry import _coerce_int as _coerce_int
from app.pipeline.tagger_geometry import _collapse_spaced_caps as _collapse_spaced_caps
from app.pipeline.tagger_geometry import _containment_ratio as _containment_ratio
from app.pipeline.tagger_geometry import _decode_pdf_text_operand as _decode_pdf_text_operand
from app.pipeline.tagger_geometry import _element_boxes as _element_boxes
from app.pipeline.tagger_geometry import _expand_bbox as _expand_bbox
from app.pipeline.tagger_geometry import _extract_text_from_operands as _extract_text_from_operands
from app.pipeline.tagger_geometry import _glyph_key as _glyph_key
from app.pipeline.tagger_geometry import _lookup_xobject as _lookup_xobject
from app.pipeline.tagger_geometry import _mat_multiply as _mat_multiply
from app.pipeline.tagger_geometry import _matrix_tuple as _matrix_tuple
from app.pipeline.tagger_geometry import _normalize_annotation_rect as _normalize_annotation_rect
from app.pipeline.tagger_geometry import _normalize_text as _normalize_text
from app.pipeline.tagger_geometry import _normalize_title_like_text as _normalize_title_like_text
from app.pipeline.tagger_geometry import _obj_key as _obj_key
from app.pipeline.tagger_geometry import _rect_array_to_bbox as _rect_array_to_bbox
from app.pipeline.tagger_geometry import _rect_to_bbox as _rect_to_bbox
from app.pipeline.tagger_geometry import _region_bbox as _region_bbox
from app.pipeline.tagger_geometry import _resolve_dictionary as _resolve_dictionary
from app.pipeline.tagger_geometry import _safe_float as _safe_float
from app.pipeline.tagger_geometry import _table_cell_bbox as _table_cell_bbox
from app.pipeline.tagger_geometry import _transform_point as _transform_point
from app.pipeline.tagger_layout_tables import _layout_table_plan
from app.pipeline.tagger_link_labels import (
    _best_annotation_source_element as _best_annotation_source_element,
)
from app.pipeline.tagger_link_labels import _infer_link_contents as _infer_link_contents
from app.pipeline.tagger_link_labels import (
    _infer_widget_accessible_name as _infer_widget_accessible_name,
)
from app.pipeline.tagger_link_labels import (
    _is_safe_inferred_link_label as _is_safe_inferred_link_label,
)
from app.pipeline.tagger_link_labels import (
    _visible_link_contents_from_page_elements as _visible_link_contents_from_page_elements,
)
from app.pipeline.tagger_link_labels import (
    _visible_link_contents_from_page_hyperlinks as _visible_link_contents_from_page_hyperlinks,
)
from app.pipeline.tagger_link_labels import (
    _visible_link_contents_from_page_words as _visible_link_contents_from_page_words,
)
from app.pipeline.tagger_normalization import FILE_NAME_TITLE_RE as FILE_NAME_TITLE_RE
from app.pipeline.tagger_normalization import OFFICE_TITLE_PREFIX_RE as OFFICE_TITLE_PREFIX_RE
from app.pipeline.tagger_normalization import PLACEHOLDER_TITLE_RE as PLACEHOLDER_TITLE_RE
from app.pipeline.tagger_normalization import (
    _is_placeholder_document_title as _is_placeholder_document_title,
)
from app.pipeline.tagger_normalization import (
    _normalize_embedded_file_specs as _normalize_embedded_file_specs,
)
from app.pipeline.tagger_normalization import (
    _normalize_heading_hierarchy as _normalize_heading_hierarchy,
)
from app.pipeline.tagger_normalization import (
    _normalize_media_clip_data_dicts as _normalize_media_clip_data_dicts,
)
from app.pipeline.tagger_normalization import (
    _normalize_optional_content_configs as _normalize_optional_content_configs,
)
from app.pipeline.tagger_normalization import (
    _normalize_type1_font_charsets as _normalize_type1_font_charsets,
)
from app.pipeline.tagger_normalization import _remove_dynamic_xfa as _remove_dynamic_xfa
from app.pipeline.tagger_normalization import _reset_xmp_metadata as _reset_xmp_metadata
from app.pipeline.tagger_normalization import (
    _resolve_document_language as _resolve_document_language,
)
from app.pipeline.tagger_normalization import (
    _save_pdf_for_viewer_compatibility as _save_pdf_for_viewer_compatibility,
)
from app.pipeline.tagger_normalization import _set_document_title as _set_document_title
from app.pipeline.tagger_normalization import _set_pdfua_identification as _set_pdfua_identification
from app.pipeline.tagger_ownership import LIST_LABEL_MAX_LINE_HEIGHTS as LIST_LABEL_MAX_LINE_HEIGHTS
from app.pipeline.tagger_ownership import NEARBY_LINE_HEIGHTS as NEARBY_LINE_HEIGHTS
from app.pipeline.tagger_ownership import SCAN_PICTURE_MIN_PAGE_SHARE as SCAN_PICTURE_MIN_PAGE_SHARE
from app.pipeline.tagger_ownership import UNOWNED_MARGIN_BAND as UNOWNED_MARGIN_BAND
from app.pipeline.tagger_ownership import WORD_MARGIN as WORD_MARGIN
from app.pipeline.tagger_ownership import _axis_gap as _axis_gap
from app.pipeline.tagger_ownership import _best_bbox_match as _best_bbox_match
from app.pipeline.tagger_ownership import _column_overlap as _column_overlap
from app.pipeline.tagger_ownership import _content_targets as _content_targets
from app.pipeline.tagger_ownership import _fallback_reading_order as _fallback_reading_order
from app.pipeline.tagger_ownership import _ocr_cell_rule_words as _ocr_cell_rule_words
from app.pipeline.tagger_ownership import _owner_of as _owner_of
from app.pipeline.tagger_ownership import _owner_of_glyphs as _owner_of_glyphs
from app.pipeline.tagger_ownership import _page_has_hidden_text as _page_has_hidden_text
from app.pipeline.tagger_ownership import _page_vertical_rules as _page_vertical_rules
from app.pipeline.tagger_ownership import _recover_unowned_words as _recover_unowned_words
from app.pipeline.tagger_ownership import _upright as _upright
from app.pipeline.tagger_ownership import _word_break as _word_break
from app.pipeline.tagger_ownership import _word_lines as _word_lines
from app.pipeline.tagger_ownership import _word_owners as _word_owners
from app.pipeline.tagger_semantics import FORMULA_GREEK_WORDS as FORMULA_GREEK_WORDS
from app.pipeline.tagger_semantics import FORMULA_MULTI_LETTER_WORDS as FORMULA_MULTI_LETTER_WORDS
from app.pipeline.tagger_semantics import FORMULA_SUBSCRIPTS as FORMULA_SUBSCRIPTS
from app.pipeline.tagger_semantics import FORMULA_SUPERSCRIPTS as FORMULA_SUPERSCRIPTS
from app.pipeline.tagger_semantics import FORMULA_SYMBOL_WORDS as FORMULA_SYMBOL_WORDS
from app.pipeline.tagger_semantics import GLYPH_TEXT_ELEMENT_TYPES as GLYPH_TEXT_ELEMENT_TYPES
from app.pipeline.tagger_semantics import _attach_glyph_text as _attach_glyph_text
from app.pipeline.tagger_semantics import _element_accessible_text as _element_accessible_text
from app.pipeline.tagger_semantics import _element_actual_text as _element_actual_text
from app.pipeline.tagger_semantics import _elements_in_user_space as _elements_in_user_space
from app.pipeline.tagger_semantics import _formula_alt_text as _formula_alt_text
from app.pipeline.tagger_semantics import _is_weak_figure_alt_text as _is_weak_figure_alt_text
from app.pipeline.tagger_semantics import _items_in_user_space as _items_in_user_space
from app.pipeline.tagger_semantics import _mcr_page_index as _mcr_page_index
from app.pipeline.tagger_semantics import _source_figure_alt_lookup as _source_figure_alt_lookup
from app.pipeline.tagger_semantics import (
    _source_figure_alt_texts_by_page as _source_figure_alt_texts_by_page,
)
from app.pipeline.tagger_semantics import _split_formula_symbol_runs as _split_formula_symbol_runs
from app.pipeline.tagger_semantics import _spoken_formula_exponent as _spoken_formula_exponent
from app.pipeline.tagger_semantics import is_placeholder_alt_text as is_placeholder_alt_text
from app.pipeline.tagger_structure import StructTreeBuilder as StructTreeBuilder
from app.pipeline.tagger_structure import _source_key as _source_key
from app.pipeline.tagger_table_structure import (
    _finish_table_structure,
    _plan_external_table_headers,
)
from app.pipeline.tagger_tables import _clean_table_summary_text as _clean_table_summary_text
from app.pipeline.tagger_tables import _complete_table_grid_cells as _complete_table_grid_cells
from app.pipeline.tagger_tables import _infer_table_headers as _infer_table_headers
from app.pipeline.tagger_tables import _list_numbering_name as _list_numbering_name
from app.pipeline.tagger_tables import _numeric_table_text as _numeric_table_text
from app.pipeline.tagger_tables import _painted_table_rules as _painted_table_rules
from app.pipeline.tagger_tables import _set_list_numbering_attr as _set_list_numbering_attr
from app.pipeline.tagger_tables import _table_summary_text as _table_summary_text
from app.services.pdf_objects import pdf_dictionary
from app.services.pdf_objects import pdf_text as pdf_text

logger = logging.getLogger(__name__)


@dataclass
class TaggingResult:
    output_path: Path
    tags_added: int = 0
    lang_set: bool = False
    struct_elems_created: int = 0
    figures_tagged: int = 0
    headings_tagged: int = 0
    tables_tagged: int = 0
    lists_tagged: int = 0
    links_tagged: int = 0
    annotations_tagged: int = 0
    decorative_figures_artifacted: int = 0
    bookmarks_added: int = 0
    title_set: bool = False
    reading_order_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    # Zero-based page index and unclaimed word counts (including words
    # deliberately left hidden); exact Artifact owners are already claimed.
    unowned_words_by_page: list[dict[str, int]] = field(default_factory=list)


def _render_page_ink_ratio(pdf_path: Path, page_number: int) -> float | None:
    from app.services.pdf_preview import render_page_png_bytes

    try:
        page_png = render_page_png_bytes(pdf_path, page_number, dpi=72, max_width=900)
    except Exception:
        return None

    with Image.open(BytesIO(page_png)) as image:
        grayscale = image.convert("L")
        histogram = grayscale.histogram()

    total_pixels = sum(histogram)
    if total_pixels <= 0:
        return 0.0
    nonwhite_pixels = sum(histogram[:BLANK_PAGE_NONWHITE_THRESHOLD])
    return nonwhite_pixels / total_pixels


def _should_artifact_nonsemantic_page_content(
    input_path: Path,
    page: pikepdf.Page,
    page_index: int,
) -> bool:
    try:
        instructions = list(pikepdf.parse_content_stream(page))
    except Exception:
        return False

    instructions = _strip_existing_markers(instructions)
    if not instructions:
        return False

    regions = _extract_content_regions(instructions, page)
    if any(region.kind == "text" for region in regions):
        return False

    meaningful_ops: list[str] = []
    has_ocr_form = False
    has_image_xobject = False
    xobjects = {}
    try:
        resources = pdf_dictionary(page.get("/Resources"))
        if resources is not None:
            xobjects = pdf_dictionary(resources.get("/XObject")) or {}
    except Exception:
        xobjects = {}

    for instr in instructions:
        if not hasattr(instr, "operator"):
            continue
        op = str(instr.operator)
        if op in {"q", "Q", "cm"}:
            continue
        meaningful_ops.append(op)
        if op != "Do":
            continue
        operands = list(instr.operands) if hasattr(instr, "operands") else []
        if not operands:
            continue
        name = operands[0]
        xobject = xobjects.get(name) if hasattr(xobjects, "get") else None
        if xobject is None or not hasattr(xobject, "get"):
            continue
        subtype = xobject.get("/Subtype")
        if subtype == pikepdf.Name("/Image"):
            has_image_xobject = True
        elif _is_ocr_form_xobject(str(name), xobject):
            has_ocr_form = True

    ink_ratio = None
    if meaningful_ops and (
        all(op == "Do" for op in meaningful_ops)
        or (
            all(op in OCR_NOISE_ONLY_OPERATORS for op in meaningful_ops)
            and has_ocr_form
            and has_image_xobject
        )
    ):
        ink_ratio = _render_page_ink_ratio(input_path, page_index + 1)

    if meaningful_ops and all(op == "Do" for op in meaningful_ops):
        if ink_ratio is not None and ink_ratio <= BLANK_PAGE_MAX_INK_RATIO:
            return True

    return (
        bool(meaningful_ops)
        and all(op in OCR_NOISE_ONLY_OPERATORS for op in meaningful_ops)
        and has_ocr_form
        and has_image_xobject
        and ink_ratio is not None
        and ink_ratio <= BLANK_PAGE_MAX_INK_RATIO
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

async def tag_pdf(
    input_path: Path,
    output_path: Path,
    structure_json: dict,
    alt_texts: list[dict] | None = None,
    language: str | None = "en",
    original_filename: str = "",
) -> TaggingResult:
    """Write PDF/UA structure tags into the PDF."""

    def _tag(input_path: Path):
        tags_added = 0
        parser_doc = None
        docling_parse_word_cache: dict[int, list[dict]] = {}
        docling_parse_hyperlink_cache: dict[int, list[dict]] = {}
        docling_parse_widget_cache: dict[int, list[dict]] = {}
        document_language = _resolve_document_language(structure_json, language)

        try:
            parser_doc = DoclingPdfParser(loglevel="fatal").load(
                str(input_path),
                lazy=True,
            )
        except Exception as e:
            logger.warning(f"docling-parse unavailable for {input_path.name}: {e}")

        with (
            PdfiumText(input_path) as pdfium_text,
            PdfGlyphReader(input_path) as glyph_reader,
            pikepdf.open(str(input_path)) as pdf,
        ):
            frames = [PageFrame.of(page) for page in pdfium_text.document]
            elements = _elements_in_user_space(structure_json.get("elements", []), frames)
            source_figure_alt_lookup = _source_figure_alt_lookup(pdf, elements)

            # 1. Mark PDF as tagged
            if "/MarkInfo" not in pdf.Root:
                pdf.Root["/MarkInfo"] = pikepdf.Dictionary(
                    {
                        "/Marked": True,
                        "/Suspects": False,
                    }
                )
            else:
                pdf.Root["/MarkInfo"]["/Marked"] = True
                pdf.Root["/MarkInfo"]["/Suspects"] = False
            tags_added += 1

            # 2. Set document language
            pdf.Root["/Lang"] = pikepdf.String(document_language)
            tags_added += 1

            # 3. Set document title
            title = _set_document_title(pdf, structure_json, original_filename, pdf_path=input_path)
            title_set = bool(title)
            _set_pdfua_identification(pdf)
            _normalize_optional_content_configs(pdf)
            _remove_dynamic_xfa(pdf)
            _normalize_embedded_file_specs(pdf)
            _normalize_type1_font_charsets(pdf)
            _normalize_media_clip_data_dicts(pdf)

            # 4. Build structure tree
            builder = StructTreeBuilder(pdf, doc_lang=document_language)
            builder.setup()
            tags_added += 1

            # Build figure override maps from review decisions.
            alt_lookup: dict[int, str] = {}
            decorative_figures: set[int] = set()
            for alt in alt_texts or []:
                fig_idx = alt.get("figure_index")
                if not isinstance(fig_idx, int):
                    continue
                status = str(alt.get("status", "")).strip().lower()
                if status == "rejected" or bool(alt.get("decorative")):
                    decorative_figures.add(fig_idx)
                    continue
                text = alt.get("text")
                if isinstance(text, str) and not is_placeholder_alt_text(text):
                    alt_lookup[fig_idx] = text.strip()

            for fig_idx, source_alt in source_figure_alt_lookup.items():
                if _is_weak_figure_alt_text(source_alt):
                    continue
                if fig_idx in decorative_figures or _is_weak_figure_alt_text(
                    alt_lookup.get(fig_idx)
                ):
                    decorative_figures.discard(fig_idx)
                    alt_lookup[fig_idx] = source_alt

            # Group elements by page
            pages_elements: dict[int, list[dict]] = {}
            for elem in elements:
                pg = elem.get("page", 0)
                if pg not in pages_elements:
                    pages_elements[pg] = []
                pages_elements[pg].append(elem)
            _attach_glyph_text(pages_elements, pdfium_text)
            reading_order_diagnostics = []
            unowned_words_by_page = []
            page_owners = {}
            nonsemantic_pages = set()
            for page_index, page in enumerate(pdf.pages):
                page_elems = pages_elements.setdefault(page_index, [])
                nonsemantic = not page_elems and _should_artifact_nonsemantic_page_content(
                    input_path, page, page_index
                )
                if nonsemantic:
                    nonsemantic_pages.add(page_index)
                _prepare_link_targets(page, builder, page_elems)
                eligible_ops, stream_starts = [], set()
                for ops in glyph_reader.page(page_index).ops.values():
                    stream_starts.add(len(eligible_ops))
                    eligible_ops.extend(ops)
                for elem in page_elems:
                    if elem.get("type") == "table":
                        elem["_layout_table"] = _layout_table_plan(elem, page_elems)
                _plan_external_table_headers(page_elems, [g for op in eligible_ops for g in op.glyphs])
                _infer_table_headers(
                    page_elems, [g for op in eligible_ops for g in op.glyphs], page,
                )
                diagnostic = {"page": page_index, "before": 0, "after": 0}
                owners = _word_owners(
                    dict(enumerate(eligible_ops)), _content_targets(page_elems),
                    elements=None if nonsemantic else page_elems, frame=frames[page_index],
                    scanned=_page_has_hidden_text(page), diagnostic=diagnostic,
                    stream_starts=stream_starts, vertical_rules=_page_vertical_rules(page),
                )
                for index, elem in enumerate(page_elems):
                    if elem.get("fallback"):
                        elem["page"] = page_index
                        elem["_docling_order"] = index
                # Fallback anchors supply their initial logical position. The
                # same recovered ownership then informs both vote and rewrite.
                before = list(page_elems)
                page_elems.sort(key=lambda elem: elem["reading_order"])
                _remap_word_owners(owners, before, page_elems)
                reading_order_diagnostics.append(_page_reading_order(
                    page, page_index, page_elems, glyph_reader.page(page_index).ops,
                    frames[page_index], decorative_figures, word_owners=owners,
                ))
                page_owners[page_index] = owners
                unowned_words_by_page.append(diagnostic)
            elements = [elem for pg in sorted(pages_elements) for elem in pages_elements[pg]]
            _normalize_heading_hierarchy([elem for elem in elements if not elem.get("continuation")])

            figures_tagged = 0
            headings_tagged = 0
            tables_tagged = 0
            lists_tagged = 0
            links_tagged = 0
            annotations_tagged = 0
            decorative_figures_artifacted = 0

            for page_index, page in enumerate(pdf.pages):
                page_elems = pages_elements.get(page_index, [])
                page_glyphs = glyph_reader.page(page_index)
                page_words = _items_in_user_space(
                    _build_docling_parse_page_words(
                        parser_doc, page_index, docling_parse_word_cache
                    ),
                    frames[page_index],
                )
                page_hyperlinks = _items_in_user_space(
                    _build_docling_parse_page_hyperlinks(
                        parser_doc, page_index, docling_parse_hyperlink_cache
                    ),
                    frames[page_index],
                )
                page_widgets = _items_in_user_space(
                    _build_docling_parse_page_widgets(
                        parser_doc, page_index, docling_parse_widget_cache
                    ),
                    frames[page_index],
                )
                page["/Tabs"] = pikepdf.Name("/S")
                page_tagged_elements: list[dict] = []
                first_page_kid = len(builder.doc_elem.get("/K") or [])
                nonsemantic_page = page_index in nonsemantic_pages
                has_words = any(op.glyphs for ops in page_glyphs.ops.values() for op in ops)
                if not nonsemantic_page and (page_elems or has_words):
                    page_word_owners = page_owners[page_index]
                    def rewrite(owner, path=(), initial_ctm=IDENTITY, ancestors=frozenset()):
                        try:
                            instructions = _strip_existing_markers(
                                list(pikepdf.parse_content_stream(owner)), owner.get("/Resources")
                            )
                        except pikepdf.PdfError as exc:
                            logger.warning("Could not parse content on page %s: %s", page_index, exc)
                            return set()
                        forms = _extract_form_invocations(owner, instructions)
                        passthrough = [
                            ContentRegion(
                                kind="passthrough",
                                start_idx=form.start_idx,
                                end_idx=form.end_idx,
                                instructions=form.instructions,
                                xobject_name=form.xobject_name,
                            )
                            for form in forms
                        ]
                        matched = _rewrite_content_stream(
                            pdf, owner, page_index, page_elems,
                            builder, page.obj, alt_lookup,
                            decorative_figures=decorative_figures,
                            initial_ctm=initial_ctm,
                            stream_owner=None if not path else owner,
                            passthrough_regions=passthrough,
                            measured_ops=page_glyphs.ops.get(path),
                            word_owners=page_word_owners,
                        )
                        for form in forms:
                            if form.stream.objgen in ancestors:
                                continue
                            matched.update(rewrite(
                                form.stream,
                                (*path, form.xobject_name.lstrip("/")),
                                _mat_multiply(form.initial_ctm, initial_ctm),
                                ancestors | {form.stream.objgen},
                            ))
                        return matched

                    matched_page_indices = rewrite(page)
                    page_tagged_elements.extend(
                        page_elems[idx] for idx in sorted(matched_page_indices)
                    )

                    seen_page_element_ids: set[int] = set()
                    for elem in page_tagged_elements:
                        elem_key = id(elem)
                        if elem_key in seen_page_element_ids:
                            continue
                        seen_page_element_ids.add(elem_key)
                        t = elem.get("type", "")
                        if t == "figure":
                            if elem.get("figure_index") in decorative_figures:
                                decorative_figures_artifacted += 1
                            else:
                                figures_tagged += 1
                        elif t == "heading":
                            headings_tagged += 1
                        elif t == "table":
                            if layout := elem.get("_layout_table"):
                                lists_tagged += sum(
                                    len(b["groups"]) for b in layout["blocks"] if b["role"] == "L"
                                )
                            else:
                                tables_tagged += 1
                        elif t == "list_item":
                            lists_tagged += 1
                else:
                    if nonsemantic_page:
                        _rewrite_content_stream(
                            pdf,
                            page,
                            page_index,
                            [],
                            builder,
                            page.obj,
                            alt_lookup,
                            decorative_figures=decorative_figures,
                            measured_ops=page_glyphs.ops.get(()),
                        )

                # Widgets participate in page order; links and other OBJRs
                # are attached after ordering. Finalize before adding bookmarks.
                _tag_widget_annotations(page, page.obj, builder, page_widgets, page_tagged_elements)
                builder.order_page_kids(first_page_kid)
                links_tagged += _tag_link_annotations(
                    page,
                    page.obj,
                    builder,
                    page_elems,
                    page_words,
                    page_hyperlinks,
                )
                annotations_tagged += _tag_generic_annotations(page, page.obj, builder)

            # 5. Finalize structure tree
            tables_tagged -= _finish_table_structure(builder, pages_elements)
            headings_tagged -= sum(
                e.get("type") == "heading" and bool(e.get("_table_header_recovered"))
                for e in elements
            )
            builder.finalize()

            # 6. Add bookmarks from headings
            bookmarks_added = _add_bookmarks(
                pdf,
                builder.headings,
                [elem for elem in elements
                 if not elem.get("continuation") and not elem.get("_table_header_recovered")],
                structure_json.get("bookmark_plan"),
                structure_json.get("native_toc"),
                structure_json.get("title"),
            )
            _sanitize_outline_leaf_counts(pdf)

            _save_pdf_for_viewer_compatibility(pdf, output_path)

        total_tags = tags_added + builder.struct_elems_created
        logger.info(
            f"Tagged PDF saved: {output_path.name} "
            f"({total_tags} tags, {headings_tagged} headings, "
            f"{figures_tagged} figures, {decorative_figures_artifacted} decorative figures, "
            f"{tables_tagged} tables, {links_tagged} links, "
            f"{annotations_tagged} annotations, "
            f"{bookmarks_added} bookmarks)"
        )

        return TaggingResult(
            output_path=output_path,
            tags_added=total_tags,
            lang_set=True,
            struct_elems_created=builder.struct_elems_created,
            figures_tagged=figures_tagged,
            headings_tagged=headings_tagged,
            tables_tagged=tables_tagged,
            lists_tagged=lists_tagged,
            links_tagged=links_tagged,
            annotations_tagged=annotations_tagged,
            decorative_figures_artifacted=decorative_figures_artifacted,
            bookmarks_added=bookmarks_added,
            title_set=title_set,
            reading_order_diagnostics=reading_order_diagnostics,
            unowned_words_by_page=unowned_words_by_page,
        )

    def _tag_with_forms():
        with unique_form_instances(input_path) as tagging_input:
            return _tag(tagging_input)

    return await asyncio.to_thread(_tag_with_forms)
