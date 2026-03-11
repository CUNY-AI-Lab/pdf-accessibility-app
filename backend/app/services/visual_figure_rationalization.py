from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pikepdf

from app.pipeline.structure import FigureInfo
from app.pipeline.tagger import _extract_content_regions
from app.services.document_intelligence import build_document_model, collect_nearby_blocks
from app.services.pdf_preview import render_bbox_preview_png_bytes

DOMINANT_IMAGE_AREA_RATIO_MIN = 0.55
DOMINANT_IMAGE_SHARE_MIN = 0.7
SYNTHETIC_FIGURE_TEXT_CHAR_MAX = 260
SYNTHETIC_FIGURE_SHORT_BLOCK_MAX = 42
SYNTHETIC_FIGURE_LONG_BLOCK_MIN = 90
_NON_ALPHA_RE = re.compile(r"[^A-Za-z]+")


def _bbox_area(bbox: dict[str, Any] | None) -> float:
    if not isinstance(bbox, dict):
        return 0.0
    try:
        width = max(0.0, float(bbox.get("r", 0.0)) - float(bbox.get("l", 0.0)))
        height = max(0.0, float(bbox.get("t", 0.0)) - float(bbox.get("b", 0.0)))
    except Exception:
        return 0.0
    return width * height


def _page_area(page: pikepdf.Page) -> float:
    try:
        mediabox = page.mediabox
        width = max(0.0, float(mediabox[2]) - float(mediabox[0]))
        height = max(0.0, float(mediabox[3]) - float(mediabox[1]))
    except Exception:
        return 0.0
    return width * height


def _is_fragmentary_text(text: str) -> bool:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return True
    if len(normalized) <= SYNTHETIC_FIGURE_SHORT_BLOCK_MAX:
        return True
    alpha_only = _NON_ALPHA_RE.sub("", normalized)
    if len(alpha_only) < 12:
        return True
    tokens = normalized.split()
    if len(tokens) <= 8 and sum(char.isdigit() for char in normalized) >= max(len(alpha_only), 1):
        return True
    return False


def _page_text_profile(blocks: list[Any]) -> dict[str, Any]:
    texts = [
        " ".join(str(block.text or "").split())
        for block in blocks
        if str(getattr(block, "role", "") or "") not in {"artifact", "figure"}
        and str(getattr(block, "text", "") or "").strip()
    ]
    if not texts:
        return {
            "block_count": 0,
            "text_chars": 0,
            "short_blocks": 0,
            "long_blocks": 0,
            "fragmentary_blocks": 0,
            "fragmentary_ratio": 0.0,
        }

    short_blocks = sum(1 for text in texts if len(text) <= SYNTHETIC_FIGURE_SHORT_BLOCK_MAX)
    long_blocks = sum(1 for text in texts if len(text) >= SYNTHETIC_FIGURE_LONG_BLOCK_MIN)
    fragmentary_blocks = sum(1 for text in texts if _is_fragmentary_text(text))
    block_count = len(texts)
    return {
        "block_count": block_count,
        "text_chars": sum(len(text) for text in texts),
        "short_blocks": short_blocks,
        "long_blocks": long_blocks,
        "fragmentary_blocks": fragmentary_blocks,
        "fragmentary_ratio": fragmentary_blocks / max(block_count, 1),
    }


def _insert_element_for_page(elements: list[dict[str, Any]], new_element: dict[str, Any]) -> None:
    page_raw = new_element.get("page")
    if not isinstance(page_raw, int):
        elements.append(new_element)
        return

    insert_at = len(elements)
    last_same_page = None
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue
        element_page = element.get("page")
        if not isinstance(element_page, int):
            continue
        if element_page == page_raw:
            last_same_page = index
        if element_page > page_raw and last_same_page is None:
            insert_at = index
            break
    if last_same_page is not None:
        insert_at = last_same_page + 1
    elements.insert(insert_at, new_element)


def _dominant_image_regions(pdf_path: Path) -> dict[int, dict[str, Any]]:
    if not pdf_path.exists():
        return {}

    dominant_by_page: dict[int, dict[str, Any]] = {}
    with pikepdf.open(str(pdf_path)) as pdf:
        for page_index, page in enumerate(pdf.pages):
            page_number = page_index + 1
            try:
                instructions = list(pikepdf.parse_content_stream(page))
            except Exception:
                continue
            regions = _extract_content_regions(instructions, page)
            image_regions = [
                region
                for region in regions
                if region.kind == "image" and isinstance(region.bbox, dict)
            ]
            if not image_regions:
                continue

            page_area = _page_area(page)
            if page_area <= 0.0:
                continue

            region_payloads = [
                {
                    "bbox": dict(region.bbox),
                    "area": _bbox_area(region.bbox),
                    "xobject_name": str(region.xobject_name or ""),
                }
                for region in image_regions
                if _bbox_area(region.bbox) > 0.0
            ]
            if not region_payloads:
                continue
            region_payloads.sort(key=lambda item: float(item["area"]), reverse=True)
            dominant = region_payloads[0]
            total_image_area = sum(float(item["area"]) for item in region_payloads)
            area_ratio = float(dominant["area"]) / page_area
            image_share = float(dominant["area"]) / max(total_image_area, 1.0)
            dominant_by_page[page_number] = {
                "page": page_number,
                "bbox": dominant["bbox"],
                "area_ratio": round(area_ratio, 4),
                "image_share": round(image_share, 4),
                "image_region_count": len(region_payloads),
                "xobject_name": dominant["xobject_name"],
            }
    return dominant_by_page


def _should_synthesize_visual_figure(
    *,
    dominant_image: dict[str, Any],
    existing_figure_count: int,
    field_count: int,
    text_profile: dict[str, Any],
) -> tuple[bool, str]:
    area_ratio = float(dominant_image.get("area_ratio") or 0.0)
    image_share = float(dominant_image.get("image_share") or 0.0)
    block_count = int(text_profile.get("block_count") or 0)
    text_chars = int(text_profile.get("text_chars") or 0)
    long_blocks = int(text_profile.get("long_blocks") or 0)
    fragmentary_ratio = float(text_profile.get("fragmentary_ratio") or 0.0)

    if area_ratio < DOMINANT_IMAGE_AREA_RATIO_MIN or image_share < DOMINANT_IMAGE_SHARE_MIN:
        return False, "image_not_dominant"
    if existing_figure_count > 0:
        return False, "figure_already_present"
    if field_count > 0:
        return False, "page_contains_fields"
    if block_count == 0:
        return True, "dominant_image_without_text"
    if text_chars <= SYNTHETIC_FIGURE_TEXT_CHAR_MAX and long_blocks == 0:
        return True, "dominant_image_with_sparse_text"
    if block_count <= 1 and area_ratio >= 0.72:
        return True, "dominant_image_with_single_text_block"
    if fragmentary_ratio >= 0.6 and long_blocks <= 1:
        return True, "dominant_image_with_fragmentary_text"
    return False, "narrative_text_already_present"


def collect_missing_visual_figure_targets(
    *,
    working_pdf: Path,
    structure_json: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(structure_json, dict):
        return []

    document = build_document_model(structure_json=structure_json, pdf_path=working_pdf)
    dominant_images = _dominant_image_regions(working_pdf)
    ignored_pages = {
        int(page)
        for page in structure_json.get("visual_meaning_ignored_pages", [])
        if isinstance(page, int) and page > 0
    }
    elements = structure_json.get("elements")
    existing_figure_counts: dict[int, int] = {}
    if isinstance(elements, list):
        for element in elements:
            if not isinstance(element, dict) or element.get("type") != "figure":
                continue
            page_raw = element.get("page")
            if not isinstance(page_raw, int) or page_raw < 0:
                continue
            page_number = page_raw + 1
            existing_figure_counts[page_number] = existing_figure_counts.get(page_number, 0) + 1

    targets: list[dict[str, Any]] = []
    page_numbers = sorted(
        {
            *dominant_images.keys(),
            *[page.page_number for page in document.pages],
        }
    )
    for page_number in page_numbers:
        if page_number in ignored_pages:
            continue
        dominant_image = dominant_images.get(page_number)
        if not isinstance(dominant_image, dict):
            continue
        page = document.page(page_number)
        blocks = list(page.blocks) if page is not None else []
        fields = list(page.fields) if page is not None else []
        text_profile = _page_text_profile(blocks)
        should_add, reason = _should_synthesize_visual_figure(
            dominant_image=dominant_image,
            existing_figure_count=int(existing_figure_counts.get(page_number, 0)),
            field_count=len(fields),
            text_profile=text_profile,
        )
        if not should_add:
            continue
        targets.append(
            {
                "page": page_number,
                "bbox": dominant_image["bbox"],
                "area_ratio": dominant_image["area_ratio"],
                "image_share": dominant_image["image_share"],
                "image_region_count": dominant_image["image_region_count"],
                "xobject_name": dominant_image["xobject_name"],
                "reason": reason,
                "text_profile": text_profile,
                "nearby_blocks": collect_nearby_blocks(
                    document,
                    page_number=page_number,
                    bbox=dominant_image["bbox"],
                    limit=6,
                ),
            }
        )
    return targets


def synthesize_missing_visual_figures(
    *,
    working_pdf: Path,
    structure_json: dict[str, Any],
    figures: list[FigureInfo],
    figures_dir: Path,
) -> tuple[dict[str, Any], list[FigureInfo], dict[str, Any]]:
    audit: dict[str, Any] = {
        "attempted": False,
        "applied": False,
        "reason": "",
        "candidate_count": 0,
        "applied_count": 0,
        "pages": [],
        "figure_indexes": [],
    }
    targets = collect_missing_visual_figure_targets(
        working_pdf=working_pdf,
        structure_json=structure_json,
    )
    if not targets:
        audit["reason"] = "no_candidates"
        return structure_json, figures, audit

    audit["attempted"] = True
    audit["candidate_count"] = len(targets)
    figures_dir.mkdir(parents=True, exist_ok=True)

    updated_structure = copy.deepcopy(structure_json)
    elements = updated_structure.get("elements")
    if not isinstance(elements, list):
        audit["reason"] = "missing_elements"
        return structure_json, figures, audit

    updated_figures = list(figures)
    next_figure_index = max((figure.index for figure in updated_figures), default=-1) + 1
    applied_pages: list[int] = []
    applied_indexes: list[int] = []

    for ordinal, target in enumerate(targets, start=1):
        page_number = int(target.get("page") or 0)
        bbox = target.get("bbox")
        if page_number <= 0 or not isinstance(bbox, dict):
            continue
        figure_path = figures_dir / f"synthetic_figure_p{page_number:03d}_{ordinal:02d}.png"
        try:
            figure_bytes = render_bbox_preview_png_bytes(
                working_pdf,
                page_number,
                bbox,
                highlight=False,
                crop_margin_points=12.0,
            )
        except Exception:
            continue
        figure_path.write_bytes(figure_bytes)

        figure = FigureInfo(
            index=next_figure_index,
            path=figure_path,
            caption=None,
            page=page_number - 1,
            bbox=dict(bbox),
        )
        updated_figures.append(figure)
        _insert_element_for_page(
            elements,
            {
                "type": "figure",
                "page": page_number - 1,
                "bbox": dict(bbox),
                "figure_index": next_figure_index,
                "caption": None,
                "review_id": f"synthetic-figure-{page_number}-{next_figure_index}",
                "synthetic_figure": True,
                "synthetic_source": "dominant_page_image",
                "synthetic_reason": str(target.get("reason") or "").strip(),
            },
        )
        applied_pages.append(page_number)
        applied_indexes.append(next_figure_index)
        next_figure_index += 1

    if not applied_indexes:
        audit["reason"] = "render_failed"
        return structure_json, figures, audit

    updated_structure["figures_count"] = len(updated_figures)
    audit["applied"] = True
    audit["applied_count"] = len(applied_indexes)
    audit["pages"] = sorted(set(applied_pages))
    audit["figure_indexes"] = applied_indexes
    audit["reason"] = "applied"
    return updated_structure, updated_figures, audit
