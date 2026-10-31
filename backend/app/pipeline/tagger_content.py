"""Prepare and rewrite marked content, including reading-order votes and scan overlays."""

import dataclasses
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

import pikepdf

from app.pipeline.page_frame import PageFrame
from app.pipeline.page_glyphs import MeasuredTextOp
from app.pipeline.reading_order import choose_order
from app.pipeline.tagger_content_geometry import (
    ContentRegion,
    TextShowRun,
    _apply_measured_geometry,
    _extract_content_regions,
    _extract_form_invocations,
    _extract_text_show_runs,
    _measured_by_instruction,
    _page_content_bbox,
    _split_text_ops_across_targets,
    _strip_existing_markers,
    _text_runs_look_like_hidden_ocr_layer,
)
from app.pipeline.tagger_geometry import (
    CONTENT_PAINT_OPERATORS,
    IDENTITY,
    LINK_TEXT_ELEMENT_TYPES,
    PATH_CONSTRUCTION_OPERATORS,
    PATH_PAINTING_OPERATORS,
    TEXT_SHOW_OPERATORS,
    ContentTarget,
    _as_positive_int,
    _bbox_area,
    _bbox_intersection,
    _bbox_intersection_box,
    _containment_ratio,
    _mat_multiply,
    _region_bbox,
)
from app.pipeline.tagger_ownership import (
    _content_targets,
    _owner_of,
    _owner_of_glyphs,
    _upright,
    _word_owners,
)
from app.pipeline.tagger_semantics import (
    _element_accessible_text,
    _element_actual_text,
    _formula_alt_text,
    _is_weak_figure_alt_text,
    is_placeholder_alt_text,
)
from app.pipeline.tagger_structure import StructTreeBuilder

logger = logging.getLogger(__name__)


def _instruction_paints_content(instr: Any) -> bool:
    op = str(instr.operator) if hasattr(instr, "operator") else ""
    return op in CONTENT_PAINT_OPERATORS


def _artifact_properties_for_element(elem: dict | None) -> pikepdf.Dictionary | None:
    if not isinstance(elem, dict):
        return None

    artifact_type = str(elem.get("artifact_type") or "").strip().lower()
    properties = pikepdf.Dictionary()
    if artifact_type == "page_header":
        properties["/Type"] = pikepdf.Name("/Pagination")
        properties["/Subtype"] = pikepdf.Name("/Header")
        properties["/Attached"] = pikepdf.Array([pikepdf.Name("/Top")])
    elif artifact_type == "page_footer":
        properties["/Type"] = pikepdf.Name("/Pagination")
        properties["/Subtype"] = pikepdf.Name("/Footer")
        properties["/Attached"] = pikepdf.Array([pikepdf.Name("/Bottom")])
    elif artifact_type in {"layout", "decorative", "decoration"}:
        properties["/Type"] = pikepdf.Name("/Layout")
    elif artifact_type == "background":
        properties["/Type"] = pikepdf.Name("/Background")
    elif artifact_type == "page":
        properties["/Type"] = pikepdf.Name("/Page")

    bbox = elem.get("bbox")
    if isinstance(bbox, dict):
        try:
            rect = [
                float(bbox["l"]),
                float(bbox["b"]),
                float(bbox["r"]),
                float(bbox["t"]),
            ]
        except (KeyError, TypeError, ValueError):
            rect = []
        if len(rect) == 4 and rect[2] > rect[0] and rect[3] > rect[1]:
            properties["/BBox"] = pikepdf.Array(rect)

    return properties if len(properties) else None


def _fragment_tag_for_element(elem: dict) -> str | None:
    elem_type = elem.get("type", "")
    if elem_type == "heading":
        level = _as_positive_int(elem.get("level", 1), default=1)
        return f"H{max(1, min(6, level))}"
    if elem_type == "figure":
        return "Figure"
    if elem_type == "list_item":
        return "LBody"
    if elem_type == "code":
        return "Code"
    if elem_type == "formula":
        return "Formula"
    if elem_type == "note":
        return "Note"
    if elem_type == "reference":
        return "Reference"
    if elem_type == "bib_entry":
        return "BibEntry"
    if elem_type == "caption":
        return "Caption"
    if elem_type == "toc_caption":
        return "Caption"
    if elem_type in {"toc_item", "toc_item_table"}:
        return "TOCI"
    if elem_type == "paragraph":
        return "P"
    return None


def _allocate_fragment_mcid(
    builder: StructTreeBuilder,
    elem: dict,
    page_index: int,
    page_ref: pikepdf.Object,
    alt_lookup: dict[int, str],
    decorative_figures: set[int],
    *,
    stream_owner: pikepdf.Object | None = None,
) -> tuple[str, int, str | None] | None:
    """Allocate an MCID for one fragmented content run."""
    tag = _fragment_tag_for_element(elem)
    if tag is None:
        return None

    existing_elem = builder.source_element_struct(elem)
    if existing_elem is not None:
        mcid = builder.add_mcr_to_struct_elem(
            existing_elem,
            page_index,
            page_ref,
            stream_owner=stream_owner,
        )
        return tag, mcid, None

    elem_type = elem.get("type", "")
    elem_lang = elem.get("lang")
    # A formula's glyphs are often a math font's, which a screen reader
    # cannot read, so its text is always its /ActualText.
    actual_text = (
        _element_accessible_text(elem) if elem_type == "formula" else _element_actual_text(elem)
    ) or None

    if elem_type == "heading":
        mcid = builder.add_heading(
            _as_positive_int(elem.get("level", 1), default=1),
            page_index,
            page_ref,
            _element_accessible_text(elem),
            lang=elem_lang,
            stream_owner=stream_owner,
        )
    elif elem_type == "figure":
        fig_idx = elem.get("figure_index")
        if fig_idx in decorative_figures:
            return None
        alt = next(
            (
                candidate.strip()
                for candidate in (alt_lookup.get(fig_idx), elem.get("caption"), elem.get("text"))
                if isinstance(candidate, str) and not is_placeholder_alt_text(candidate)
            ),
            None,
        )
        mcid = builder.add_figure(
            page_index,
            page_ref,
            alt_text=alt,
            stream_owner=stream_owner,
        )
        actual_text = None
    elif elem_type == "list_item":
        mcid = builder.add_list_item(
            page_index,
            page_ref,
            elem.get("list_group_ref"),
            parent_list_group_ref=elem.get("parent_list_group_ref"),
            marker=elem.get("marker"),
            enumerated=bool(elem.get("enumerated", False)),
            stream_owner=stream_owner,
        )
    elif elem_type == "code":
        mcid = builder.add_code(
            page_index,
            page_ref,
            lang=elem_lang,
            stream_owner=stream_owner,
        )
    elif elem_type == "formula":
        formula_alt = _formula_alt_text(actual_text or _element_accessible_text(elem))
        mcid = builder.add_formula(
            page_index,
            page_ref,
            alt_text=formula_alt or None,
            stream_owner=stream_owner,
        )
    elif elem_type == "note":
        mcid = builder.add_note(
            page_index,
            page_ref,
            stream_owner=stream_owner,
        )
    elif elem_type == "reference":
        mcid = builder.add_reference(
            page_index,
            page_ref,
            stream_owner=stream_owner,
        )
    elif elem_type == "bib_entry":
        mcid = builder.add_bib_entry(
            page_index,
            page_ref,
            stream_owner=stream_owner,
        )
    elif elem_type == "caption":
        mcid = builder.add_caption(
            page_index,
            page_ref,
            stream_owner=stream_owner,
        )
    elif elem_type == "toc_caption":
        mcid = builder.add_toc_caption(
            page_index,
            page_ref,
            elem.get("toc_group_ref"),
            stream_owner=stream_owner,
        )
    elif elem_type in {"toc_item", "toc_item_table"}:
        mcid = builder.add_toc_item(
            page_index,
            page_ref,
            elem.get("toc_group_ref"),
            stream_owner=stream_owner,
        )
    else:
        mcid = builder.add_paragraph(
            page_index,
            page_ref,
            lang=elem_lang,
            stream_owner=stream_owner,
        )

    builder.remember_source_element(elem, stream_owner or page_ref, mcid)
    if actual_text and elem_type != "formula":
        # An element's text covers all of its fragments. On the first
        # fragment's marked content it would be read, and then the later
        # fragments' own glyphs would be read again.
        struct_elem = builder.source_element_struct(elem)
        if struct_elem is not None:
            struct_elem["/ActualText"] = pikepdf.String(actual_text)
            actual_text = None
    return tag, mcid, actual_text


def _is_full_page_scan_image_region(
    region: ContentRegion,
    content_owner,
    elements: list[dict],
) -> bool:
    if region.kind != "image" or not region.bbox:
        return False
    if not any(
        elem.get("type") in LINK_TEXT_ELEMENT_TYPES and isinstance(elem.get("bbox"), dict)
        for elem in elements
    ):
        return False
    content_bbox = _page_content_bbox(content_owner)
    if not content_bbox:
        return False
    return (
        _containment_ratio(region.bbox, content_bbox) >= 0.85
        and _containment_ratio(content_bbox, region.bbox) >= 0.85
    )


def _resolved_figure_alt_for_tagging(elem: dict, alt_lookup: dict[int, str]) -> str:
    fig_idx = elem.get("figure_index")
    alt = (
        alt_lookup.get(fig_idx)
        or elem.get("caption")
        or elem.get("text")
    )
    if not isinstance(alt, str):
        return ""
    alt = " ".join(alt.split()).strip()
    if _is_weak_figure_alt_text(alt):
        return ""
    return alt


def _scan_figure_overlay_targets(
    regions: list[ContentRegion],
    elements: list[dict],
    content_owner,
    alt_lookup: dict[int, str],
    decorative_figures: set[int],
) -> tuple[list[tuple[ContentRegion, int]], set[int]]:
    """Find figure elements that can be represented by clipped full-page scan pixels."""
    overlay_targets: list[tuple[ContentRegion, int]] = []
    decorative_covered: set[int] = set()
    figure_elements = [
        (elem_idx, elem)
        for elem_idx, elem in enumerate(elements)
        if elem.get("type") == "figure" and isinstance(elem.get("bbox"), dict)
    ]
    if not figure_elements:
        return overlay_targets, decorative_covered

    full_page_images = [
        region
        for region in regions
        if _is_full_page_scan_image_region(region, content_owner, elements)
    ]
    if not full_page_images:
        return overlay_targets, decorative_covered

    for elem_idx, elem in figure_elements:
        fig_idx = elem.get("figure_index")
        elem_bbox = elem.get("bbox")
        if not isinstance(elem_bbox, dict):
            continue

        best_region: ContentRegion | None = None
        best_containment = 0.0
        for region in full_page_images:
            containment = _containment_ratio(elem_bbox, region.bbox)
            if containment > best_containment:
                best_containment = containment
                best_region = region
        if best_region is None or best_containment < 0.95:
            continue

        if fig_idx in decorative_figures:
            decorative_covered.add(elem_idx)
            continue

        if not _resolved_figure_alt_for_tagging(elem, alt_lookup):
            continue
        overlay_targets.append((best_region, elem_idx))

    return overlay_targets, decorative_covered


def _make_clipped_image_figure_instructions(
    region: ContentRegion,
    elem: dict,
    builder: StructTreeBuilder,
    page_index: int,
    page_ref: pikepdf.Object,
    alt_lookup: dict[int, str],
    decorative_figures: set[int],
    *,
    stream_owner: pikepdf.Object | None = None,
) -> list[pikepdf.ContentStreamInstruction]:
    """Reuse an existing scan image with a clip path for one real figure region."""
    elem_bbox = elem.get("bbox")
    clip_bbox = _bbox_intersection_box(elem_bbox, region.bbox if isinstance(elem_bbox, dict) else None)
    if not clip_bbox:
        return []
    if clip_bbox["r"] - clip_bbox["l"] <= 1.0 or clip_bbox["t"] - clip_bbox["b"] <= 1.0:
        return []
    if not _resolved_figure_alt_for_tagging(elem, alt_lookup):
        return []

    allocated = _allocate_fragment_mcid(
        builder,
        elem,
        page_index,
        page_ref,
        alt_lookup,
        decorative_figures,
        stream_owner=stream_owner,
    )
    if allocated is None:
        return []
    tag, mcid, _actual_text = allocated
    if tag != "Figure":
        return []

    return [
        _make_bdc("Figure", mcid),
        pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")),
        pikepdf.ContentStreamInstruction(
            [
                clip_bbox["l"],
                clip_bbox["b"],
                clip_bbox["r"] - clip_bbox["l"],
                clip_bbox["t"] - clip_bbox["b"],
            ],
            pikepdf.Operator("re"),
        ),
        pikepdf.ContentStreamInstruction([], pikepdf.Operator("W")),
        pikepdf.ContentStreamInstruction([], pikepdf.Operator("n")),
        pikepdf.ContentStreamInstruction(list(region.ctm), pikepdf.Operator("cm")),
        pikepdf.ContentStreamInstruction(
            [pikepdf.Name(region.xobject_name)],
            pikepdf.Operator("Do"),
        ),
        pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")),
        _make_emc(),
    ]


def _make_bdc(
    struct_type: str,
    mcid: int,
    *,
    actual_text: str | None = None,
) -> pikepdf.ContentStreamInstruction:
    attributes = pikepdf.Dictionary({"/MCID": mcid})
    normalized_actual_text = str(actual_text or "").strip()
    if normalized_actual_text:
        attributes["/ActualText"] = pikepdf.String(normalized_actual_text)
    return pikepdf.ContentStreamInstruction(
        [pikepdf.Name(f"/{struct_type}"), attributes],
        pikepdf.Operator("BDC"),
    )


def _make_emc() -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC"))


def _figure_of(
    region: ContentRegion,
    elements: list[dict],
    content_owner,
) -> int | None:
    """The figure an image belongs to: the one whose box and the image's
    overlap by at least half of the smaller of the two (an image cropped to
    the figure, or a figure made of several images). A full-page scan
    behind a page's text belongs to none."""
    box = _region_bbox(region)
    if not box or _is_full_page_scan_image_region(region, content_owner, elements):
        return None
    best: int | None = None
    best_share = 0.5
    for idx, elem in sorted(enumerate(elements), key=lambda pair: pair[1].get("_docling_order", pair[0])):
        figure_box = elem.get("bbox")
        if elem.get("type") != "figure" or not isinstance(figure_box, dict):
            continue
        smaller = min(_bbox_area(box), _bbox_area(figure_box))
        share = _bbox_intersection(box, figure_box) / smaller if smaller > 0 else 0.0
        if share >= best_share:
            best, best_share = idx, share
    return best


_ARTIFACT_MARK: Literal["artifact"] = "artifact"
_PASSTHROUGH_MARK: Literal["passthrough"] = "passthrough"


@dataclass
class PreparedContent:
    instructions: list
    regions: list[ContentRegion]
    text_runs: list[TextShowRun]
    marks: dict[int, ContentTarget | int | Literal["artifact", "passthrough"]]
    passthrough: list[ContentRegion]


def _prepare_content_stream(
    content_owner,
    page_index: int,
    elements: list[dict],
    *,
    initial_ctm: tuple[float, float, float, float, float, float] = IDENTITY,
    passthrough_regions: list[ContentRegion] | None = None,
    measured_ops: list[MeasuredTextOp] | None = None,
    word_owners: dict[tuple, ContentTarget | None] | None = None,
) -> PreparedContent | None:
    """Assign content to elements without writing tags. Used both for the
    drawing-order vote and the rewrite, including split strings and cells."""
    try:
        instructions = list(pikepdf.parse_content_stream(content_owner))
    except Exception as e:
        logger.warning(f"Could not parse content stream for page {page_index}: {e}")
        return None

    instructions = _strip_existing_markers(instructions, content_owner.get("/Resources"))
    if not instructions:
        return None

    passthrough_regions = passthrough_regions or []
    targets = _content_targets(elements)
    measured = _measured_by_instruction(instructions, measured_ops)
    if measured is not None:
        if word_owners is None:
            word_owners = _word_owners(measured, targets)
        instructions, measured, new_index = _split_text_ops_across_targets(
            instructions, measured, word_owners
        )
        passthrough_regions = [
            dataclasses.replace(
                region, start_idx=new_index[region.start_idx], end_idx=new_index[region.end_idx]
            )
            for region in passthrough_regions
        ]
    word_owners = word_owners or {}
    regions = _extract_content_regions(instructions, content_owner, initial_ctm=initial_ctm)
    text_runs = _extract_text_show_runs(instructions, initial_ctm=initial_ctm)
    if measured is not None:
        _apply_measured_geometry(regions, text_runs, measured)

    mark_by_idx: dict[int, ContentTarget | int | Literal["artifact", "passthrough"]] = {}
    for run in text_runs:
        op = measured.get(run.instruction_idx) if measured is not None else None
        owner = (
            _owner_of_glyphs(op.glyphs, word_owners)
            if op is not None and op.glyphs
            else _owner_of(run.bbox, targets)
        )
        for idx in range(run.segment_start_idx, run.segment_end_idx):
            mark_by_idx[idx] = owner if owner is not None else _ARTIFACT_MARK
    for region in regions:
        if region.kind == "image":
            figure = _figure_of(region, elements, content_owner)
            for idx in range(region.start_idx, region.end_idx):
                mark_by_idx[idx] = figure if figure is not None else _ARTIFACT_MARK
    for region in passthrough_regions:
        for idx in range(region.start_idx, region.end_idx):
            mark_by_idx[idx] = _PASSTHROUGH_MARK

    # Keep each source replacement inside one new MCID, with its closing
    # EMC inside that same sequence. Otherwise a new fragment boundary can
    # close the source Span early or leave its replacement outside the tree.
    replacements: list[int] = []
    for index, instr in enumerate(instructions):
        op = str(instr.operator)
        if op == "BDC":
            replacements.append(index)
        elif op == "EMC" and replacements:
            start = replacements.pop()
            marks = Counter(
                mark_by_idx[j] for j in range(start + 1, index)
                if str(instructions[j].operator) in TEXT_SHOW_OPERATORS and j in mark_by_idx
            )
            if marks:
                mark = marks.most_common(1)[0][0]
                for j in range(start, index + 1):
                    mark_by_idx[j] = mark

    return PreparedContent(instructions, regions, text_runs, mark_by_idx, passthrough_regions)


def _remap_word_owners(owners: dict, before: list[dict], after: list[dict]) -> None:
    indices = {id(elem): index for index, elem in enumerate(after)}
    owners.update({
        key: dataclasses.replace(owner, elem_idx=indices[id(before[owner.elem_idx])])
        for key, owner in owners.items() if owner is not None
    })


def _page_reading_order(
    page: pikepdf.Page, page_index: int, elements: list[dict],
    page_ops: dict[tuple[str, ...], list[MeasuredTextOp]], frame: PageFrame,
    decorative_figures: set[int],
    *, word_owners: dict[tuple, ContentTarget | None] | None = None,
) -> dict:
    """Vote before building any structure or normalizing heading levels.

    Forms are placed at their page invocation, not after page content. The
    preparation pass is discarded per page to bound memory; the rewrite
    repeats the same ownership calculation with the chosen element order.
    """
    first: dict[int, tuple[int, ...]] = {}

    def visit(owner, path=(), prefix=(), initial_ctm=IDENTITY, ancestors=frozenset()):
        try:
            instructions = _strip_existing_markers(
                list(pikepdf.parse_content_stream(owner)), owner.get("/Resources")
            )
        except pikepdf.PdfError:
            return
        forms = _extract_form_invocations(owner, instructions)
        passthrough = [
            ContentRegion(
                kind="passthrough", start_idx=call.start_idx, end_idx=call.end_idx,
                instructions=call.instructions, xobject_name=call.xobject_name,
            ) for call in forms
        ]
        prepared = _prepare_content_stream(
            owner, page_index, elements, initial_ctm=initial_ctm,
            passthrough_regions=passthrough, measured_ops=page_ops.get(path),
            word_owners=word_owners,
        )
        if prepared is None:
            return
        for index, instruction in enumerate(prepared.instructions):
            if not _instruction_paints_content(instruction):
                continue
            mark = prepared.marks.get(index)
            if isinstance(mark, ContentTarget):
                element_index = mark.elem_idx
            elif isinstance(mark, int):
                element_index = mark
            else:
                continue
            key = id(elements[element_index])
            position = (*prefix, index, 0)
            first[key] = min(first.get(key, position), position)
        for call, region in zip(forms, prepared.passthrough, strict=True):
            if call.stream.objgen in ancestors:
                continue
            visit(
                call.stream, (*path, call.xobject_name.lstrip("/")),
                (*prefix, region.start_idx), _mat_multiply(call.initial_ctm, initial_ctm),
                ancestors | {call.stream.objgen},
            )

    visit(page)

    semantic = [
        elem for elem in elements
        if elem.get("type") != "artifact"
        and not (elem.get("type") == "figure" and elem.get("figure_index") in decorative_figures)
    ]
    boxes = [
        _upright(elem["bbox"], frame.rotation // 90) if isinstance(elem.get("bbox"), dict) else None
        for elem in semantic
    ]
    chosen, diagnostics = choose_order(
        boxes, {i: first[id(elem)] for i, elem in enumerate(semantic) if id(elem) in first},
    )
    # Artifacts keep their slots. All existing consumers receive the chosen
    # sequence through the same list and reading_order keys as before.
    ordered = iter(semantic[i] for i in chosen)
    semantic_ids = {id(elem) for elem in semantic}
    before = list(elements)
    elements[:] = [next(ordered) if id(elem) in semantic_ids else elem for elem in elements]
    if word_owners is not None:
        _remap_word_owners(word_owners, before, elements)
    for order, elem in enumerate(elements):
        elem["reading_order"] = order
    return {"page": page_index, **diagnostics}


def _rewrite_content_stream(
    pdf: pikepdf.Pdf,
    content_owner,
    page_index: int,
    elements: list[dict],
    builder: StructTreeBuilder,
    page_ref: pikepdf.Object,
    alt_lookup: dict[int, str],
    decorative_figures: set[int] | None = None,
    *,
    initial_ctm: tuple[float, float, float, float, float, float] = IDENTITY,
    stream_owner: pikepdf.Object | None = None,
    passthrough_regions: list[ContentRegion] | None = None,
    measured_ops: list[MeasuredTextOp] | None = None,
    word_owners: dict[tuple, ContentTarget | None] | None = None,
) -> set[int]:
    """Mark a content stream's content for the elements it belongs to.

    Each text-showing unit (a TJ string, or a whole Tj) belongs to the text
    element or table cell holding at least half of it (see _owner_of), TJs
    being split where the owner changes; each image belongs to the figure it
    overlaps (see _figure_of). Consecutive content with one owner is one
    marked-content sequence. Everything else that paints is an artifact, a
    path object wrapped whole; OCR form calls pass through unmarked, to be
    tagged inside the form. Returns the indices of the elements tagged."""
    prepared = _prepare_content_stream(
        content_owner, page_index, elements, initial_ctm=initial_ctm,
        passthrough_regions=passthrough_regions, measured_ops=measured_ops,
        word_owners=word_owners,
    )
    if prepared is None:
        return set()
    instructions, regions, text_runs = prepared.instructions, prepared.regions, prepared.text_runs
    mark_by_idx = prepared.marks
    decorative_figures = decorative_figures or set()

    overlay_targets: list[tuple[ContentRegion, int]] = []
    decorative_covered: set[int] = set()
    if stream_owner is None and _text_runs_look_like_hidden_ocr_layer(text_runs):
        overlay_targets, decorative_covered = _scan_figure_overlay_targets(
            regions,
            elements,
            content_owner,
            alt_lookup,
            decorative_figures,
        )
    tagged: set[int] = set(decorative_covered)

    new_instructions: list = []
    active: ContentTarget | int | Literal["artifact"] | None = None
    pending_path: list = []

    def close_active() -> None:
        nonlocal active
        if active is not None:
            new_instructions.append(_make_emc())
            active = None

    def open_mark(mark: ContentTarget | int | Literal["artifact"]) -> None:
        """Open the marked content for a mark, or an artifact if no
        structure element can hold it."""
        nonlocal active
        if mark == _ARTIFACT_MARK:
            new_instructions.append(_make_bmc_artifact())
            active = mark
            return
        elem_idx = mark if isinstance(mark, int) else mark.elem_idx
        elem = elements[elem_idx]
        with builder.at_reading_order(elem.get("reading_order")):
            if elem.get("type") == "artifact":
                new_instructions.append(_make_bmc_artifact(elem))
                active = mark
                return
            allocated: tuple[str, int, str | None] | None = None
            if isinstance(mark, ContentTarget) and mark.cell is not None:
                cell = builder.add_table_cell_fragment(
                    page_index, page_ref, elem, mark.cell, stream_owner=stream_owner
                )
                allocated = (*cell, None) if cell is not None else None
            else:
                allocated = _allocate_fragment_mcid(
                    builder,
                    elem,
                    page_index,
                    page_ref,
                    alt_lookup,
                    decorative_figures,
                    stream_owner=stream_owner,
                )
            if allocated is None:
                new_instructions.append(_make_bmc_artifact())
                active = _ARTIFACT_MARK
                return
            tag, mcid, actual_text = allocated
            if isinstance(mark, ContentTarget) and mark.list_label:
                body = builder.source_element_struct(elem)
                if body is not None:
                    builder.move_fragment_to_list_label(body, stream_owner or page_ref, mcid)
                    tag = "Lbl"
            if isinstance(mark, ContentTarget) and mark.annotation is not None:
                builder.put_mcid_in_link(stream_owner or page_ref, mcid, page_ref, mark.annotation)
                tag, actual_text = "Link", None
            new_instructions.append(_make_bdc(tag, mcid, actual_text=actual_text))
            tagged.add(elem_idx)
            active = mark

    def flush_path(painted: bool) -> None:
        if painted:
            new_instructions.append(_make_bmc_artifact())
            new_instructions.extend(pending_path)
            new_instructions.append(_make_emc())
        else:
            new_instructions.extend(pending_path)
        pending_path.clear()

    for idx, instr in enumerate(instructions):
        mark = mark_by_idx.get(idx)
        if mark != active:
            close_active()
            if mark is not None and mark != _PASSTHROUGH_MARK:
                open_mark(mark)
        if mark is not None:
            new_instructions.append(instr)
            continue
        op = str(instr.operator)
        if op in PATH_CONSTRUCTION_OPERATORS or (pending_path and op in PATH_PAINTING_OPERATORS):
            pending_path.append(instr)
            if op in PATH_PAINTING_OPERATORS:
                flush_path(painted=op != "n")
            continue
        if pending_path:
            flush_path(painted=False)
        if _instruction_paints_content(instr):
            new_instructions.extend([_make_bmc_artifact(), instr, _make_emc()])
        else:
            new_instructions.append(instr)

    if pending_path:
        flush_path(painted=False)
    close_active()
    for source_region, elem_idx in overlay_targets:
        with builder.at_reading_order(elements[elem_idx].get("reading_order")):
            overlay = _make_clipped_image_figure_instructions(
                source_region,
                elements[elem_idx],
                builder,
                page_index,
                page_ref,
                alt_lookup,
                decorative_figures,
                stream_owner=stream_owner,
            )
        if overlay:
            new_instructions.extend(overlay)
            tagged.add(elem_idx)

    try:
        new_stream = pikepdf.unparse_content_stream(new_instructions)
        if isinstance(content_owner, pikepdf.Page):
            content_owner["/Contents"] = pdf.make_stream(new_stream)
        else:
            content_owner.write(new_stream)
    except Exception as e:
        logger.error(f"Failed to write content stream for page {page_index}: {e}")
        return set()

    return tagged


def _make_bmc_artifact(
    elem: dict | None = None,
) -> pikepdf.ContentStreamInstruction:
    properties = _artifact_properties_for_element(elem)
    if properties:
        return pikepdf.ContentStreamInstruction(
            [pikepdf.Name("/Artifact"), properties],
            pikepdf.Operator("BDC"),
        )
    return pikepdf.ContentStreamInstruction(
        [pikepdf.Name("/Artifact")], pikepdf.Operator("BMC"),
    )
