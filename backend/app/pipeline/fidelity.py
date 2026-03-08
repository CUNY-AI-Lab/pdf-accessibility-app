"""Deterministic post-remediation fidelity checks."""

from __future__ import annotations

import logging
import re
from bisect import bisect_right
from contextlib import contextmanager
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from typing import Any

import pikepdf
from PIL import Image
from pdfminer.high_level import extract_text

from app.services.pdf_operator_context import extract_operator_text_context
from app.services.pdf_context import parse_verapdf_context_path
from app.services.pdf_preview import render_page_png_bytes

FONT_RULE_FRAGMENT = "-7.21."

# Link text quality patterns (1A)
_POOR_LINK_TEXT_EXACT = frozenset({
    "click here", "here", "link", "read more", "learn more", "more",
    "details", "more info", "more information", "this link", "go",
    "click", "click this", "this", "page", "website", "site",
})
_POOR_LINK_TEXT_RE = re.compile(
    r"^https?://\S+$"      # bare URL
    r"|^.{0,1}$"            # single character or empty
    r"|^[\d\s]+$"           # only digits/whitespace
    r"|^link\s*to\s+https?://",  # "Link to http://..." (our inferred fallback)
    re.IGNORECASE,
)

TEXT_SAMPLE_MAX_PAGES = 10
TEXT_SAMPLE_MAX_CHARS = 20000
TEXT_SAMPLE_MIN_CHARS = 300
SCANNED_TEXT_MIN_CHARS = 40
OCR_VISUAL_SAMPLE_MAX_PAGES = 3
OCR_VISUAL_NONWHITE_THRESHOLD = 245
OCR_VISUAL_MIN_INK_RATIO = 0.001
STRUCTURE_FRAGMENT_LIMIT = 24
STRUCTURE_FRAGMENT_MIN_LEN = 18
NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
TABLE_KEYWORDS = ("table", "thead", "tbody", "tfoot", "tr", "th", "td")
FONT_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
USED_GLYPH_FONT_RE = re.compile(r"usedGlyphs\[\d+\]\(([^ )]+)")
PDFMINER_FONTBBOX_WARNING = "Could not get FontBBox from font descriptor because"
TABLE_REVIEW_TARGET_LIMIT = 6


class _PdfMinerFontBBoxFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return PDFMINER_FONTBBOX_WARNING not in record.getMessage()


@contextmanager
def _suppress_pdfminer_fontbbox_warning():
    logger = logging.getLogger("pdfminer.pdffont")
    warning_filter = _PdfMinerFontBBoxFilter()
    logger.addFilter(warning_filter)
    try:
        yield
    finally:
        logger.removeFilter(warning_filter)


def _normalize_text(value: str | None) -> str:
    raw = str(value or "").lower()
    return " ".join(NORMALIZE_RE.sub(" ", raw).split())


def _normalize_dense_text(value: str | None) -> str:
    return _normalize_text(value).replace(" ", "")


def _strip_subset_prefix(value: str | None) -> str:
    raw = str(value or "").strip().lstrip("/")
    if FONT_SUBSET_RE.match(raw):
        return raw.split("+", 1)[1]
    return raw


def _extract_pdf_text_sample(path: Path) -> str:
    try:
        with _suppress_pdfminer_fontbbox_warning():
            text = extract_text(
                str(path),
                page_numbers=list(range(TEXT_SAMPLE_MAX_PAGES)),
            )
    except Exception:
        return ""
    return _normalize_text(text)[:TEXT_SAMPLE_MAX_CHARS]


def _meaningful_structure_element_count(structure_json: dict[str, Any]) -> int:
    elements = structure_json.get("elements", [])
    if not isinstance(elements, list):
        return 0

    count = 0
    for element in elements:
        if not isinstance(element, dict):
            continue
        if str(element.get("type") or "") == "artifact":
            continue
        text = _normalize_text(element.get("text"))
        if text or str(element.get("type") or "") in {"figure", "table"}:
            count += 1
    return count


def _sample_visual_ink(path: Path) -> dict[str, Any]:
    try:
        with pikepdf.Pdf.open(path) as pdf:
            page_total = len(pdf.pages)
    except Exception:
        return {
            "sampled_pages": 0,
            "pages_with_visible_ink": 0,
            "mean_ink_ratio": 0.0,
            "max_ink_ratio": 0.0,
            "visually_blank": None,
        }

    ratios: list[float] = []
    for page_number in range(1, min(page_total, OCR_VISUAL_SAMPLE_MAX_PAGES) + 1):
        try:
            page_png = render_page_png_bytes(path, page_number, dpi=72, max_width=900)
        except Exception:
            continue
        with Image.open(BytesIO(page_png)) as image:
            grayscale = image.convert("L")
            histogram = grayscale.histogram()
        total_pixels = sum(histogram)
        if total_pixels <= 0:
            ratios.append(0.0)
            continue
        nonwhite_pixels = sum(histogram[:OCR_VISUAL_NONWHITE_THRESHOLD])
        ratios.append(nonwhite_pixels / total_pixels)

    pages_with_visible_ink = sum(
        1 for ratio in ratios if ratio >= OCR_VISUAL_MIN_INK_RATIO
    )
    mean_ratio = (sum(ratios) / len(ratios)) if ratios else 0.0
    max_ratio = max(ratios, default=0.0)
    return {
        "sampled_pages": len(ratios),
        "pages_with_visible_ink": pages_with_visible_ink,
        "mean_ink_ratio": round(mean_ratio, 4),
        "max_ink_ratio": round(max_ratio, 4),
        "visually_blank": bool(ratios) and pages_with_visible_ink == 0,
    }


def _collect_structural_fragments(structure_json: dict[str, Any]) -> list[str]:
    elements = structure_json.get("elements", [])
    if not isinstance(elements, list):
        return []

    fragments: list[str] = []
    seen: set[str] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        elem_type = str(element.get("type") or "")
        if elem_type == "artifact" or elem_type.startswith("toc_"):
            continue
        text = _normalize_text(element.get("text"))
        if len(text) < STRUCTURE_FRAGMENT_MIN_LEN:
            continue
        fragment = text[:120]
        if fragment in seen:
            continue
        seen.add(fragment)
        fragments.append(fragment)
        if len(fragments) >= STRUCTURE_FRAGMENT_LIMIT:
            break
    return fragments


def _table_review_targets(structure_json: dict[str, Any], *, limit: int = 3) -> list[dict[str, Any]]:
    elements = structure_json.get("elements", [])
    if not isinstance(elements, list):
        return []

    targets: list[dict[str, Any]] = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict) or element.get("type") != "table":
            continue
        cells = element.get("cells")
        if not isinstance(cells, list) or not cells:
            continue
        page_raw = element.get("page")
        page = int(page_raw) + 1 if isinstance(page_raw, int) and page_raw >= 0 else None
        if page is None:
            continue
        targets.append({
            "table_review_id": str(element.get("review_id") or f"review-{index}"),
            "page": page,
            "bbox": element.get("bbox") if isinstance(element.get("bbox"), dict) else None,
            "num_rows": int(element.get("num_rows", 0) or 0),
            "num_cols": int(element.get("num_cols", 0) or 0),
        })
        if len(targets) >= limit:
            break
    return targets


def _table_semantics_risk(structure_json: dict[str, Any]) -> dict[str, Any]:
    elements = structure_json.get("elements", [])
    if not isinstance(elements, list):
        return {
            "table_count": 0,
            "complex_tables": 0,
            "high_risk_tables": 0,
            "risk_score": 0.0,
            "targets": [],
        }

    targets: list[dict[str, Any]] = []
    complex_tables = 0
    high_risk_tables = 0
    total_risk_score = 0.0

    for index, element in enumerate(elements):
        if not isinstance(element, dict) or element.get("type") != "table":
            continue
        cells = element.get("cells")
        if not isinstance(cells, list) or not cells:
            continue

        page_raw = element.get("page")
        page = int(page_raw) + 1 if isinstance(page_raw, int) and page_raw >= 0 else None
        if page is None:
            continue

        num_rows = int(element.get("num_rows", 0) or 0)
        num_cols = int(element.get("num_cols", 0) or 0)
        spans_present = False
        header_rows: set[int] = set()
        row_header_columns: set[int] = set()
        header_cell_count = 0
        nonempty_cells = 0

        for cell in cells:
            if not isinstance(cell, dict):
                continue
            row = cell.get("row")
            col = cell.get("col")
            if bool(cell.get("column_header", False)) and isinstance(row, int):
                header_rows.add(row)
                header_cell_count += 1
            if bool(cell.get("row_header", False)) and isinstance(col, int):
                row_header_columns.add(col)
                header_cell_count += 1
            row_span = int(cell.get("row_span", 1) or 1) if isinstance(cell.get("row_span"), int) else 1
            col_span = int(cell.get("col_span", 1) or 1) if isinstance(cell.get("col_span"), int) else 1
            if row_span > 1 or col_span > 1:
                spans_present = True
            if _normalize_text(cell.get("text")):
                nonempty_cells += 1

        dense_matrix = num_rows >= 8 and num_cols >= 6
        very_dense_matrix = num_rows >= 12 and num_cols >= 8
        weak_header_signal = header_cell_count == 0 or (
            num_cols >= 4 and len(header_rows) == 0
        )
        multi_level_headers = len(header_rows) > 1 or len(row_header_columns) > 1
        sparse_text = nonempty_cells < max(4, len(cells) * 0.35)

        risk_score = 0.0
        reasons: list[str] = []
        if spans_present:
            risk_score += 1.0
            reasons.append("merged cells or spans present")
        if dense_matrix:
            risk_score += 1.0
            reasons.append("large table matrix")
        if very_dense_matrix:
            risk_score += 1.0
            reasons.append("very dense table")
        if weak_header_signal:
            risk_score += 1.5
            reasons.append("weak header signal")
        if multi_level_headers:
            risk_score += 1.0
            reasons.append("multi-level header pattern")
        if sparse_text:
            risk_score += 0.5
            reasons.append("sparse cell text")

        if risk_score <= 0:
            continue

        complex_tables += 1
        if risk_score >= 2.5:
            high_risk_tables += 1

        total_risk_score += risk_score
        if len(targets) < TABLE_REVIEW_TARGET_LIMIT:
            targets.append({
                "table_review_id": str(element.get("review_id") or f"review-{index}"),
                "page": page,
                "bbox": element.get("bbox") if isinstance(element.get("bbox"), dict) else None,
                "num_rows": num_rows,
                "num_cols": num_cols,
                "risk_score": round(risk_score, 2),
                "risk_reasons": reasons,
                "header_rows": sorted(header_rows),
                "row_header_columns": sorted(row_header_columns),
                "text_excerpt": _normalize_text(element.get("text"))[:240],
            })

    targets.sort(
        key=lambda item: (
            -float(item.get("risk_score", 0.0)),
            int(item.get("page", 0)),
            str(item.get("table_review_id", "")),
        )
    )
    return {
        "table_count": sum(
            1 for element in elements if isinstance(element, dict) and element.get("type") == "table"
        ),
        "complex_tables": complex_tables,
        "high_risk_tables": high_risk_tables,
        "risk_score": round(total_risk_score, 2),
        "targets": targets,
    }


def _longest_nondecreasing_subsequence_len(values: list[int]) -> int:
    tails: list[int] = []
    for value in values:
        idx = bisect_right(tails, value)
        if idx == len(tails):
            tails.append(value)
        else:
            tails[idx] = value
    return len(tails)


def _reading_order_positions(
    fragments: list[str],
    output_text: str,
    *,
    dense: bool,
) -> list[int]:
    if dense:
        search_text = _normalize_dense_text(output_text)
        normalized_fragments = [_normalize_dense_text(fragment) for fragment in fragments]
    else:
        search_text = output_text
        normalized_fragments = fragments

    positions: list[int] = []
    for fragment in normalized_fragments:
        if not fragment:
            continue
        pos = search_text.find(fragment)
        if pos >= 0:
            positions.append(pos)
    return positions


def _reading_order_metrics(fragments: list[str], output_text: str) -> dict[str, float | int | str]:
    exact_positions = _reading_order_positions(fragments, output_text, dense=False)
    dense_positions = _reading_order_positions(fragments, output_text, dense=True)

    positions = exact_positions
    match_mode = "exact"
    if len(dense_positions) > len(exact_positions):
        positions = dense_positions
        match_mode = "dense"
    elif len(dense_positions) == len(exact_positions) and dense_positions:
        exact_ordered = _longest_nondecreasing_subsequence_len(exact_positions)
        dense_ordered = _longest_nondecreasing_subsequence_len(dense_positions)
        if dense_ordered > exact_ordered:
            positions = dense_positions
            match_mode = "dense"

    hits = len(positions)
    hit_rate = hits / max(len(fragments), 1)
    ordered_hits = _longest_nondecreasing_subsequence_len(positions) if positions else 0
    order_rate = ordered_hits / max(hits, 1) if hits else 0.0

    return {
        "fragments_considered": len(fragments),
        "matched_fragments": hits,
        "ordered_fragments": ordered_hits,
        "hit_rate": round(hit_rate, 4),
        "order_rate": round(order_rate, 4),
        "match_mode": match_mode,
    }


def _is_poor_link_text(text: str) -> bool:
    """Return True if *text* is a known non-descriptive link label."""
    normalised = text.strip().lower()
    if normalised in _POOR_LINK_TEXT_EXACT:
        return True
    return bool(_POOR_LINK_TEXT_RE.match(normalised))


def _canonical_named_destination(value) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text or text == "None":
        return None
    return text[1:] if text.startswith("/") else text


def _iter_name_tree_entries(node) -> list[tuple[str, Any]]:
    entries: list[tuple[str, Any]] = []
    if not isinstance(node, pikepdf.Dictionary):
        return entries

    names = node.get("/Names")
    if isinstance(names, pikepdf.Array):
        for index in range(0, len(names) - 1, 2):
            key = _canonical_named_destination(names[index])
            if key:
                entries.append((key, names[index + 1]))

    kids = node.get("/Kids")
    if isinstance(kids, pikepdf.Array):
        for kid in kids:
            try:
                entries.extend(_iter_name_tree_entries(kid))
            except Exception:
                continue

    return entries


def _is_generated_link_contents(text: str, *, uri: str = "", has_dest: bool = False) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if normalized == "Link":
        return True
    if has_dest and normalized == "Link to destination":
        return True
    if uri and normalized == f"Link to {uri}":
        return True
    return False


def _check_link_text_quality(output_pdf: Path) -> list[dict[str, Any]]:
    """Scan output PDF for links whose /Contents text is non-descriptive."""
    poor_links: list[dict[str, Any]] = []
    try:
        with pikepdf.Pdf.open(output_pdf) as pdf:
            for page_idx, page in enumerate(pdf.pages, start=1):
                annots = page.get("/Annots")
                if not isinstance(annots, pikepdf.Array):
                    continue
                for annotation in annots:
                    try:
                        subtype = annotation.get("/Subtype")
                        if subtype != pikepdf.Name("/Link"):
                            continue
                        contents = str(annotation.get("/Contents", "")).strip()
                        uri = ""
                        action = annotation.get("/A")
                        if action is not None and hasattr(action, "get"):
                            uri = str(action.get("/URI", "")).strip()
                        has_dest = annotation.get("/Dest") is not None or (
                            action is not None and hasattr(action, "get") and str(action.get("/S", "")) == "/GoTo"
                        )
                        if _is_generated_link_contents(contents, uri=uri, has_dest=has_dest):
                            continue
                        if not contents or _is_poor_link_text(contents):
                            poor_links.append({
                                "page": page_idx,
                                "text": contents or "(empty)",
                                "uri": uri,
                            })
                    except Exception:
                        continue
    except Exception:
        pass
    return poor_links


def _check_internal_link_destinations(output_pdf: Path) -> list[dict[str, Any]]:
    """Find internal links whose destinations do not resolve."""
    broken: list[dict[str, Any]] = []
    try:
        with pikepdf.Pdf.open(output_pdf) as pdf:
            named_dests: set[str] = set()
            dests = pdf.Root.get("/Dests")
            if isinstance(dests, pikepdf.Dictionary):
                for key in dests.keys():
                    canonical = _canonical_named_destination(key)
                    if canonical:
                        named_dests.add(canonical)
            names = pdf.Root.get("/Names")
            if isinstance(names, pikepdf.Dictionary):
                dest_tree = names.get("/Dests")
                if isinstance(dest_tree, pikepdf.Dictionary):
                    for key, _ in _iter_name_tree_entries(dest_tree):
                        named_dests.add(key)

            for page_idx, page in enumerate(pdf.pages, start=1):
                annots = page.get("/Annots")
                if not isinstance(annots, pikepdf.Array):
                    continue
                for annotation in annots:
                    try:
                        subtype = annotation.get("/Subtype")
                        if subtype != pikepdf.Name("/Link"):
                            continue
                        # Check /Dest
                        dest = annotation.get("/Dest")
                        if dest is not None:
                            dest_name = _canonical_named_destination(dest)
                            if dest_name is not None:
                                if dest_name not in named_dests:
                                    broken.append({
                                        "page": page_idx,
                                        "dest": dest_name,
                                        "reason": "Named destination not found",
                                    })
                                continue
                            if isinstance(dest, pikepdf.Array) and len(dest) >= 1:
                                # Direct page reference — verify page object exists
                                continue  # Direct references are generally valid
                        # Check /A GoTo action
                        action = annotation.get("/A")
                        if action is not None and hasattr(action, "get"):
                            action_type = str(action.get("/S", ""))
                            if action_type == "/GoTo":
                                goto_dest = action.get("/D")
                                dest_name = _canonical_named_destination(goto_dest)
                                if dest_name is not None:
                                    if dest_name not in named_dests:
                                        broken.append({
                                            "page": page_idx,
                                            "dest": dest_name,
                                            "reason": "GoTo destination not found",
                                        })
                                elif isinstance(goto_dest, pikepdf.Array) and len(goto_dest) >= 1:
                                    # Direct page ref in array — try to resolve
                                    try:
                                        page_ref = goto_dest[0]
                                        if isinstance(page_ref, pikepdf.Object) and hasattr(page_ref, "objgen"):
                                            # Verify the referenced object exists
                                            _ = page_ref.get("/Type")
                                    except Exception:
                                        broken.append({
                                            "page": page_idx,
                                            "dest": str(goto_dest),
                                            "reason": "GoTo page reference unresolvable",
                                        })
                    except Exception:
                        continue
    except Exception:
        pass
    return broken


def _severity_rank(severity: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(severity, 0)


def _task_for_violation(violation: dict[str, Any]) -> tuple[str, str, str]:
    category = str(violation.get("category") or "").strip().lower()
    rule_id = str(violation.get("rule_id") or "")
    description = str(violation.get("description") or "").lower()

    if category == "fonts" or FONT_RULE_FRAGMENT in rule_id:
        return (
            "font_text_fidelity",
            "Verify font-to-text mapping",
            "Remaining font and Unicode errors can change what assistive technology reads.",
        )
    if category in {"annotations", "links"}:
        return (
            "annotation_description",
            "Verify annotations and link descriptions",
            "Annotations or links still need manual review for accessible naming and structure.",
        )
    if category == "figures" or "alternate text" in description or " alt " in f" {description} ":
        return (
            "alt_text",
            "Verify figure descriptions",
            "Some figure descriptions still require manual review for accuracy.",
        )
    if category == "tables" or any(keyword in description for keyword in TABLE_KEYWORDS):
        return (
            "table_semantics",
            "Verify table semantics",
            "Table structure still needs manual review for headers and reading order.",
        )
    return (
        "reading_order",
        "Verify reading order and structure",
        "Remaining structural issues may affect the document reading experience.",
    )


def _rule_id_from_summary(summary: dict[str, Any]) -> str:
    specification = str(summary.get("specification") or "").strip()
    clause = str(summary.get("clause") or "").strip()
    test_number = str(summary.get("testNumber") or "").strip()
    if not specification or not clause or not test_number:
        return ""
    return f"{specification}-{clause}-{test_number}"


def _iter_verapdf_rule_summaries(raw_validation_report: dict[str, Any] | None):
    report_root = raw_validation_report.get("report", {}) if isinstance(raw_validation_report, dict) else {}
    jobs = report_root.get("jobs", []) if isinstance(report_root, dict) else []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        validation_raw = job.get("validationResult", {})
        validation_entries = validation_raw if isinstance(validation_raw, list) else [validation_raw]
        for validation in validation_entries:
            if not isinstance(validation, dict):
                continue
            details = validation.get("details", {})
            if not isinstance(details, dict):
                continue
            rule_summaries = details.get("ruleSummaries", [])
            if not isinstance(rule_summaries, list):
                continue
            for summary in rule_summaries:
                if isinstance(summary, dict):
                    yield summary


def _extract_font_review_targets(
    raw_validation_report: dict[str, Any] | None,
    font_rule_ids: set[str],
    *,
    output_pdf: Path | None = None,
) -> tuple[list[dict[str, Any]], list[int], list[str]]:
    grouped: dict[tuple[str, int | None, str, int | None], dict[str, Any]] = {}
    pages_seen: set[int] = set()
    fonts_seen: set[str] = set()

    for summary in _iter_verapdf_rule_summaries(raw_validation_report):
        rule_id = _rule_id_from_summary(summary)
        if not rule_id or rule_id not in font_rule_ids:
            continue

        checks = summary.get("checks", [])
        if not isinstance(checks, list):
            continue

        for check in checks:
            if not isinstance(check, dict):
                continue
            if str(check.get("status") or "").lower() != "failed":
                continue

            context = str(check.get("context") or "").strip()
            parsed_context = parse_verapdf_context_path(context)
            page_number = parsed_context.get("page_number")
            if not isinstance(page_number, int):
                page_number = None
            font_match = USED_GLYPH_FONT_RE.search(context)
            font_name = _strip_subset_prefix(font_match.group(1)) if font_match else ""
            content_stream_index = parsed_context.get("page_content_stream_index")
            if not isinstance(content_stream_index, int):
                content_stream_index = None
            operator_index = parsed_context.get("operator_index")
            if not isinstance(operator_index, int):
                operator_index = None
            key = (rule_id, page_number, font_name, operator_index)
            entry = grouped.setdefault(
                key,
                {
                    "rule_id": rule_id,
                    "page": page_number,
                    "font": font_name,
                    "content_stream_index": content_stream_index,
                    "operator_index": operator_index,
                    "context_path": context,
                    "count": 0,
                    "sample_context": context[:240],
                },
            )
            entry["count"] += 1
            if page_number is not None:
                pages_seen.add(page_number)
            if font_name:
                fonts_seen.add(font_name)

    if output_pdf is not None and output_pdf.exists():
        for entry in grouped.values():
            context_path = str(entry.get("context_path") or "").strip()
            if not context_path:
                continue
            try:
                context_info = extract_operator_text_context(
                    pdf_path=output_pdf,
                    context_path=context_path,
                )
            except Exception:
                continue
            if isinstance(context_info, dict):
                entry.update(context_info)

    targets = sorted(
        grouped.values(),
        key=lambda item: (
            item["page"] if isinstance(item.get("page"), int) else 10**9,
            item["operator_index"] if isinstance(item.get("operator_index"), int) else 10**9,
            -int(item.get("count", 0) or 0),
            str(item.get("font") or "").lower(),
            str(item.get("rule_id") or ""),
        ),
    )
    return targets[:12], sorted(pages_seen), sorted(fonts_seen)


def assess_fidelity(
    *,
    input_pdf: Path,
    output_pdf: Path,
    comparison_source_pdf: Path | None = None,
    structure_json: dict[str, Any],
    alt_entries: list[dict[str, Any]],
    validation_report: dict[str, Any],
    raw_validation_report: dict[str, Any] | None = None,
    tagging_metrics: dict[str, Any],
    classification: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    tasks_by_key: dict[str, dict[str, Any]] = {}

    def add_task(
        key: str,
        *,
        task_type: str,
        title: str,
        detail: str,
        severity: str = "medium",
        blocking: bool = True,
        source: str = "fidelity",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        existing = tasks_by_key.get(key)
        if existing is None:
            tasks_by_key[key] = {
                "task_type": task_type,
                "title": title,
                "detail": detail,
                "severity": severity,
                "blocking": blocking,
                "status": "pending_review",
                "source": source,
                "metadata": metadata or {},
            }
            return

        if blocking and not existing["blocking"]:
            existing["blocking"] = True
        if _severity_rank(severity) > _severity_rank(str(existing.get("severity"))):
            existing["severity"] = severity
        if metadata:
            existing.setdefault("metadata", {}).update(metadata)

    violations = validation_report.get("violations", [])
    unresolved_errors = [
        violation
        for violation in violations
        if isinstance(violation, dict) and str(violation.get("severity")) == "error"
    ]

    if unresolved_errors:
        checks.append({
            "check": "compliance",
            "status": "fail",
            "message": f"{len(unresolved_errors)} validation rule(s) still require remediation.",
            "metrics": {
                "remaining_rules": len(unresolved_errors),
                "remaining_errors": validation_report.get("summary", {}).get("errors", 0),
            },
        })
        grouped: dict[str, dict[str, Any]] = {}
        for violation in unresolved_errors:
            task_type, title, detail = _task_for_violation(violation)
            key = f"review:{task_type}"
            entry = grouped.setdefault(
                key,
                {
                "task_type": task_type,
                "title": title,
                "detail": detail,
                "count": 0,
                "rules": [],
                },
            )
            count = violation.get("count", 1)
            entry["count"] += count if isinstance(count, int) and count > 0 else 1
            rule_id = str(violation.get("rule_id") or "").strip()
            if rule_id and rule_id not in entry["rules"]:
                entry["rules"].append(rule_id)

        for key, entry in grouped.items():
            add_task(
                key,
                task_type=entry["task_type"],
                title=entry["title"],
                detail=f"{entry['detail']} Remaining occurrences: {entry['count']}.",
                severity="high",
                blocking=True,
                source="validation",
                metadata={"rules": entry["rules"], "count": entry["count"]},
            )
    else:
        checks.append({
            "check": "compliance",
            "status": "pass",
            "message": "No remaining validation errors.",
            "metrics": {"remaining_rules": 0, "remaining_errors": 0},
        })

    comparison_pdf = comparison_source_pdf or input_pdf
    using_alternate_source = comparison_pdf != input_pdf
    source_text = _extract_pdf_text_sample(comparison_pdf)
    output_text = _extract_pdf_text_sample(output_pdf)
    original_source_text = (
        _extract_pdf_text_sample(input_pdf)
        if using_alternate_source
        else source_text
    )
    meaningful_structure_elements = _meaningful_structure_element_count(structure_json)

    if classification == "scanned":
        ocr_metrics = _sample_visual_ink(input_pdf)
        has_meaningful_ocr_text = len(output_text) >= SCANNED_TEXT_MIN_CHARS
        has_meaningful_structure = meaningful_structure_elements > 0
        sampled_pages = int(ocr_metrics.get("sampled_pages") or 0)
        visually_blank = ocr_metrics.get("visually_blank")
        status = "pass"
        message = "Scanned-document OCR produced usable text or structure."

        if sampled_pages == 0:
            status = "skip"
            message = "Skipped scanned OCR coverage check because page previews were unavailable."
        elif visually_blank is True:
            status = "skip"
            message = "Sampled scanned pages appear visually blank; OCR coverage check skipped."
        elif not has_meaningful_ocr_text and not has_meaningful_structure:
            status = "fail"
            message = (
                "Scanned document appears to contain visible content, but OCR and structure extraction "
                "did not produce usable text."
            )
            add_task(
                "review:content_fidelity",
                task_type="content_fidelity",
                title="Verify OCR text coverage",
                detail=(
                    "The scanned PDF appears to contain visible page content, but OCR and structure extraction "
                    "produced little or no usable text. Review the OCR result or rescan the source document."
                ),
                severity="high",
                blocking=True,
                metadata={
                    "output_chars": len(output_text),
                    "meaningful_structure_elements": meaningful_structure_elements,
                    **ocr_metrics,
                },
            )
        elif not has_meaningful_ocr_text:
            status = "warning"
            message = "Scanned document OCR produced limited extractable text; spot-check the result."
            add_task(
                "review:content_fidelity",
                task_type="content_fidelity",
                title="Spot-check OCR text coverage",
                detail=(
                    "The scanned PDF produced very little extractable text. Check whether the output text "
                    "matches the visible content before distribution."
                ),
                severity="medium",
                blocking=False,
                metadata={
                    "output_chars": len(output_text),
                    "meaningful_structure_elements": meaningful_structure_elements,
                    **ocr_metrics,
                },
            )

        checks.append({
            "check": "ocr_coverage",
            "status": status,
            "message": message,
            "metrics": {
                "output_chars": len(output_text),
                "meaningful_structure_elements": meaningful_structure_elements,
                **ocr_metrics,
            },
        })

    if len(source_text) >= TEXT_SAMPLE_MIN_CHARS and len(output_text) >= TEXT_SAMPLE_MIN_CHARS:
        similarity = SequenceMatcher(None, source_text, output_text).ratio()
        length_ratio = len(output_text) / max(len(source_text), 1)
        status = "pass"
        original_similarity = None
        original_length_ratio = None
        if using_alternate_source and len(original_source_text) >= TEXT_SAMPLE_MIN_CHARS:
            original_similarity = SequenceMatcher(None, original_source_text, output_text).ratio()
            original_length_ratio = len(output_text) / max(len(original_source_text), 1)
            if (
                original_similarity < 0.82
                or original_length_ratio < 0.7
                or original_length_ratio > 1.45
            ):
                add_task(
                    "review:content_fidelity:ocr_rescue",
                    task_type="content_fidelity",
                    title="Spot-check OCR rescue text fidelity",
                    detail=(
                        "OCR rescue replaced the original extractable text layer. "
                        "Spot-check a few pages against the visible document before distribution."
                    ),
                    severity="medium",
                    blocking=False,
                    metadata={
                        "comparison_source": "retag_input",
                        "original_similarity": round(original_similarity, 4),
                        "original_length_ratio": round(original_length_ratio, 4),
                    },
                )
        if similarity < 0.82 or length_ratio < 0.7 or length_ratio > 1.45:
            status = "fail"
            add_task(
                "review:content_fidelity",
                task_type="content_fidelity",
                title="Verify extracted text fidelity",
                detail=(
                    "The remediated PDF text differs substantially from the source text sample. "
                    "Check for OCR drift, Unicode remaps, or missing content."
                ),
                severity="high",
                blocking=True,
                metadata={
                    "similarity": round(similarity, 4),
                    "length_ratio": round(length_ratio, 4),
                },
            )
        elif similarity < 0.9 or length_ratio < 0.85 or length_ratio > 1.2:
            status = "warning"
            add_task(
                "review:content_fidelity",
                task_type="content_fidelity",
                title="Spot-check extracted text fidelity",
                detail=(
                    "The remediated PDF text sample differs moderately from the source. "
                    "A manual spot-check is recommended."
                ),
                severity="medium",
                blocking=False,
                metadata={
                    "similarity": round(similarity, 4),
                    "length_ratio": round(length_ratio, 4),
                },
            )
        checks.append({
            "check": "text_drift",
            "status": status,
            "message": (
                "Compared the final PDF against the source used for the final tagging pass."
                if using_alternate_source
                else "Compared source and remediated text samples."
            ),
            "metrics": {
                "similarity": round(similarity, 4),
                "length_ratio": round(length_ratio, 4),
                "source_chars": len(source_text),
                "output_chars": len(output_text),
                "comparison_source": "retag_input" if using_alternate_source else "original_input",
                **(
                    {"original_similarity": round(original_similarity, 4)}
                    if original_similarity is not None
                    else {}
                ),
                **(
                    {"original_length_ratio": round(original_length_ratio, 4)}
                    if original_length_ratio is not None
                    else {}
                ),
            },
        })
    else:
        checks.append({
            "check": "text_drift",
            "status": "skip",
            "message": "Skipped text-drift comparison due to limited extractable text.",
            "metrics": {
                "source_chars": len(source_text),
                "output_chars": len(output_text),
                "classification": classification or "unknown",
            },
        })

    fragments = _collect_structural_fragments(structure_json)
    if fragments and output_text:
        order_metrics = _reading_order_metrics(fragments, output_text)
        hit_rate = float(order_metrics["hit_rate"])
        order_rate = float(order_metrics["order_rate"])
        status = "pass"
        if len(fragments) >= 8 and (hit_rate < 0.4 or order_rate < 0.55):
            status = "fail"
            add_task(
                "review:reading_order",
                task_type="reading_order",
                title="Verify reading order",
                detail=(
                    "Structural text fragments are not appearing in the remediated text in a stable order. "
                    "Manual reading-order review is required."
                ),
                severity="high",
                blocking=True,
                metadata=order_metrics,
            )
        elif len(fragments) >= 8 and (hit_rate < 0.65 or order_rate < 0.85):
            status = "warning"
            add_task(
                "review:reading_order",
                task_type="reading_order",
                title="Spot-check reading order",
                detail=(
                    "The structural text order signal is weaker than expected. "
                    "A manual reading-order spot-check is recommended."
                ),
                severity="medium",
                blocking=False,
                metadata=order_metrics,
            )

        checks.append({
            "check": "reading_order",
            "status": status,
            "message": "Compared structural text fragments against remediated text order.",
            "metrics": order_metrics,
        })
    else:
        checks.append({
            "check": "reading_order",
            "status": "skip",
            "message": "Skipped reading-order heuristic due to limited structural text.",
            "metrics": {"fragments_considered": len(fragments)},
        })

    elements = structure_json.get("elements", [])
    figure_captions: dict[int, str] = {}
    table_count = 0
    if isinstance(elements, list):
        for element in elements:
            if not isinstance(element, dict):
                continue
            if element.get("type") == "figure" and isinstance(element.get("figure_index"), int):
                figure_captions[element["figure_index"]] = _normalize_text(element.get("caption"))
            elif element.get("type") == "table":
                table_count += 1

    tagged_tables = tagging_metrics.get("tables_tagged", 0)
    tagged_table_count = tagged_tables if isinstance(tagged_tables, int) and tagged_tables >= 0 else 0
    if table_count > 0:
        coverage = tagged_table_count / table_count
        status = "pass" if tagged_table_count >= table_count else "warning"
        if tagged_table_count < table_count:
            table_targets = _table_review_targets(structure_json)
            add_task(
                "review:table_semantics",
                task_type="table_semantics",
                title="Verify table semantics",
                detail=(
                    "Not all detected tables were tagged in the final PDF. "
                    "Manual review is required for table headers and structure."
                ),
                severity="high" if coverage < 0.75 else "medium",
                blocking=True,
                metadata={
                    "detected_tables": table_count,
                    "tagged_tables": tagged_table_count,
                    "coverage": round(coverage, 4),
                    "pages_to_check": sorted({
                        int(target["page"])
                        for target in table_targets
                        if isinstance(target.get("page"), int)
                    }),
                    "table_review_targets": table_targets,
                },
            )
        checks.append({
            "check": "table_coverage",
            "status": status,
            "message": "Compared detected tables against tagged tables.",
            "metrics": {
                "detected_tables": table_count,
                "tagged_tables": tagged_table_count,
                "coverage": round(coverage, 4),
            },
        })

        table_risk = _table_semantics_risk(structure_json)
        if table_risk["complex_tables"] > 0:
            risk_targets = table_risk["targets"]
            risk_status = "warning"
            blocking = False
            severity = "medium"
            detail = (
                "Some detected tables look semantically risky for assistive technology. "
                "Review header rows, row headers, and merged cells."
            )

            if table_risk["high_risk_tables"] > 0:
                risk_status = "fail"
                blocking = True
                severity = "high"
                detail = (
                    "Complex tables with dense layouts, weak header signals, or merged cells need manual review "
                    "to confirm accessible table semantics."
                )

            add_task(
                "review:table_semantics_risk",
                task_type="table_semantics",
                title="Review complex table semantics",
                detail=detail,
                severity=severity,
                blocking=blocking,
                metadata={
                    "detected_tables": table_count,
                    "tagged_tables": tagged_table_count,
                    "complex_tables": table_risk["complex_tables"],
                    "high_risk_tables": table_risk["high_risk_tables"],
                    "risk_score": table_risk["risk_score"],
                    "pages_to_check": sorted({
                        int(target["page"])
                        for target in risk_targets
                        if isinstance(target.get("page"), int)
                    }),
                    "table_review_targets": risk_targets,
                },
            )

            checks.append({
                "check": "table_risk",
                "status": risk_status,
                "message": "Flagged tables with complex structure, weak header signals, or merged cells.",
                "metrics": {
                    "complex_tables": table_risk["complex_tables"],
                    "high_risk_tables": table_risk["high_risk_tables"],
                    "risk_score": table_risk["risk_score"],
                },
            })
        else:
            checks.append({
                "check": "table_risk",
                "status": "pass",
                "message": "No semantically risky tables detected from structure metadata.",
                "metrics": {
                    "complex_tables": 0,
                    "high_risk_tables": 0,
                    "risk_score": 0.0,
                },
            })
    else:
        checks.append({
            "check": "table_coverage",
            "status": "skip",
            "message": "No tables detected in structure extraction.",
            "metrics": {"detected_tables": 0, "tagged_tables": tagged_table_count},
        })
        checks.append({
            "check": "table_risk",
            "status": "skip",
            "message": "No tables detected in structure extraction.",
            "metrics": {
                "complex_tables": 0,
                "high_risk_tables": 0,
                "risk_score": 0.0,
            },
        })

    machine_only_alt = 0
    caption_backed_alt = 0
    for entry in alt_entries:
        if not isinstance(entry, dict) or entry.get("status") != "approved":
            continue
        generated_text = _normalize_text(entry.get("generated_text"))
        edited_text = _normalize_text(entry.get("edited_text"))
        if not generated_text or edited_text:
            continue
        figure_index = entry.get("figure_index")
        caption_text = figure_captions.get(figure_index) if isinstance(figure_index, int) else ""
        if caption_text and generated_text == caption_text:
            caption_backed_alt += 1
        else:
            machine_only_alt += 1

    if machine_only_alt > 0:
        add_task(
            "review:alt_text",
            task_type="alt_text",
            title="Spot-check generated alt text",
            detail=(
                "Some figure descriptions were auto-approved without human edits. "
                "Spot-check them for accuracy before distribution."
            ),
            severity="medium",
            blocking=False,
            metadata={
                "machine_only_alt": machine_only_alt,
                "caption_backed_alt": caption_backed_alt,
            },
        )
        status = "warning"
    else:
        status = "pass"
    checks.append({
        "check": "alt_text_provenance",
        "status": status,
        "message": "Tracked whether approved alt text was human-edited, caption-backed, or machine-only.",
        "metrics": {
            "machine_only_alt": machine_only_alt,
            "caption_backed_alt": caption_backed_alt,
            "reviewed_alt_entries": len(alt_entries),
        },
    })

    # ── 1A: Link text quality ─────────────────────────────────────────────
    poor_links = _check_link_text_quality(output_pdf)
    if poor_links:
        add_task(
            "review:link_text_quality",
            task_type="annotation_description",
            title="Review non-descriptive link text",
            detail=(
                f"{len(poor_links)} link(s) use non-descriptive text such as "
                f"\"{poor_links[0]['text']}\". Screen reader users rely on "
                "meaningful link labels to understand where links lead."
            ),
            severity="medium",
            blocking=False,
            metadata={"poor_links": poor_links[:20]},
        )
    checks.append({
        "check": "link_text_quality",
        "status": "warning" if poor_links else "pass",
        "message": (
            f"{len(poor_links)} link(s) with non-descriptive text detected."
            if poor_links
            else "All link text appears descriptive."
        ),
        "metrics": {"poor_link_count": len(poor_links)},
    })

    # ── 1C: Internal link destination validation ──────────────────────────
    broken_links = _check_internal_link_destinations(output_pdf)
    if broken_links:
        add_task(
            "review:broken_internal_links",
            task_type="annotation_description",
            title="Fix broken internal links",
            detail=(
                f"{len(broken_links)} internal link(s) point to destinations "
                "that could not be resolved. These links may not function "
                "for any user, including those using assistive technology."
            ),
            severity="medium",
            blocking=False,
            metadata={"broken_links": broken_links[:20]},
        )
    checks.append({
        "check": "internal_link_destinations",
        "status": "warning" if broken_links else "pass",
        "message": (
            f"{len(broken_links)} broken internal link destination(s) detected."
            if broken_links
            else "All internal link destinations resolve."
        ),
        "metrics": {"broken_link_count": len(broken_links)},
    })

    # ── Font text fidelity ────────────────────────────────────────────────
    font_remediation = validation_report.get("remediation", {}).get("font_remediation", {})
    unicode_gate = font_remediation.get("unicode_gate", {})
    font_diagnostics = (
        font_remediation.get("postflight_diagnostics")
        or font_remediation.get("preflight_diagnostics")
        or {}
    )
    font_diagnostics_summary = (
        font_diagnostics.get("summary")
        if isinstance(font_diagnostics, dict)
        else {}
    )
    top_font_profiles = []
    if isinstance(font_diagnostics, dict):
        raw_profiles = font_diagnostics.get("profiles")
        if isinstance(raw_profiles, list):
            for profile in raw_profiles[:3]:
                if not isinstance(profile, dict):
                    continue
                top_font_profiles.append({
                    "base_font": str(profile.get("base_font") or ""),
                    "subtype": str(profile.get("subtype") or ""),
                    "issue_tags": profile.get("issue_tags") if isinstance(profile.get("issue_tags"), list) else [],
                    "missing_used_code_count": int(profile.get("missing_used_code_count", 0) or 0),
                    "invalid_tounicode_entries": int(profile.get("invalid_tounicode_entries", 0) or 0),
                    "embedded": bool(profile.get("embedded", False)),
                })
    remaining_font_errors = sum(
        (
            violation.get("count", 1) if isinstance(violation.get("count"), int) and violation.get("count", 1) > 0 else 1
        )
        for violation in unresolved_errors
        if isinstance(violation, dict) and (
            str(violation.get("category") or "").lower() == "fonts"
            or FONT_RULE_FRAGMENT in str(violation.get("rule_id") or "")
        )
    )
    font_rule_ids = sorted({
        str(violation.get("rule_id") or "").strip()
        for violation in unresolved_errors
        if isinstance(violation, dict) and (
            str(violation.get("category") or "").lower() == "fonts"
            or FONT_RULE_FRAGMENT in str(violation.get("rule_id") or "")
        )
        and str(violation.get("rule_id") or "").strip()
    })
    font_review_targets, pages_to_check, fonts_to_check = _extract_font_review_targets(
        raw_validation_report,
        set(font_rule_ids),
        output_pdf=output_pdf,
    )
    if remaining_font_errors > 0 or (
        isinstance(unicode_gate, dict) and not unicode_gate.get("allow_automatic", True)
    ):
        add_task(
            "review:font_text_fidelity",
            task_type="font_text_fidelity",
            title="Verify accessible text against visible text",
            detail=(
                "Font or Unicode mapping risk remains. Check that screen readers announce the same text "
                "that sighted readers see."
            ),
            severity="high",
            blocking=True,
            metadata={
                "remaining_font_errors": remaining_font_errors,
                "font_rule_ids": font_rule_ids,
                "pages_to_check": pages_to_check,
                "fonts_to_check": fonts_to_check,
                "font_review_targets": font_review_targets,
                "unicode_gate": unicode_gate,
                "font_diagnostics_summary": font_diagnostics_summary,
                "top_font_profiles": top_font_profiles,
            },
        )
        checks.append({
            "check": "font_text_fidelity",
            "status": "fail",
            "message": "Font-related compliance or Unicode-gate risk requires manual review.",
            "metrics": {
                "remaining_font_errors": remaining_font_errors,
                "review_pages": len(pages_to_check),
                "review_fonts": len(fonts_to_check),
                "automatic_unicode_allowed": bool(unicode_gate.get("allow_automatic", True))
                if isinstance(unicode_gate, dict)
                else True,
            },
        })
    else:
        checks.append({
            "check": "font_text_fidelity",
            "status": "pass",
            "message": "No residual font-text fidelity risk was detected.",
            "metrics": {"remaining_font_errors": 0},
        })

    tasks = sorted(
        tasks_by_key.values(),
        key=lambda task: (
            0 if task["blocking"] else 1,
            -_severity_rank(str(task.get("severity"))),
            str(task.get("task_type")),
        ),
    )
    blocking_tasks = [task for task in tasks if task["blocking"]]
    advisory_tasks = [task for task in tasks if not task["blocking"]]

    report = {
        "passed": len(blocking_tasks) == 0,
        "summary": {
            "blocking_tasks": len(blocking_tasks),
            "advisory_tasks": len(advisory_tasks),
            "total_tasks": len(tasks),
        },
        "checks": checks,
    }
    return report, tasks
