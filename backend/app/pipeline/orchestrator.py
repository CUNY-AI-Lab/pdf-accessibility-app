"""Pipeline orchestrator: runs all steps in sequence with progress events."""

import asyncio
import copy
import json
import logging
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pikepdf
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AltTextEntry, AppliedChange, Job, JobStep, ReviewTask
from app.pipeline.alt_text import AltTextResult, figure_applied_change_specs, generate_alt_text
from app.pipeline.classify import classify_pdf
from app.pipeline.fidelity import _table_semantics_risk, assess_fidelity

# Keep the former font entry points importable; dependency patches belong in fonts.*.
from app.pipeline.fonts.diagnostics import _inspect_font_diagnostics as _inspect_font_diagnostics
from app.pipeline.fonts.diagnostics import _inspect_pdf_features as _inspect_pdf_features
from app.pipeline.fonts.diagnostics import (
    _unicode_repair_gate_from_diagnostics as _unicode_repair_gate_from_diagnostics,
)
from app.pipeline.fonts.embedding import (
    GHOSTSCRIPT_ALWAYS_EMBED_FONTS as GHOSTSCRIPT_ALWAYS_EMBED_FONTS,
)
from app.pipeline.fonts.embedding import _embed_system_fonts as _embed_system_fonts
from app.pipeline.fonts.embedding import _embed_system_fonts_sync as _embed_system_fonts_sync
from app.pipeline.fonts.embedding import _ghostscript_embed_command as _ghostscript_embed_command
from app.pipeline.fonts.embedding import (
    _rewrite_pdf_with_ghostscript_embed as _rewrite_pdf_with_ghostscript_embed,
)
from app.pipeline.fonts.encoding import HEX_STR_RE as HEX_STR_RE
from app.pipeline.fonts.encoding import MAX_TOUNICODE_BFRANGE_SPAN as MAX_TOUNICODE_BFRANGE_SPAN
from app.pipeline.fonts.encoding import (
    SAFE_IMPLICIT_STANDARD_BASEFONTS as SAFE_IMPLICIT_STANDARD_BASEFONTS,
)
from app.pipeline.fonts.encoding import _cff_builtin_encoding_map as _cff_builtin_encoding_map
from app.pipeline.fonts.encoding import _collect_gid_to_unicode as _collect_gid_to_unicode
from app.pipeline.fonts.encoding import _decode_tounicode_hex as _decode_tounicode_hex
from app.pipeline.fonts.encoding import _encoding_differences_map as _encoding_differences_map
from app.pipeline.fonts.encoding import _glyph_name_to_unicode as _glyph_name_to_unicode
from app.pipeline.fonts.encoding import _is_valid_unicode_text as _is_valid_unicode_text
from app.pipeline.fonts.encoding import _merge_tounicode_maps as _merge_tounicode_maps
from app.pipeline.fonts.encoding import _named_simple_encoding_map as _named_simple_encoding_map
from app.pipeline.fonts.encoding import _parse_tounicode_map as _parse_tounicode_map
from app.pipeline.fonts.encoding import _parse_tounicode_map_details as _parse_tounicode_map_details
from app.pipeline.fonts.encoding import _raw_text_bytes as _raw_text_bytes
from app.pipeline.fonts.encoding import _render_tounicode_cmap as _render_tounicode_cmap
from app.pipeline.fonts.encoding import (
    _sanitize_text_showing_zero_bytes as _sanitize_text_showing_zero_bytes,
)
from app.pipeline.fonts.encoding import (
    _simple_font_auto_unicode_policy as _simple_font_auto_unicode_policy,
)
from app.pipeline.fonts.encoding import _simple_font_unicode_map as _simple_font_unicode_map
from app.pipeline.fonts.encoding import (
    _simple_font_zero_byte_repair_candidate as _simple_font_zero_byte_repair_candidate,
)
from app.pipeline.fonts.lanes import _attempt_font_lane as _attempt_font_lane
from app.pipeline.fonts.lanes import _embed_lane_should_skip_local as _embed_lane_should_skip_local
from app.pipeline.fonts.lanes import _font_remediation_lanes as _font_remediation_lanes
from app.pipeline.fonts.lanes import _has_font_errors as _has_font_errors
from app.pipeline.fonts.lanes import _join_messages as _join_messages
from app.pipeline.fonts.lanes import _ocr_lane_skip_reasons as _ocr_lane_skip_reasons
from app.pipeline.fonts.names import FONT_NAME_RE as FONT_NAME_RE
from app.pipeline.fonts.names import FONT_SUBSET_RE as FONT_SUBSET_RE
from app.pipeline.fonts.names import _base_font_name as _base_font_name
from app.pipeline.fonts.names import _looks_like_ocr_overlay_font as _looks_like_ocr_overlay_font
from app.pipeline.fonts.names import _normalize_font_name as _normalize_font_name
from app.pipeline.fonts.names import _strip_subset_prefix as _strip_subset_prefix
from app.pipeline.fonts.programs import (
    GHOSTSCRIPT_TYPE1_DESCRIPTOR_SPECS as GHOSTSCRIPT_TYPE1_DESCRIPTOR_SPECS,
)
from app.pipeline.fonts.programs import (
    GHOSTSCRIPT_TYPE1_SUBSTITUTES as GHOSTSCRIPT_TYPE1_SUBSTITUTES,
)
from app.pipeline.fonts.programs import SYSTEM_FONT_DIRS as SYSTEM_FONT_DIRS
from app.pipeline.fonts.programs import SYSTEM_FONT_EXTENSIONS as SYSTEM_FONT_EXTENSIONS
from app.pipeline.fonts.programs import _coerce_metric_int as _coerce_metric_int
from app.pipeline.fonts.programs import _font_name_keys as _font_name_keys
from app.pipeline.fonts.programs import _font_name_metadata as _font_name_metadata
from app.pipeline.fonts.programs import (
    _ghostscript_font_metrics_dir as _ghostscript_font_metrics_dir,
)
from app.pipeline.fonts.programs import (
    _ghostscript_resource_font_dir as _ghostscript_resource_font_dir,
)
from app.pipeline.fonts.programs import (
    _ghostscript_type1_descriptor as _ghostscript_type1_descriptor,
)
from app.pipeline.fonts.programs import (
    _ghostscript_type1_font_program as _ghostscript_type1_font_program,
)
from app.pipeline.fonts.programs import _local_embed_support_kind as _local_embed_support_kind
from app.pipeline.fonts.programs import _local_font_program as _local_font_program
from app.pipeline.fonts.programs import _system_font_index as _system_font_index
from app.pipeline.fonts.programs import _system_font_program as _system_font_program
from app.pipeline.fonts.repair import _cid_cff_width_key as _cid_cff_width_key
from app.pipeline.fonts.repair import _collect_cid_cff_widths as _collect_cid_cff_widths
from app.pipeline.fonts.repair import _parse_cid_widths_array as _parse_cid_widths_array
from app.pipeline.fonts.repair import _render_cid_widths_array as _render_cid_widths_array
from app.pipeline.fonts.repair import _repair_pdf_font_dicts as _repair_pdf_font_dicts
from app.pipeline.fonts.repair import _repair_pdf_font_dicts_sync as _repair_pdf_font_dicts_sync
from app.pipeline.fonts.repair import _sync_pdf_cid_cff_widths as _sync_pdf_cid_cff_widths
from app.pipeline.fonts.repair import _sync_pdf_cid_cff_widths_sync as _sync_pdf_cid_cff_widths_sync
from app.pipeline.fonts.resources import _is_form_xobject as _is_form_xobject
from app.pipeline.fonts.resources import _is_pdf_mapping as _is_pdf_mapping
from app.pipeline.fonts.resources import _obj_key as _obj_key
from app.pipeline.fonts.resources import _resolve_dictionary as _resolve_dictionary
from app.pipeline.fonts.resources import _walk_pdf_resource_graph as _walk_pdf_resource_graph
from app.pipeline.fonts.result import FontRemediation
from app.pipeline.fonts.rules import FONT_DICT_REPAIR_RULE_MARKERS as FONT_DICT_REPAIR_RULE_MARKERS
from app.pipeline.fonts.rules import FONT_EMBED_RULE_MARKERS as FONT_EMBED_RULE_MARKERS
from app.pipeline.fonts.rules import FONT_LANE_EMBED as FONT_LANE_EMBED
from app.pipeline.fonts.rules import FONT_LANE_OCR_FORCE as FONT_LANE_OCR_FORCE
from app.pipeline.fonts.rules import FONT_LANE_OCR_REDO as FONT_LANE_OCR_REDO
from app.pipeline.fonts.rules import FONT_LANE_REPAIR_DICTS as FONT_LANE_REPAIR_DICTS
from app.pipeline.fonts.rules import FONT_LANE_REPAIR_TOUNICODE as FONT_LANE_REPAIR_TOUNICODE
from app.pipeline.fonts.rules import FONT_RULE_FRAGMENT as FONT_RULE_FRAGMENT
from app.pipeline.fonts.rules import FONT_UNICODE_RULE_MARKERS as FONT_UNICODE_RULE_MARKERS
from app.pipeline.fonts.tounicode import _repair_pdf_tounicode as _repair_pdf_tounicode
from app.pipeline.fonts.tounicode import _repair_pdf_tounicode_sync as _repair_pdf_tounicode_sync
from app.pipeline.language import bcp47_to_tesseract
from app.pipeline.llm_operations import (
    await_llm_operation as _await_llm_operation,
)
from app.pipeline.llm_operations import (
    empty_llm_usage as _empty_llm_usage,
)
from app.pipeline.llm_operations import (
    llm_usage_snapshot as _llm_usage_snapshot,
)
from app.pipeline.llm_operations import (
    make_pretag_llm_client as _make_pretag_llm_client,
)
from app.pipeline.llm_operations import (
    pretag_llm_fallback_operation_timeout_seconds as _pretag_llm_fallback_operation_timeout_seconds,
)
from app.pipeline.llm_operations import (
    pretag_llm_operation_timeout_seconds as _pretag_llm_operation_timeout_seconds,
)
from app.pipeline.ocr import run_ocr
from app.pipeline.page_frame import PageFrame
from app.pipeline.pdfium_text import PdfiumText
from app.pipeline.structure import docling_table_cell_matching, extract_structure
from app.pipeline.tagger import TaggingResult, is_placeholder_alt_text, tag_pdf
from app.pipeline.validator import ValidationResult, validate_pdf
from app.services.applied_changes import add_applied_change
from app.services.auto_structure_apply import auto_apply_structure_tasks
from app.services.bookmark_intelligence import enhance_bookmark_structure_with_intelligence
from app.services.docling_escalation import (
    docling_pretag_ambiguity_router,
    docling_structure_escalation_plan,
)
from app.services.early_pages_intelligence import (
    enhance_document_title_and_front_matter_with_intelligence,
)
from app.services.file_storage import create_job_dir, get_output_path
from app.services.font_intelligence_auto import (
    attempt_auto_llm_font_map as _attempt_auto_llm_font_map,
)
from app.services.font_intelligence_auto import (
    fidelity_not_worse as _fidelity_not_worse,
)
from app.services.form_fields import (
    apply_field_accessible_names,
    extract_widget_fields,
    remove_widget_fields,
)
from app.services.grounded_text_apply import (
    PRETAG_GROUNDED_TEXT_TARGET_LIMIT,
)
from app.services.grounded_text_apply import (
    apply_grounded_text_resolutions_to_structure as _apply_grounded_text_resolutions_to_structure,
)
from app.services.grounded_text_apply import (
    collect_safe_grounded_text_resolutions as _collect_safe_grounded_text_resolutions,
)
from app.services.grounded_text_apply import (
    has_grounded_text_candidate_task as _has_grounded_text_candidate_task,
)
from app.services.grounded_text_intelligence import (
    adjudicate_grounded_text_candidates as _adjudicate_grounded_text_candidates,
)
from app.services.grounded_text_intelligence import (
    recalculate_fidelity_summary as _recalculate_fidelity_summary,
)
from app.services.intelligence_forms import (
    generate_form_intelligence,
    generate_form_intelligence_for_page,
)
from app.services.intelligence_pages import generate_suspicious_text_intelligence
from app.services.intelligence_tables import (
    generate_table_intelligence,
    generate_table_intelligence_for_page,
)
from app.services.intelligence_widgets import (
    generate_widget_intelligence,
    generate_widget_intelligence_for_page,
)
from app.services.job_manager import JobManager
from app.services.job_state import clear_terminal_artifacts
from app.services.llm_client import (
    job_model_access,
    make_llm_client,
    raise_if_model_access_refused,
    track_llm_usage,
)
from app.services.page_intelligence import collect_grounded_text_candidates
from app.services.review_surface import (
    filter_user_visible_review_tasks,
    is_user_visible_applied_change_type,
)
from app.services.semantic_pretag_policy import (
    apply_table_intelligence_to_element as _apply_table_intelligence_to_element,
)
from app.services.semantic_pretag_policy import (
    form_targets_for_intelligence as _form_targets_for_intelligence,
)
from app.services.semantic_pretag_policy import (
    should_auto_apply_form_intelligence as _should_auto_apply_form_intelligence,
)
from app.services.semantic_pretag_policy import (
    should_auto_apply_table_intelligence as _should_auto_apply_table_intelligence,
)
from app.services.semantic_pretag_policy import (
    should_auto_remove_widget as _should_auto_remove_widget,
)
from app.services.semantic_pretag_policy import (
    should_retry_table_intelligence_aggressively as _should_retry_table_intelligence_aggressively,
)
from app.services.semantic_pretag_policy import (
    should_retry_table_intelligence_confirm_existing as _should_retry_table_intelligence_confirm_existing,
)
from app.services.semantic_pretag_policy import (
    table_page_structure_fragments as _table_page_structure_fragments,
)
from app.services.semantic_pretag_policy import (
    table_targets_with_cells as _table_targets_with_cells,
)
from app.services.semantic_pretag_policy import (
    widget_targets_for_rationalization as _widget_targets_for_rationalization,
)
from app.services.title_intelligence import enhance_document_title_with_intelligence
from app.services.toc_intelligence import enhance_toc_structure_with_intelligence
from app.services.validation_compare import is_better_validation as _is_better_validation
from app.services.visual_figure_rationalization import synthesize_missing_visual_figures

logger = logging.getLogger(__name__)


SEMANTIC_COVERAGE_INTERESTING_TAGS = (
    "Document",
    "Part",
    "Sect",
    "Div",
    "P",
    "Span",
    "H",
    "H1",
    "H2",
    "H3",
    "H4",
    "H5",
    "H6",
    "L",
    "LI",
    "Lbl",
    "LBody",
    "Table",
    "THead",
    "TBody",
    "TFoot",
    "TR",
    "TH",
    "TD",
    "Figure",
    "Caption",
    "Reference",
    "BibEntry",
    "Note",
    "Formula",
    "Link",
    "TOC",
    "TOCI",
    "Quote",
    "BlockQuote",
)
FIGURE_RECLASSIFY_KINDS = {"table", "form_region", "artifact"}


def _accumulate_llm_usage(
    total: dict[str, float | int],
    delta: dict[str, float | int] | None,
) -> dict[str, float | int]:
    if not delta:
        return total
    total["request_count"] = int(total.get("request_count", 0) or 0) + int(
        delta.get("request_count", 0) or 0
    )
    total["prompt_tokens"] = int(total.get("prompt_tokens", 0) or 0) + int(
        delta.get("prompt_tokens", 0) or 0
    )
    total["completion_tokens"] = int(total.get("completion_tokens", 0) or 0) + int(
        delta.get("completion_tokens", 0) or 0
    )
    total["total_tokens"] = int(total.get("total_tokens", 0) or 0) + int(
        delta.get("total_tokens", 0) or 0
    )
    total["cost_usd"] = float(total.get("cost_usd", 0.0) or 0.0) + float(
        delta.get("cost_usd", 0.0) or 0.0
    )
    return total


def _alt_text_step_result(
    *,
    count: int,
    approved: int,
    rejected: int,
    reviewable_changes: int,
    reclassified: int,
    auto_approve_enabled: bool,
    llm_usage: dict[str, float | int] | None = None,
    reason: str | None = None,
    skipped_figure_count: int | None = None,
    figure_reclassification: dict[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "count": count,
        "approved": approved,
        "rejected": rejected,
        "reviewable_changes": reviewable_changes,
        "reclassified": reclassified,
        "auto_approve_enabled": auto_approve_enabled,
        "llm_usage": dict(llm_usage or _empty_llm_usage()),
    }
    if reason is not None:
        result["reason"] = reason
    if skipped_figure_count is not None:
        result["skipped_figure_count"] = skipped_figure_count
    if figure_reclassification is not None:
        result["figure_reclassification"] = figure_reclassification
    return result


def _aggregate_violations(violations) -> dict[str, dict]:
    """Aggregate violations by rule_id while preserving key display fields."""
    aggregated: dict[str, dict] = {}
    for v in violations:
        rule_id = str(getattr(v, "rule_id", "")).strip()
        if not rule_id:
            continue

        count = getattr(v, "count", 1)
        if not isinstance(count, int) or count < 1:
            count = 1

        if rule_id not in aggregated:
            aggregated[rule_id] = {
                "rule_id": rule_id,
                "description": getattr(v, "description", "Unknown violation"),
                "severity": getattr(v, "severity", "error"),
                "location": getattr(v, "location", None),
                "category": getattr(v, "category", None),
                "fix_hint": getattr(v, "fix_hint", None),
                "count": 0,
            }

        entry = aggregated[rule_id]
        entry["count"] += count
        if entry.get("severity") != "error" and getattr(v, "severity", "") == "error":
            entry["severity"] = "error"
        if not entry.get("location") and getattr(v, "location", None):
            entry["location"] = getattr(v, "location")
        if not entry.get("category") and getattr(v, "category", None):
            entry["category"] = getattr(v, "category")
        if not entry.get("fix_hint") and getattr(v, "fix_hint", None):
            entry["fix_hint"] = getattr(v, "fix_hint")
    return aggregated


def _build_validation_changes(
    baseline_violations,
    post_violations,
) -> tuple[list[dict], dict[str, str]]:
    """Build per-rule remediation lifecycle entries."""
    baseline_map = _aggregate_violations(baseline_violations)
    post_map = _aggregate_violations(post_violations)
    all_rule_ids = sorted(set(baseline_map) | set(post_map))

    changes: list[dict] = []
    status_by_rule: dict[str, str] = {}
    for rule_id in all_rule_ids:
        before = baseline_map.get(rule_id)
        after = post_map.get(rule_id)
        remediation_status = "needs_remediation" if after else "auto_remediated"
        status_by_rule[rule_id] = remediation_status
        source = after or before or {}

        changes.append(
            {
                "rule_id": rule_id,
                "description": source.get("description", "Unknown violation"),
                "severity": source.get("severity", "error"),
                "location": source.get("location"),
                "category": source.get("category"),
                "fix_hint": source.get("fix_hint"),
                "baseline_count": before.get("count", 0) if before else 0,
                "post_count": after.get("count", 0) if after else 0,
                "remediation_status": remediation_status,
            }
        )

    return changes, status_by_rule


def _collect_semantic_coverage(
    node: Any,
    *,
    tag_counts: dict[str, int],
    list_numbering: dict[str, int],
) -> None:
    if isinstance(node, pikepdf.Dictionary):
        struct_type = node.get("/S")
        if struct_type is not None:
            tag = str(struct_type).lstrip("/")
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
            if tag == "L":
                attrs = node.get("/A")
                if isinstance(attrs, pikepdf.Dictionary):
                    numbering = attrs.get("/ListNumbering")
                    if numbering is not None:
                        name = str(numbering).lstrip("/")
                        list_numbering[name] = list_numbering.get(name, 0) + 1

        _collect_semantic_coverage(
            node.get("/K"),
            tag_counts=tag_counts,
            list_numbering=list_numbering,
        )
    elif isinstance(node, pikepdf.Array):
        for child in node:
            _collect_semantic_coverage(
                child,
                tag_counts=tag_counts,
                list_numbering=list_numbering,
            )


def _semantic_coverage_summary(pdf_path: Path | str | None) -> dict[str, object]:
    if pdf_path is None:
        return {"available": False, "reason": "missing_output_path"}

    tag_counts: dict[str, int] = {}
    list_numbering: dict[str, int] = {}
    try:
        with pikepdf.open(str(pdf_path)) as pdf:
            struct_root = pdf.Root.get("/StructTreeRoot")
            if struct_root is None:
                return {"available": False, "reason": "missing_struct_tree"}
            _collect_semantic_coverage(
                struct_root,
                tag_counts=tag_counts,
                list_numbering=list_numbering,
            )
    except Exception as exc:
        logger.warning("Failed to collect semantic coverage for %s: %s", pdf_path, exc)
        return {"available": False, "reason": "inspection_failed", "error": str(exc)}

    heading_tags = {
        tag: tag_counts[tag]
        for tag in ("H", "H1", "H2", "H3", "H4", "H5", "H6")
        if tag_counts.get(tag, 0)
    }
    illegal_heading_tags = {
        tag: count for tag, count in tag_counts.items() if re.fullmatch(r"H(?:[7-9]|\d{2,})", tag)
    }
    interesting_tags = {
        tag: tag_counts[tag] for tag in SEMANTIC_COVERAGE_INTERESTING_TAGS if tag_counts.get(tag, 0)
    }

    return {
        "available": True,
        "total_struct_elems": sum(tag_counts.values()),
        "tag_counts": dict(sorted(tag_counts.items())),
        "interesting_tags": interesting_tags,
        "heading_tags": heading_tags,
        "illegal_heading_tags": illegal_heading_tags,
        "list_numbering": dict(sorted(list_numbering.items())),
    }


def _violation_weight(violation) -> int:
    count = getattr(violation, "count", 1)
    if isinstance(count, int) and count > 0:
        return count
    return 1


def _error_count(validation) -> int:
    return sum(_violation_weight(v) for v in validation.violations if v.severity == "error")


def _warning_count(validation) -> int:
    return sum(_violation_weight(v) for v in validation.violations if v.severity != "error")


def _build_validation_payload(
    *,
    baseline_validation,
    selected_validation,
    settings: Settings,
    font_remediation: FontRemediation,
    tagging_result,
    llm_font_map_auto: dict[str, object] | None = None,
) -> dict[str, object]:
    baseline_has_verapdf_report = bool(baseline_validation.raw_report.get("report"))
    baseline_validator_name = (
        "veraPDF"
        if baseline_has_verapdf_report
        else baseline_validation.raw_report.get("validator", "unknown")
    )
    baseline_errors = _error_count(baseline_validation)
    baseline_warnings = _warning_count(baseline_validation)

    has_verapdf_report = bool(selected_validation.raw_report.get("report"))
    validator_name = (
        "veraPDF"
        if has_verapdf_report
        else selected_validation.raw_report.get("validator", "unknown")
    )
    post_errors = _error_count(selected_validation)
    post_warnings = _warning_count(selected_validation)

    changes, status_by_rule = _build_validation_changes(
        baseline_validation.violations,
        selected_validation.violations,
    )
    needs_remediation = len([c for c in changes if c["remediation_status"] == "needs_remediation"])
    auto_remediated = len([c for c in changes if c["remediation_status"] == "auto_remediated"])
    manual_remediated = len([c for c in changes if c["remediation_status"] == "manual_remediated"])

    error_changes = [c for c in changes if c.get("severity") == "error"]
    baseline_error_rules = len([c for c in error_changes if c["baseline_count"] > 0])
    post_error_rules = len([c for c in error_changes if c["post_count"] > 0])
    auto_remediated_errors = len(
        [c for c in error_changes if c["remediation_status"] == "auto_remediated"]
    )

    remediation_payload: dict[str, object] = {
        "needs_remediation": needs_remediation,
        "auto_remediated": auto_remediated,
        "manual_remediated": manual_remediated,
        "baseline_errors": baseline_errors,
        "baseline_warnings": baseline_warnings,
        "post_errors": post_errors,
        "post_warnings": post_warnings,
        "errors_reduced": baseline_errors - post_errors,
        "warnings_reduced": baseline_warnings - post_warnings,
        "baseline_error_rules": baseline_error_rules,
        "post_error_rules": post_error_rules,
        "auto_remediated_errors": auto_remediated_errors,
        "font_remediation": font_remediation.to_dict(),
    }
    if isinstance(llm_font_map_auto, dict):
        remediation_payload["llm_font_map_auto"] = llm_font_map_auto

    claims_payload: dict[str, object] = {
        "automated_validation_only": True,
        "requires_manual_check_for_reading_experience": True,
    }
    if isinstance(llm_font_map_auto, dict):
        claims_payload["llm_font_map_auto_attempted"] = bool(llm_font_map_auto.get("attempted"))
        if bool(llm_font_map_auto.get("applied")):
            claims_payload["llm_assisted_auto_remediation"] = True

    return {
        "compliant": selected_validation.compliant,
        "profile": settings.verapdf_flavour,
        "standard": "PDF/UA",
        "validator": validator_name,
        "generated_at": datetime.now(UTC).isoformat(),
        "baseline": {
            "compliant": baseline_validation.compliant,
            "validator": baseline_validator_name,
            "violations_count": len(baseline_validation.violations),
            "summary": {
                "errors": baseline_errors,
                "warnings": baseline_warnings,
            },
        },
        "violations": [
            {
                "rule_id": v.rule_id,
                "description": v.description,
                "severity": v.severity,
                "location": v.location,
                "count": v.count,
                "category": v.category,
                "fix_hint": v.fix_hint,
                "remediation_status": status_by_rule.get(v.rule_id, "needs_remediation"),
            }
            for v in selected_validation.violations
        ],
        "summary": {
            "passed": len([v for v in selected_validation.violations if v.severity != "error"]),
            "failed": len([v for v in selected_validation.violations if v.severity == "error"]),
            "errors": post_errors,
            "warnings": post_warnings,
        },
        "changes": changes,
        "remediation": remediation_payload,
        "tagging": {
            "headings_tagged": tagging_result.headings_tagged,
            "figures_tagged": tagging_result.figures_tagged,
            "decorative_figures_artifacted": tagging_result.decorative_figures_artifacted,
            "tables_tagged": tagging_result.tables_tagged,
            "lists_tagged": tagging_result.lists_tagged,
            "links_tagged": tagging_result.links_tagged,
            "bookmarks_added": tagging_result.bookmarks_added,
            "title_set": tagging_result.title_set,
            "reading_order_diagnostics": getattr(tagging_result, "reading_order_diagnostics", []),
            "lang_set": tagging_result.lang_set,
        },
        "semantic_coverage": _semantic_coverage_summary(
            getattr(tagging_result, "output_path", None)
        ),
        "claims": claims_payload,
    }


def _blocking_task_count(review_tasks: list[dict[str, object]]) -> int:
    return len([task for task in review_tasks if bool(task.get("blocking"))])


def _grounded_text_confirmed_block_count(
    review_tasks: list[dict[str, object]],
) -> int:
    confirmed_count = 0
    for task in review_tasks:
        if not bool(task.get("blocking")):
            continue
        if str(task.get("task_type") or "") != "content_fidelity":
            continue
        if str(task.get("source") or "fidelity") != "fidelity":
            continue
        metadata = task.get("metadata")
        if not isinstance(metadata, dict) or not bool(metadata.get("grounded_text_candidate")):
            continue
        grounded_target_count = metadata.get("grounded_target_count")
        if isinstance(grounded_target_count, int):
            confirmed_count += max(0, grounded_target_count)
            continue
        flagged_blocks = metadata.get("flagged_blocks")
        if isinstance(flagged_blocks, list):
            confirmed_count += len(flagged_blocks)
            continue
        confirmed_count += 1
    return confirmed_count


def _grounded_text_retry_improved(
    current_review_tasks: list[dict[str, object]],
    retry_review_tasks: list[dict[str, object]],
) -> bool:
    current_blocking_count = _blocking_task_count(current_review_tasks)
    retry_blocking_count = _blocking_task_count(retry_review_tasks)
    if retry_blocking_count < current_blocking_count:
        return True
    if retry_blocking_count > current_blocking_count:
        return False
    return _grounded_text_confirmed_block_count(
        retry_review_tasks
    ) < _grounded_text_confirmed_block_count(current_review_tasks)


def _prepare_user_review_surface(
    review_tasks: list[dict[str, object]],
    fidelity_report: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    # Fidelity and terminal status stay tied to the full pipeline backlog.
    updated_fidelity_report = _recalculate_fidelity_summary(fidelity_report, review_tasks)
    user_visible_review_tasks = filter_user_visible_review_tasks(review_tasks)
    return user_visible_review_tasks, updated_fidelity_report


def _merge_review_task_metadata(
    review_tasks: list[dict[str, object]],
    *,
    task_type: str,
    source: str,
    metadata: dict[str, object],
) -> list[dict[str, object]]:
    if not metadata:
        return review_tasks
    for task in review_tasks:
        if (
            str(task.get("task_type") or "") == task_type
            and str(task.get("source") or "fidelity") == source
        ):
            current = task.get("metadata", {})
            if not isinstance(current, dict):
                current = {}
            task["metadata"] = {**current, **metadata}
            break
    return review_tasks


def _alt_text_entry_state(
    alt,
    *,
    caption: str | None,
    auto_approve: bool,
) -> tuple[str, str]:
    """Return the (status, text) to store for one figure's generated alt text.

    An empty or placeholder description is never approved: the figure keeps
    no alt text and stays "pending_review" until a person describes it, unless
    its visible caption can stand in. A figure the model judged a table or
    form region but that was kept (see _apply_figure_reclassification) always
    waits for a person: a caption names a table, it does not read it.
    """
    generated_text = (alt.generated_text or "").strip()
    status = alt.status
    caption_fallback = (caption or "").strip()

    if auto_approve and generated_text.lower() == "decorative":
        return "rejected", ""
    if status == "rejected":
        return status, generated_text
    if status == "pending_review" and getattr(alt, "resolved_kind", None) in (
        "table",
        "form_region",
    ):
        return "pending_review", ""
    if is_placeholder_alt_text(generated_text):
        if caption_fallback and not is_placeholder_alt_text(caption_fallback):
            return "approved", caption_fallback
        return "pending_review", ""
    if auto_approve:
        return "approved", generated_text
    return status, generated_text


# A reclassified figure's region is covered by a Docling table only when the
# table holds this share of the figure's box ...
RECLASSIFIED_FIGURE_TABLE_CONTAINMENT = 0.8
# ... and this share of the table's words is real text inside the box: the
# tagger tags a table from the text drawn on the page, never from pixels.
RECLASSIFIED_FIGURE_TABLE_TEXT_SHARE = 0.5
_WORD_RE = re.compile(r"\w+")


def _box_containment(inner: dict, outer: dict) -> float:
    """The share of ``inner``'s area inside ``outer`` (boxes l, b, r, t)."""
    try:
        il, ir = sorted((float(inner["l"]), float(inner["r"])))
        ib, it = sorted((float(inner["b"]), float(inner["t"])))
        ol, orr = sorted((float(outer["l"]), float(outer["r"])))
        ob, ot = sorted((float(outer["b"]), float(outer["t"])))
    except (KeyError, TypeError, ValueError):
        return 0.0
    area = (ir - il) * (it - ib)
    if area <= 0:
        return 0.0
    width = max(0.0, min(ir, orr) - max(il, ol))
    height = max(0.0, min(it, ot) - max(ib, ob))
    return width * height / area


def _reclassified_figure_cover(
    figure: dict,
    kind: str,
    elements: list[object],
    working_pdf: Path | None,
) -> str | None:
    """What carries a reclassified figure's content once the figure is gone.

    Deleting the figure turns its pixels into an artifact, so its content
    survives only in what the tagger tags in the same region: the text drawn
    there, as the cells of a Docling table that holds the region, or the form
    fields there (widgets are tagged as Form). Returns "table" or "form_field",
    or None when nothing tagged covers the region (or it cannot be checked).
    """
    page = figure.get("page")
    bbox = figure.get("bbox")
    if working_pdf is None or not isinstance(page, int) or not isinstance(bbox, dict):
        return None
    tables = [
        element
        for element in elements
        if isinstance(element, dict)
        and element.get("type") == "table"
        and element.get("page") == page
        and isinstance(element.get("bbox"), dict)
        and _box_containment(bbox, element["bbox"]) >= RECLASSIFIED_FIGURE_TABLE_CONTAINMENT
    ]
    try:
        with PdfiumText(working_pdf) as pdfium_text:
            if page >= len(pdfium_text.document):
                return None
            user_box = PageFrame.of(pdfium_text.document[page]).to_user_space(bbox)
            region_words = set(_WORD_RE.findall(pdfium_text.in_boxes(page, [user_box]).lower()))
        if kind == "form_region":
            for field in extract_widget_fields(working_pdf):
                field_box = field.get("bbox")
                if (
                    field.get("page") == page + 1
                    and isinstance(field_box, dict)
                    and _box_containment(field_box, user_box) >= 0.5
                ):
                    return "form_field"
    except Exception:
        logger.warning("Could not check what covers a reclassified figure", exc_info=True)
        return None
    for table in tables:
        cell_words = [
            word
            for cell in table.get("cells") or []
            if isinstance(cell, dict)
            for word in _WORD_RE.findall(str(cell.get("text") or "").lower())
        ]
        if cell_words and (
            sum(1 for word in cell_words if word in region_words) / len(cell_words)
            >= RECLASSIFIED_FIGURE_TABLE_TEXT_SHARE
        ):
            return "table"
    return None


def _apply_figure_reclassification(
    structure_json: dict[str, object],
    alt_texts: list[AltTextResult],
    *,
    working_pdf: Path | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Act on the figures the model reclassified.

    A figure judged a table or part of a form is removed only when a tagged
    table or form field covers its region (see _reclassified_figure_cover).
    Otherwise it stays a figure with no description, which holds the document
    for a person. A figure judged an artifact is kept and marked decorative,
    as the model's mark_decorative decision is, so a person can review it.
    The kept figures' results in ``alt_texts`` are updated in place.
    """
    audit: dict[str, object] = {
        "attempted": False,
        "applied": False,
        "reason": "",
        "candidate_count": 0,
        "removed_count": 0,
        "removed_indexes": [],
        "removed_kinds": {},
        "covered_by": {},
        "kept_for_review_indexes": [],
        "decorative_indexes": [],
        "pages": [],
        "artifact_pages": [],
    }
    if not isinstance(structure_json, dict):
        audit["reason"] = "missing_structure"
        return structure_json, audit

    reclassified: dict[int, AltTextResult] = {}
    for item in alt_texts:
        figure_index = getattr(item, "figure_index", None)
        status = str(getattr(item, "status", "") or "").strip()
        resolved_kind = str(getattr(item, "resolved_kind", "") or "").strip()
        if (
            isinstance(figure_index, int)
            and status == "reclassified"
            and resolved_kind in FIGURE_RECLASSIFY_KINDS
        ):
            reclassified[figure_index] = item

    if not reclassified:
        audit["reason"] = "no_candidates"
        return structure_json, audit

    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        audit["reason"] = "missing_elements"
        return structure_json, audit

    audit["attempted"] = True
    audit["candidate_count"] = len(reclassified)
    updated = copy.deepcopy(structure_json)
    updated_elements: list[object] = []
    removed_indexes: list[int] = []
    removed_pages: set[int] = set()
    removed_kinds: dict[str, int] = {}
    covered_by: dict[str, str] = {}
    kept_for_review: list[int] = []
    decorative: list[int] = []
    artifact_pages: set[int] = set()
    for element in elements:
        if not isinstance(element, dict):
            updated_elements.append(element)
            continue
        if element.get("type") == "figure" and isinstance(element.get("figure_index"), int):
            figure_index = int(element["figure_index"])
            result = reclassified.get(figure_index)
            if result is not None:
                resolved_kind = str(result.resolved_kind)
                page = element.get("page")
                if resolved_kind == "artifact":
                    if (
                        isinstance(page, int)
                        and page >= 0
                        and bool(element.get("synthetic_figure"))
                    ):
                        artifact_pages.add(page + 1)
                    result.status = "rejected"
                    result.generated_text = ""
                    result.suggested_action = "mark_decorative"
                    result.reviewable = True
                    decorative.append(figure_index)
                    updated_elements.append(element)
                    continue
                cover = _reclassified_figure_cover(element, resolved_kind, elements, working_pdf)
                if cover is None:
                    result.status = "pending_review"
                    result.generated_text = ""
                    result.suggested_action = "manual_only"
                    result.reviewable = True
                    result.importance = "high"
                    kept_for_review.append(figure_index)
                    updated_elements.append(element)
                    continue
                removed_indexes.append(figure_index)
                covered_by[str(figure_index)] = cover
                if isinstance(page, int) and page >= 0:
                    removed_pages.add(page + 1)
                removed_kinds[resolved_kind] = removed_kinds.get(resolved_kind, 0) + 1
                continue
        updated_elements.append(element)

    audit["removed_count"] = len(removed_indexes)
    audit["removed_indexes"] = sorted(removed_indexes)
    audit["removed_kinds"] = removed_kinds
    audit["covered_by"] = covered_by
    audit["kept_for_review_indexes"] = sorted(kept_for_review)
    audit["decorative_indexes"] = sorted(decorative)
    audit["pages"] = sorted(removed_pages)
    audit["artifact_pages"] = sorted(artifact_pages)
    if not removed_indexes and not artifact_pages:
        audit["reason"] = (
            "kept_figures" if kept_for_review or decorative else "no_matching_structure_elements"
        )
        return structure_json, audit

    updated["elements"] = updated_elements
    if artifact_pages:
        existing_ignored = updated.get("visual_meaning_ignored_pages")
        ignored_pages: set[int] = set()
        if isinstance(existing_ignored, list):
            ignored_pages.update(
                int(page) for page in existing_ignored if isinstance(page, int) and page > 0
            )
        ignored_pages.update(artifact_pages)
        updated["visual_meaning_ignored_pages"] = sorted(ignored_pages)
    audit["applied"] = True
    audit["reason"] = "applied"
    return updated, audit


async def _apply_pretag_grounded_text_resolutions(
    *,
    job: Job,
    settings: Settings,
    working_pdf: Path,
    structure_json: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    audit: dict[str, object] = {
        "enabled": bool(settings.auto_apply_grounded_text),
        "attempted": False,
        "applied": False,
        "reason": "",
        "candidate_count": 0,
        "applied_count": 0,
        "applied_actual_text_count": 0,
        "applied_code_text_count": 0,
        "applied_artifact_count": 0,
        "pages": [],
        "review_ids": [],
        "llm_usage": _empty_llm_usage(),
    }
    if not settings.auto_apply_grounded_text:
        audit["reason"] = "disabled"
        return structure_json, audit
    if not isinstance(structure_json, dict):
        audit["reason"] = "missing_structure"
        return structure_json, audit

    grounded = collect_grounded_text_candidates(
        working_pdf,
        structure_json,
        target_limit=PRETAG_GROUNDED_TEXT_TARGET_LIMIT,
    )
    suspicious_blocks = grounded.get("targets")
    if not isinstance(suspicious_blocks, list) or not suspicious_blocks:
        audit["reason"] = "no_candidates"
        return structure_json, audit
    pages_to_check = sorted(
        {
            int(block["page"])
            for block in suspicious_blocks
            if isinstance(block, dict) and isinstance(block.get("page"), int)
        }
    )
    if not pages_to_check:
        audit["reason"] = "no_candidate_pages"
        return structure_json, audit

    audit["attempted"] = True
    audit["candidate_count"] = len(suspicious_blocks)

    llm_client = _make_pretag_llm_client(settings)
    pretag_timeout = _pretag_llm_operation_timeout_seconds(settings)
    with track_llm_usage() as usage:
        try:
            adjudication = await _await_llm_operation(
                settings=settings,
                label="grounded_text_intelligence",
                timeout_seconds=pretag_timeout,
                awaitable=generate_suspicious_text_intelligence(
                    job=job,
                    page_numbers=pages_to_check,
                    suspicious_blocks=suspicious_blocks,
                    llm_client=llm_client,
                ),
            )
        except Exception as exc:
            audit["reason"] = f"llm_failed: {exc}"
            return structure_json, audit
        finally:
            audit["llm_usage"] = _llm_usage_snapshot(usage)
            await llm_client.close()

    approved_by_key = _collect_safe_grounded_text_resolutions(adjudication)
    updated_structure, apply_audit = _apply_grounded_text_resolutions_to_structure(
        structure_json,
        approved_by_key,
    )
    audit.update(apply_audit)
    return updated_structure, audit


async def _apply_pretag_table_intelligence(
    *,
    job: Job,
    settings: Settings,
    structure_json: dict[str, object],
    targets: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    audit: dict[str, object] = {
        "enabled": bool(settings.auto_apply_table_intelligence),
        "attempted": False,
        "applied": False,
        "reason": "",
        "candidate_count": 0,
        "applied_count": 0,
        "page_batch_count": 0,
        "page_batch_resolved_count": 0,
        "page_batch_failed_count": 0,
        "individual_fallback_count": 0,
        "pages": [],
        "review_ids": [],
        "confirmed_count": 0,
        "set_headers_count": 0,
        "aggressive_retry_count": 0,
        "confirm_existing_retry_count": 0,
        "llm_usage": _empty_llm_usage(),
    }
    if not settings.auto_apply_table_intelligence:
        audit["reason"] = "disabled"
        return structure_json, audit
    if not isinstance(structure_json, dict):
        audit["reason"] = "missing_structure"
        return structure_json, audit

    precomputed_targets = targets
    if precomputed_targets is None:
        table_risk = _table_semantics_risk(structure_json)
        targets = table_risk.get("targets")
    else:
        targets = precomputed_targets
    if not isinstance(targets, list) or not targets:
        audit["reason"] = "no_candidates"
        return structure_json, audit
    if precomputed_targets is None:
        detailed_targets = _table_targets_with_cells(
            structure_json,
            {
                str(target.get("table_review_id") or "").strip()
                for target in targets
                if isinstance(target, dict)
            },
        )
    else:
        detailed_targets = {
            str(target.get("table_review_id") or "").strip(): dict(target)
            for target in targets
            if isinstance(target, dict) and str(target.get("table_review_id") or "").strip()
        }
    audit["attempted"] = True
    audit["candidate_count"] = len(targets)

    llm_client = _make_pretag_llm_client(settings)
    pretag_timeout = _pretag_llm_operation_timeout_seconds(settings)
    fallback_timeout = _pretag_llm_fallback_operation_timeout_seconds(settings)
    with track_llm_usage() as usage:
        try:
            intelligence_items: list[dict[str, object]] = []
            targets_by_review_id = {
                str(target.get("table_review_id") or "").strip(): target
                for target in targets
                if isinstance(target, dict) and str(target.get("table_review_id") or "").strip()
            }
            detailed_targets_by_review_id: dict[str, dict[str, object]] = {}
            targets_by_page: dict[int, list[dict[str, object]]] = {}
            page_fragments_by_page: dict[int, list[dict[str, object]]] = {}
            for target in targets:
                if not isinstance(target, dict):
                    continue
                review_id = str(target.get("table_review_id") or "").strip()
                if not review_id:
                    continue
                page = target.get("page")
                page_number = int(page) if isinstance(page, int) else None
                page_fragments = _table_page_structure_fragments(
                    structure_json,
                    page_numbers=[page_number] if page_number is not None else [],
                )
                detailed_target = dict(detailed_targets.get(review_id) or target)
                if isinstance(target.get("risk_reasons"), list):
                    detailed_target["risk_reasons"] = list(target.get("risk_reasons") or [])
                if isinstance(target.get("risk_score"), (int, float)):
                    detailed_target["risk_score"] = float(target.get("risk_score"))
                detailed_targets_by_review_id[review_id] = detailed_target
                if page_number is None or page_number <= 0:
                    continue
                targets_by_page.setdefault(page_number, []).append(detailed_target)
                if page_number not in page_fragments_by_page:
                    page_fragments_by_page[page_number] = page_fragments

            audit["page_batch_count"] = len(targets_by_page)
            page_items_by_id: dict[str, dict[str, object]] = {}
            for page_number, page_targets in sorted(targets_by_page.items()):
                try:
                    page_result = await _await_llm_operation(
                        settings=settings,
                        label=f"table_page_intelligence[{page_number}]",
                        timeout_seconds=pretag_timeout,
                        awaitable=generate_table_intelligence_for_page(
                            job=job,
                            page_number=page_number,
                            targets=page_targets,
                            page_structure_fragments=page_fragments_by_page.get(page_number) or [],
                            llm_client=llm_client,
                        ),
                    )
                except Exception:
                    audit["page_batch_failed_count"] = (
                        int(audit.get("page_batch_failed_count", 0)) + 1
                    )
                    continue
                page_items = page_result
                for item in page_items:
                    if not isinstance(item, dict):
                        continue
                    review_id = str(item.get("table_review_id") or "").strip()
                    if review_id:
                        page_items_by_id[review_id] = item
                    intelligence_items.append(item)

            resolved_page_results = sum(
                1
                for item in page_items_by_id.values()
                if _should_auto_apply_table_intelligence(item)
            )
            audit["page_batch_resolved_count"] = resolved_page_results

            unresolved_targets = [
                target
                for review_id, target in detailed_targets_by_review_id.items()
                if not _should_auto_apply_table_intelligence(page_items_by_id.get(review_id, {}))
            ]
            audit["individual_fallback_count"] = len(unresolved_targets)
            for target in unresolved_targets:
                review_id = str(target.get("table_review_id") or "").strip()
                page = target.get("page")
                page_number = int(page) if isinstance(page, int) else None
                intelligence = await _await_llm_operation(
                    settings=settings,
                    label=f"table_intelligence[{review_id or '?'}]",
                    timeout_seconds=fallback_timeout,
                    awaitable=generate_table_intelligence(
                        job=job,
                        target=target,
                        page_structure_fragments=page_fragments_by_page.get(page_number or -1)
                        or [],
                        llm_client=llm_client,
                    ),
                )
                page_items_by_id[review_id] = intelligence

            intelligence_items = list(page_items_by_id.values())
            retried = 0
            for idx, item in enumerate(list(intelligence_items)):
                if not isinstance(item, dict):
                    continue
                review_id = str(item.get("table_review_id") or "").strip()
                target = targets_by_review_id.get(review_id)
                if not _should_retry_table_intelligence_aggressively(target or {}, item):
                    continue
                if not target:
                    continue
                detailed_target = detailed_targets_by_review_id.get(review_id) or dict(
                    detailed_targets.get(review_id) or target
                )
                page = detailed_target.get("page")
                page_number = int(page) if isinstance(page, int) else None
                retried_item = await _await_llm_operation(
                    settings=settings,
                    label=f"table_intelligence[{review_id or '?'}].aggressive",
                    timeout_seconds=fallback_timeout,
                    awaitable=generate_table_intelligence(
                        job=job,
                        target=detailed_target,
                        page_structure_fragments=page_fragments_by_page.get(page_number or -1)
                        or [],
                        llm_client=llm_client,
                        aggressive=True,
                    ),
                )
                intelligence_items[idx] = retried_item
                retried += 1
            audit["aggressive_retry_count"] = retried
            confirm_existing_retried = 0
            for idx, item in enumerate(list(intelligence_items)):
                if not isinstance(item, dict):
                    continue
                review_id = str(item.get("table_review_id") or "").strip()
                target = targets_by_review_id.get(review_id)
                if not _should_retry_table_intelligence_confirm_existing(target or {}, item):
                    continue
                if not target:
                    continue
                detailed_target = detailed_targets_by_review_id.get(review_id) or dict(
                    detailed_targets.get(review_id) or target
                )
                page = detailed_target.get("page")
                page_number = int(page) if isinstance(page, int) else None
                retried_item = await _await_llm_operation(
                    settings=settings,
                    label=f"table_intelligence[{review_id or '?'}].confirm_existing",
                    timeout_seconds=fallback_timeout,
                    awaitable=generate_table_intelligence(
                        job=job,
                        target=detailed_target,
                        page_structure_fragments=page_fragments_by_page.get(page_number or -1)
                        or [],
                        llm_client=llm_client,
                        aggressive=True,
                        confirm_existing=True,
                    ),
                )
                intelligence_items[idx] = retried_item
                confirm_existing_retried += 1
            audit["confirm_existing_retry_count"] = confirm_existing_retried
        except Exception as exc:
            audit["reason"] = f"llm_failed: {exc}"
            return structure_json, audit
        finally:
            audit["llm_usage"] = _llm_usage_snapshot(usage)
            await llm_client.close()

    approved_by_id: dict[str, dict[str, object]] = {}
    for item in intelligence_items:
        if not isinstance(item, dict) or not _should_auto_apply_table_intelligence(item):
            continue
        table_review_id = str(item.get("table_review_id") or "").strip()
        if table_review_id:
            approved_by_id[table_review_id] = item
    if not approved_by_id:
        audit["reason"] = "no_safe_resolutions"
        return structure_json, audit

    updated_structure = copy.deepcopy(structure_json)
    elements = updated_structure.get("elements")
    if not isinstance(elements, list):
        audit["reason"] = "missing_elements"
        return structure_json, audit

    applied_pages: set[int] = set()
    applied_review_ids: list[str] = []
    applied_count = 0
    confirmed_count = 0
    set_headers_count = 0
    for index, element in enumerate(elements):
        if not isinstance(element, dict) or element.get("type") != "table":
            continue
        review_id = str(element.get("review_id") or f"review-{index}").strip()
        approved = approved_by_id.get(review_id)
        if not approved:
            continue
        action = str(approved.get("suggested_action") or "").strip()
        if not _apply_table_intelligence_to_element(
            element,
            action=action,
            header_rows=list(approved.get("header_rows") or []),
            row_header_columns=list(approved.get("row_header_columns") or []),
        ):
            continue
        element["review_id"] = review_id
        element["table_llm_confirmed"] = True
        element["table_llm_confidence"] = str(approved.get("confidence") or "").strip() or "high"
        element["table_llm_action"] = action
        element["table_llm_reason"] = str(approved.get("reason") or "").strip()
        element["table_llm_summary"] = str(approved.get("summary") or "").strip()
        page_number = element.get("page")
        if isinstance(page_number, int) and page_number >= 0:
            applied_pages.add(page_number + 1)
        applied_review_ids.append(review_id)
        applied_count += 1
        if action == "confirm_current_headers":
            confirmed_count += 1
        elif action == "set_table_headers":
            set_headers_count += 1

    if applied_count <= 0:
        audit["reason"] = "no_matching_structure_elements"
        return structure_json, audit

    audit["applied"] = True
    audit["applied_count"] = applied_count
    audit["confirmed_count"] = confirmed_count
    audit["set_headers_count"] = set_headers_count
    audit["pages"] = sorted(applied_pages)
    audit["review_ids"] = applied_review_ids
    audit["reason"] = "applied"
    return updated_structure, audit


def _preview_job_for_pdf(job: Job, pdf_path: Path) -> Job:
    preview_job = copy.copy(job)
    preview_job.input_path = str(pdf_path)
    preview_job.output_path = str(pdf_path)
    return preview_job


async def _apply_pretag_widget_rationalization(
    *,
    job: Job,
    settings: Settings,
    working_pdf: Path,
    structure_json: dict[str, object],
    targets: list[dict[str, object]] | None = None,
) -> tuple[Path, dict[str, object]]:
    audit: dict[str, object] = {
        "enabled": bool(settings.auto_apply_form_intelligence),
        "attempted": False,
        "applied": False,
        "reason": "",
        "candidate_count": 0,
        "applied_count": 0,
        "page_batch_count": 0,
        "page_batch_resolved_count": 0,
        "page_batch_failed_count": 0,
        "individual_fallback_count": 0,
        "individual_fallback_failed_count": 0,
        "pages": [],
        "review_ids": [],
        "llm_usage": _empty_llm_usage(),
    }
    if not settings.auto_apply_form_intelligence:
        audit["reason"] = "disabled"
        return working_pdf, audit
    if not working_pdf.exists():
        audit["reason"] = "missing_pdf"
        return working_pdf, audit
    if not isinstance(structure_json, dict):
        audit["reason"] = "missing_structure"
        return working_pdf, audit

    if targets is None:
        targets = _widget_targets_for_rationalization(
            working_pdf=working_pdf,
            structure_json=structure_json,
        )
    if not targets:
        audit["reason"] = "no_candidates"
        return working_pdf, audit

    audit["attempted"] = True
    audit["candidate_count"] = len(targets)
    preview_job = _preview_job_for_pdf(job, working_pdf)
    llm_client = _make_pretag_llm_client(settings)
    pretag_timeout = _pretag_llm_operation_timeout_seconds(settings)
    fallback_timeout = _pretag_llm_fallback_operation_timeout_seconds(settings)
    with track_llm_usage() as usage:
        try:
            semaphore = asyncio.Semaphore(max(1, settings.llm_max_concurrency))
            targets_by_review_id = {
                str(target.get("field_review_id") or "").strip(): target
                for target in targets
                if str(target.get("field_review_id") or "").strip()
            }
            targets_by_page: dict[int, list[dict[str, object]]] = {}
            for target in targets:
                page = target.get("page")
                if not isinstance(page, int) or page <= 0:
                    continue
                targets_by_page.setdefault(page, []).append(target)
            audit["page_batch_count"] = len(targets_by_page)

            async def _generate(target: dict[str, object]) -> dict[str, object]:
                async with semaphore:
                    review_id = str(target.get("field_review_id") or "?").strip() or "?"
                    return await _await_llm_operation(
                        settings=settings,
                        label=f"widget_intelligence[{review_id}]",
                        timeout_seconds=fallback_timeout,
                        awaitable=generate_widget_intelligence(
                            job=preview_job,
                            target=target,
                            llm_client=llm_client,
                        ),
                    )

            async def _generate_page(
                page_number: int, page_targets: list[dict[str, object]]
            ) -> list[dict[str, object]]:
                async with semaphore:
                    return await _await_llm_operation(
                        settings=settings,
                        label=f"widget_page_intelligence[{page_number}]",
                        timeout_seconds=pretag_timeout,
                        awaitable=generate_widget_intelligence_for_page(
                            job=preview_job,
                            page_number=page_number,
                            targets=page_targets,
                            llm_client=llm_client,
                        ),
                    )

            intelligence_items: list[dict[str, object]] = []
            page_items_by_id: dict[str, dict[str, object]] = {}
            for page_number, page_targets in sorted(targets_by_page.items()):
                try:
                    page_result = await _generate_page(page_number, page_targets)
                except Exception:
                    audit["page_batch_failed_count"] = (
                        int(audit.get("page_batch_failed_count", 0)) + 1
                    )
                    continue
                page_items = page_result
                for item in page_items:
                    if not isinstance(item, dict):
                        continue
                    review_id = str(item.get("field_review_id") or "").strip()
                    if review_id:
                        page_items_by_id[review_id] = item
                    intelligence_items.append(item)

            resolved_page_results = sum(
                1
                for item in page_items_by_id.values()
                if str(item.get("suggested_action") or "").strip()
                in {"preserve_control", "remove_static_widget"}
            )
            audit["page_batch_resolved_count"] = resolved_page_results

            unresolved_targets = [
                target
                for review_id, target in targets_by_review_id.items()
                if str(page_items_by_id.get(review_id, {}).get("suggested_action") or "").strip()
                not in {"preserve_control", "remove_static_widget"}
            ]
            audit["individual_fallback_count"] = len(unresolved_targets)
            if unresolved_targets:
                fallback_items = await asyncio.gather(
                    *[_generate(target) for target in unresolved_targets],
                    return_exceptions=True,
                )
                for item in fallback_items:
                    if isinstance(item, Exception):
                        audit["individual_fallback_failed_count"] = (
                            int(audit.get("individual_fallback_failed_count", 0)) + 1
                        )
                        continue
                    if not isinstance(item, dict):
                        continue
                    review_id = str(item.get("field_review_id") or "").strip()
                    if review_id:
                        page_items_by_id[review_id] = item
                intelligence_items = list(page_items_by_id.values())
        except Exception as exc:
            audit["reason"] = f"llm_failed: {exc}"
            return working_pdf, audit
        finally:
            audit["llm_usage"] = _llm_usage_snapshot(usage)
            await llm_client.close()

    remove_review_ids = [
        str(item.get("field_review_id") or "").strip()
        for item in intelligence_items
        if isinstance(item, dict) and _should_auto_remove_widget(item)
    ]
    if not remove_review_ids:
        audit["reason"] = "no_safe_resolutions"
        return working_pdf, audit

    patched_pdf = get_output_path(
        job.id,
        f"pretag_widget_rationalization_{job.original_filename}",
    )
    try:
        applied_review_ids = remove_widget_fields(
            input_pdf=working_pdf,
            output_pdf=patched_pdf,
            review_ids_to_remove=set(remove_review_ids),
        )
    except Exception as exc:
        audit["reason"] = f"apply_failed: {exc}"
        return working_pdf, audit
    if not applied_review_ids:
        audit["reason"] = "no_matching_fields"
        return working_pdf, audit

    applied_pages = {
        int(target.get("page"))
        for target in targets
        if str(target.get("field_review_id") or "").strip() in set(applied_review_ids)
        and isinstance(target.get("page"), int)
        and int(target.get("page")) > 0
    }
    audit["applied"] = True
    audit["applied_count"] = len(applied_review_ids)
    audit["pages"] = sorted(applied_pages)
    audit["review_ids"] = applied_review_ids
    audit["reason"] = "applied"
    return patched_pdf, audit


async def _apply_pretag_form_intelligence(
    *,
    job: Job,
    settings: Settings,
    working_pdf: Path,
    structure_json: dict[str, object],
    targets: list[dict[str, object]] | None = None,
) -> tuple[Path, dict[str, object]]:
    audit: dict[str, object] = {
        "enabled": bool(settings.auto_apply_form_intelligence),
        "attempted": False,
        "applied": False,
        "reason": "",
        "candidate_count": 0,
        "applied_count": 0,
        "page_batch_count": 0,
        "page_batch_resolved_count": 0,
        "page_batch_failed_count": 0,
        "individual_fallback_count": 0,
        "individual_fallback_failed_count": 0,
        "pages": [],
        "review_ids": [],
        "llm_usage": _empty_llm_usage(),
    }
    if not settings.auto_apply_form_intelligence:
        audit["reason"] = "disabled"
        return working_pdf, audit
    if not working_pdf.exists():
        audit["reason"] = "missing_pdf"
        return working_pdf, audit
    if not isinstance(structure_json, dict):
        audit["reason"] = "missing_structure"
        return working_pdf, audit

    if targets is None:
        targets = _form_targets_for_intelligence(
            working_pdf=working_pdf,
            structure_json=structure_json,
        )
    if not targets:
        audit["reason"] = "no_candidates"
        return working_pdf, audit

    audit["attempted"] = True
    audit["candidate_count"] = len(targets)
    preview_job = _preview_job_for_pdf(job, working_pdf)
    llm_client = _make_pretag_llm_client(settings)
    pretag_timeout = _pretag_llm_operation_timeout_seconds(settings)
    fallback_timeout = _pretag_llm_fallback_operation_timeout_seconds(settings)
    with track_llm_usage() as usage:
        try:
            semaphore = asyncio.Semaphore(max(1, settings.llm_max_concurrency))

            targets_by_review_id = {
                str(target.get("field_review_id") or "").strip(): target
                for target in targets
                if str(target.get("field_review_id") or "").strip()
            }
            targets_by_page: dict[int, list[dict[str, object]]] = {}
            for target in targets:
                page = target.get("page")
                if not isinstance(page, int) or page <= 0:
                    continue
                targets_by_page.setdefault(page, []).append(target)
            audit["page_batch_count"] = len(targets_by_page)

            async def _generate(target: dict[str, object]) -> dict[str, object]:
                async with semaphore:
                    review_id = str(target.get("field_review_id") or "?").strip() or "?"
                    return await _await_llm_operation(
                        settings=settings,
                        label=f"form_intelligence[{review_id}]",
                        timeout_seconds=fallback_timeout,
                        awaitable=generate_form_intelligence(
                            job=preview_job,
                            target={
                                key: value
                                for key, value in target.items()
                                if key != "nearby_blocks"
                            },
                            nearby_blocks=list(target.get("nearby_blocks") or []),
                            llm_client=llm_client,
                        ),
                    )

            async def _generate_page(
                page_number: int, page_targets: list[dict[str, object]]
            ) -> list[dict[str, object]]:
                async with semaphore:
                    return await _await_llm_operation(
                        settings=settings,
                        label=f"form_page_intelligence[{page_number}]",
                        timeout_seconds=pretag_timeout,
                        awaitable=generate_form_intelligence_for_page(
                            job=preview_job,
                            page_number=page_number,
                            targets=page_targets,
                            llm_client=llm_client,
                        ),
                    )

            intelligence_items: list[dict[str, object]] = []
            page_items_by_id: dict[str, dict[str, object]] = {}
            for page_number, page_targets in sorted(targets_by_page.items()):
                try:
                    page_result = await _generate_page(page_number, page_targets)
                except Exception:
                    audit["page_batch_failed_count"] = (
                        int(audit.get("page_batch_failed_count", 0)) + 1
                    )
                    continue
                page_items = page_result
                for item in page_items:
                    if not isinstance(item, dict):
                        continue
                    review_id = str(item.get("field_review_id") or "").strip()
                    if review_id:
                        page_items_by_id[review_id] = item
                    intelligence_items.append(item)
            safe_page_results = sum(
                1
                for item in page_items_by_id.values()
                if _should_auto_apply_form_intelligence(item)
            )
            audit["page_batch_resolved_count"] = safe_page_results

            unresolved_targets = [
                target
                for review_id, target in targets_by_review_id.items()
                if not _should_auto_apply_form_intelligence(page_items_by_id.get(review_id, {}))
            ]
            audit["individual_fallback_count"] = len(unresolved_targets)
            if unresolved_targets:
                fallback_items = await asyncio.gather(
                    *[_generate(target) for target in unresolved_targets],
                    return_exceptions=True,
                )
                for item in fallback_items:
                    if isinstance(item, Exception):
                        audit["individual_fallback_failed_count"] = (
                            int(audit.get("individual_fallback_failed_count", 0)) + 1
                        )
                        continue
                    if not isinstance(item, dict):
                        continue
                    review_id = str(item.get("field_review_id") or "").strip()
                    if review_id:
                        page_items_by_id[review_id] = item
                intelligence_items = list(page_items_by_id.values())
        except Exception as exc:
            audit["reason"] = f"llm_failed: {exc}"
            return working_pdf, audit
        finally:
            audit["llm_usage"] = _llm_usage_snapshot(usage)
            await llm_client.close()

    approved_labels: dict[str, str] = {}
    approved_pages: set[int] = set()
    for item in intelligence_items:
        if not isinstance(item, dict) or not _should_auto_apply_form_intelligence(item):
            continue
        review_id = str(item.get("field_review_id") or "").strip()
        label = str(item.get("accessible_label") or "").strip()
        if not review_id or not label:
            continue
        approved_labels[review_id] = label
        page = item.get("page")
        if isinstance(page, int) and page > 0:
            approved_pages.add(page)

    if not approved_labels:
        audit["reason"] = "no_safe_resolutions"
        return working_pdf, audit

    patched_pdf = get_output_path(
        job.id,
        f"pretag_form_intelligence_{job.original_filename}",
    )
    try:
        applied_review_ids = apply_field_accessible_names(
            input_pdf=working_pdf,
            output_pdf=patched_pdf,
            labels_by_review_id=approved_labels,
        )
    except Exception as exc:
        audit["reason"] = f"apply_failed: {exc}"
        return working_pdf, audit
    if not applied_review_ids:
        audit["reason"] = "no_matching_fields"
        return working_pdf, audit

    audit["applied"] = True
    audit["applied_count"] = len(applied_review_ids)
    audit["pages"] = sorted(approved_pages)
    audit["review_ids"] = applied_review_ids
    audit["reason"] = (
        "applied_with_fallback_failures"
        if int(audit.get("individual_fallback_failed_count", 0)) > 0
        else "applied"
    )
    return patched_pdf, audit


def _tagging_regressions(candidate, current) -> list[str]:
    """Return significant structural-tagging regressions for candidate output."""
    regressions: list[str] = []

    def _count(obj, field: str) -> int:
        value = getattr(obj, field, 0)
        return value if isinstance(value, int) and value >= 0 else 0

    tracked = (
        ("headings_tagged", "headings"),
        ("figures_tagged", "figures"),
        ("tables_tagged", "tables"),
        ("lists_tagged", "lists"),
    )
    for field, label in tracked:
        current_count = _count(current, field)
        candidate_count = _count(candidate, field)
        if current_count <= 0:
            continue
        # Reject complete drop-to-zero, or major drop on non-trivial counts.
        if candidate_count == 0:
            regressions.append(f"{label} dropped to zero ({current_count} -> 0)")
        elif current_count >= 5 and candidate_count < int(current_count * 0.8):
            regressions.append(
                f"{label} dropped significantly ({current_count} -> {candidate_count})"
            )

    current_links = _count(current, "links_tagged")
    candidate_links = _count(candidate, "links_tagged")
    if current_links > 0 and candidate_links < current_links:
        regressions.append(f"links decreased ({current_links} -> {candidate_links})")

    return regressions


async def _apply_table_model(
    working_pdf: Path, structure_json: dict, settings: Settings
) -> tuple[dict, dict]:
    """Docling's tables rebuilt on the table model's grids (table_model.py)
    when TABLE_MODEL_URL is set. A model that is down, busy or slow leaves
    every table as Docling found it; the job never fails here."""
    if not settings.table_model_url:
        return structure_json, {"enabled": False}
    from app.pipeline.table_model import HttpReader, rewrite_tables

    reader = HttpReader(
        settings.table_model_url, settings.table_model_name, timeout=settings.table_model_timeout
    )
    try:
        structure, audit = await rewrite_tables(
            working_pdf,
            structure_json,
            reader,
            min_coverage=settings.table_model_min_coverage,
            min_coverage_scan=settings.table_model_min_coverage_scan,
            correct_scan_text=settings.table_model_scan_text,
        )
    except Exception as exc:
        # The step is optional: any failure keeps Docling's tables.
        logger.warning("Table model step failed for %s: %r", working_pdf.name, exc)
        return structure_json, {"enabled": True, "error": repr(exc)}
    audit["enabled"] = True
    return structure, audit


async def _update_step(
    db: AsyncSession,
    job_id: str,
    step_name: str,
    status: str,
    result: dict | None = None,
    error: str | None = None,
):
    """Update a job step's status in the database."""
    stmt = select(JobStep).where(JobStep.job_id == job_id, JobStep.step_name == step_name)
    row = await db.execute(stmt)
    step = row.scalar_one_or_none()
    if step is None:
        step = JobStep(job_id=job_id, step_name=step_name)
        db.add(step)
        await db.flush()

    step.status = status
    if status == "running":
        step.started_at = datetime.now(UTC)
    if status in ("complete", "failed", "skipped"):
        step.completed_at = datetime.now(UTC)
    if result:
        step.result_json = json.dumps(result)
    if error:
        step.error = error

    await db.commit()


async def run_pipeline(
    job_id: str,
    db_session_maker,
    settings: Settings,
    job_manager: JobManager,
):
    """Execute the full PDF accessibility pipeline for a job."""
    with job_model_access():
        await _run_pipeline(job_id, db_session_maker, settings, job_manager)


async def _run_pipeline(
    job_id: str,
    db_session_maker,
    settings: Settings,
    job_manager: JobManager,
):
    async with db_session_maker() as db:
        job = await db.get(Job, job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return

        input_path = Path(job.input_path)
        job_dir = create_job_dir(job_id)
        current_step = None

        try:
            job.status = "processing"
            await db.commit()

            # ── Step 1: Classify ──
            current_step = "classify"
            await _update_step(db, job_id, "classify", "running")
            job_manager.emit_progress(job_id, step="classify", status="running")

            classification = await classify_pdf(input_path)

            job.classification = classification.type
            job.page_count = classification.total_pages
            # Set OCR language: auto-detection > global default.
            if not job.ocr_language and classification.detected_language:
                job.ocr_language = bcp47_to_tesseract(
                    classification.detected_language, settings.ocr_language
                )
            elif not job.ocr_language:
                job.ocr_language = settings.ocr_language
            await _update_step(
                db,
                job_id,
                "classify",
                "complete",
                result={
                    "type": classification.type,
                    "confidence": classification.confidence,
                    "pages_with_text": classification.pages_with_text,
                    "total_pages": classification.total_pages,
                    "image_heavy_pages": classification.image_heavy_pages,
                    "total_image_pixels": classification.total_image_pixels,
                    "ocr_scan_like": classification.ocr_scan_like,
                    "detected_language": classification.detected_language,
                    "ocr_language": job.ocr_language,
                },
            )
            job_manager.emit_progress(
                job_id,
                step="classify",
                status="complete",
                result={"type": classification.type},
            )

            # ── Step 2: OCR (conditional) ──
            current_step = "ocr"
            working_pdf = input_path

            if classification.type in ("scanned", "mixed"):
                await _update_step(db, job_id, "ocr", "running")
                job_manager.emit_progress(job_id, step="ocr", status="running")

                ocr_output = job_dir / "ocred.pdf"
                ocr_result = await run_ocr(
                    input_path,
                    ocr_output,
                    job.ocr_language or settings.ocr_language,
                    # Pages whose text is in their images also have native
                    # text, which --redo-ocr leaves out of OCR.
                    mode="redo" if classification.ocr_pages else "skip",
                    rotate_pages=settings.ocr_rotate_pages,
                    deskew=settings.ocr_deskew,
                    timeout_seconds=settings.subprocess_timeout_ocr,
                    engine=settings.ocr_engine,
                    pages=classification.ocr_pages or None,
                )

                if ocr_result.success:
                    working_pdf = ocr_result.output_path
                    await _update_step(
                        db,
                        job_id,
                        "ocr",
                        "complete",
                        result={
                            "skipped": ocr_result.skipped,
                            "message": ocr_result.message,
                        },
                    )
                    job_manager.emit_progress(job_id, step="ocr", status="complete")
                else:
                    await _update_step(db, job_id, "ocr", "failed", error=ocr_result.message)
                    job_manager.emit_progress(
                        job_id,
                        step="ocr",
                        status="failed",
                        message=ocr_result.message,
                    )
                    raise RuntimeError(
                        f"OCR failed for {classification.type} document: {ocr_result.message}"
                    )
            elif classification.type == "ocr_scan":
                await _update_step(
                    db,
                    job_id,
                    "ocr",
                    "skipped",
                    result={
                        "skipped": True,
                        "message": ("Skipped OCR because this scan already has an OCR text layer."),
                    },
                )
                job_manager.emit_progress(
                    job_id,
                    step="ocr",
                    status="skipped",
                    result={"reason": "existing_ocr_text_layer"},
                )
            else:
                await _update_step(db, job_id, "ocr", "skipped")
                job_manager.emit_progress(job_id, step="ocr", status="skipped")

            raise_if_model_access_refused()

            # ── Step 3: Structure Extraction ──
            current_step = "structure"
            await _update_step(db, job_id, "structure", "running")
            job_manager.emit_progress(job_id, step="structure", status="running")

            structure = await extract_structure(
                working_pdf,
                job_dir,
                table_cell_matching=docling_table_cell_matching(classification.type),
            )
            if structure.processed_pdf_path:
                working_pdf = structure.processed_pdf_path
            docling_escalation_plan = docling_structure_escalation_plan(structure.document_json)

            toc_intelligence_assist = {
                "attempted": False,
                "applied": False,
                "reason": "disabled",
                "groups_considered": 0,
                "groups_applied": 0,
                "llm_usage": _empty_llm_usage(),
            }
            title_intelligence_assist = {
                "attempted": False,
                "applied": False,
                "reason": "disabled",
                "llm_usage": _empty_llm_usage(),
            }
            bookmark_intelligence_assist = {
                "attempted": False,
                "applied": False,
                "reason": "disabled",
                "selected_heading_count": 0,
                "llm_usage": _empty_llm_usage(),
            }
            structure_intelligence_llm_usage = _empty_llm_usage()
            if (
                settings.assist_toc_with_llm
                or settings.assist_title_with_llm
                or settings.assist_bookmarks_with_llm
            ):
                llm_client = make_llm_client(settings)
                try:
                    combined_early_pages_used = False
                    prefetched_front_matter_entries: list[dict[str, Any]] | None = None
                    prefetched_front_matter_audit: dict[str, Any] | None = None
                    if settings.assist_toc_with_llm:
                        with track_llm_usage() as usage:
                            try:
                                (
                                    structure.document_json,
                                    toc_intelligence_assist,
                                ) = await enhance_toc_structure_with_intelligence(
                                    pdf_path=working_pdf,
                                    structure_json=structure.document_json,
                                    original_filename=job.original_filename or input_path.name,
                                    llm_client=llm_client,
                                )
                            except Exception as exc:
                                logger.warning(
                                    f"TOC intelligence assist failed for {job.original_filename}: {exc}"
                                )
                                toc_intelligence_assist = {
                                    "attempted": True,
                                    "applied": False,
                                    "reason": str(exc),
                                    "groups_considered": 0,
                                    "groups_applied": 0,
                                }
                            toc_intelligence_assist["llm_usage"] = _llm_usage_snapshot(usage)
                            _accumulate_llm_usage(
                                structure_intelligence_llm_usage,
                                toc_intelligence_assist.get("llm_usage"),
                            )
                    title_needs_llm = (
                        docling_escalation_plan.get("title", {}).get("decision") != "docling"
                    )
                    bookmarks_need_llm = (
                        docling_escalation_plan.get("bookmarks", {}).get("decision") != "docling"
                    )
                    if settings.assist_title_with_llm and not title_needs_llm:
                        title_intelligence_assist = {
                            "attempted": False,
                            "applied": False,
                            "reason": docling_escalation_plan.get("title", {}).get(
                                "reason", "docling"
                            ),
                            "llm_usage": _empty_llm_usage(),
                        }
                    if settings.assist_bookmarks_with_llm and not bookmarks_need_llm:
                        bookmark_intelligence_assist = {
                            "attempted": False,
                            "applied": False,
                            "reason": docling_escalation_plan.get("bookmarks", {}).get(
                                "reason", "docling"
                            ),
                            "selected_heading_count": 0,
                            "llm_usage": _empty_llm_usage(),
                        }
                    if (
                        settings.assist_title_with_llm
                        and settings.assist_bookmarks_with_llm
                        and title_needs_llm
                        and bookmarks_need_llm
                    ):
                        with track_llm_usage() as usage:
                            try:
                                (
                                    structure.document_json,
                                    combined_title_assist,
                                    prefetched_front_matter_entries,
                                    prefetched_front_matter_audit,
                                ) = await enhance_document_title_and_front_matter_with_intelligence(
                                    pdf_path=working_pdf,
                                    structure_json=structure.document_json,
                                    original_filename=job.original_filename or input_path.name,
                                    llm_client=llm_client,
                                )
                                combined_early_pages_used = bool(
                                    combined_title_assist.get("attempted", False)
                                ) or bool(
                                    (prefetched_front_matter_audit or {}).get("attempted", False)
                                )
                                if combined_early_pages_used:
                                    title_intelligence_assist = combined_title_assist
                            except Exception as exc:
                                logger.warning(
                                    f"Early-pages intelligence assist failed for {job.original_filename}: {exc}"
                                )
                                combined_early_pages_used = False
                                prefetched_front_matter_entries = None
                                prefetched_front_matter_audit = None
                            if combined_early_pages_used:
                                title_intelligence_assist["llm_usage"] = _llm_usage_snapshot(usage)
                                _accumulate_llm_usage(
                                    structure_intelligence_llm_usage,
                                    title_intelligence_assist.get("llm_usage"),
                                )
                    if settings.assist_title_with_llm:
                        if title_needs_llm and not combined_early_pages_used:
                            with track_llm_usage() as usage:
                                try:
                                    (
                                        structure.document_json,
                                        title_intelligence_assist,
                                    ) = await enhance_document_title_with_intelligence(
                                        pdf_path=working_pdf,
                                        structure_json=structure.document_json,
                                        original_filename=job.original_filename or input_path.name,
                                        llm_client=llm_client,
                                    )
                                except Exception as exc:
                                    logger.warning(
                                        f"Title intelligence assist failed for {job.original_filename}: {exc}"
                                    )
                                    title_intelligence_assist = {
                                        "attempted": True,
                                        "applied": False,
                                        "reason": str(exc),
                                    }
                                title_intelligence_assist["llm_usage"] = _llm_usage_snapshot(usage)
                                _accumulate_llm_usage(
                                    structure_intelligence_llm_usage,
                                    title_intelligence_assist.get("llm_usage"),
                                )
                    if settings.assist_bookmarks_with_llm and bookmarks_need_llm:
                        with track_llm_usage() as usage:
                            try:
                                (
                                    structure.document_json,
                                    bookmark_intelligence_assist,
                                ) = await enhance_bookmark_structure_with_intelligence(
                                    pdf_path=working_pdf,
                                    structure_json=structure.document_json,
                                    original_filename=job.original_filename or input_path.name,
                                    llm_client=llm_client,
                                    prefetched_front_matter_entries=(
                                        prefetched_front_matter_entries
                                        if combined_early_pages_used
                                        else None
                                    ),
                                    prefetched_front_matter_audit=(
                                        prefetched_front_matter_audit
                                        if combined_early_pages_used
                                        else None
                                    ),
                                )
                            except Exception as exc:
                                logger.warning(
                                    f"Bookmark intelligence assist failed for {job.original_filename}: {exc}"
                                )
                                bookmark_intelligence_assist = {
                                    "attempted": True,
                                    "applied": False,
                                    "reason": str(exc),
                                    "selected_heading_count": 0,
                                }
                            bookmark_intelligence_assist["llm_usage"] = _llm_usage_snapshot(usage)
                            _accumulate_llm_usage(
                                structure_intelligence_llm_usage,
                                bookmark_intelligence_assist.get("llm_usage"),
                            )
                finally:
                    await llm_client.close()

            structure.document_json, structure.figures, synthetic_figure_rationalization = (
                synthesize_missing_visual_figures(
                    working_pdf=working_pdf,
                    structure_json=structure.document_json,
                    figures=structure.figures,
                    figures_dir=job_dir / "figures",
                )
            )
            structure.figures_count = len(structure.figures)
            structure.document_json, table_model_audit = await _apply_table_model(
                working_pdf, structure.document_json, settings
            )

            elements = structure.document_json.get("elements", [])
            headings_count = sum(1 for el in elements if el.get("type") == "heading")
            tables_count = sum(1 for el in elements if el.get("type") == "table")
            toc_caption_count = sum(1 for el in elements if el.get("type") == "toc_caption")
            toc_item_count = sum(
                1 for el in elements if el.get("type") in {"toc_item", "toc_item_table"}
            )

            job.structure_json = json.dumps(structure.document_json)
            await _update_step(
                db,
                job_id,
                "structure",
                "complete",
                result={
                    "page_count": structure.page_count,
                    "headings": headings_count,
                    "tables": tables_count,
                    "figures": structure.figures_count,
                    "toc_captions": toc_caption_count,
                    "toc_items": toc_item_count,
                    "toc_intelligence_assist": toc_intelligence_assist,
                    "title_intelligence_assist": title_intelligence_assist,
                    "bookmark_intelligence_assist": bookmark_intelligence_assist,
                    "structure_intelligence_llm_usage": structure_intelligence_llm_usage,
                    "docling_escalation_plan": docling_escalation_plan,
                    "synthetic_figure_rationalization": synthetic_figure_rationalization,
                    "table_model": table_model_audit,
                },
            )
            job_manager.emit_progress(
                job_id,
                step="structure",
                status="complete",
                result={
                    "figures_found": structure.figures_count,
                    "toc_captions": toc_caption_count,
                    "toc_items": toc_item_count,
                    "toc_intelligence_assist_applied": bool(
                        toc_intelligence_assist.get("applied", False)
                    ),
                    "title_intelligence_assist_applied": bool(
                        title_intelligence_assist.get("applied", False)
                    ),
                    "bookmark_intelligence_assist_applied": bool(
                        bookmark_intelligence_assist.get("applied", False)
                    ),
                    "structure_intelligence_llm_request_count": int(
                        structure_intelligence_llm_usage.get("request_count", 0) or 0
                    ),
                    "synthetic_figure_count": int(
                        synthetic_figure_rationalization.get("applied_count", 0) or 0
                    ),
                },
            )

            raise_if_model_access_refused()

            # ── Step 4: Alt Text Generation ──
            current_step = "alt_text"
            if structure.figures:
                if settings.skip_alt_text_generation:
                    skip_result = _alt_text_step_result(
                        count=0,
                        approved=0,
                        rejected=0,
                        reviewable_changes=0,
                        reclassified=0,
                        auto_approve_enabled=settings.auto_approve_generated_alt_text,
                        llm_usage=_empty_llm_usage(),
                        reason="disabled_by_settings",
                        skipped_figure_count=len(structure.figures),
                    )
                    await _update_step(
                        db,
                        job_id,
                        "alt_text",
                        "skipped",
                        result=skip_result,
                    )
                    job_manager.emit_progress(
                        job_id,
                        step="alt_text",
                        status="skipped",
                        result=skip_result,
                    )
                    await run_tagging_and_validation(
                        job_id, db, settings, job_manager, working_pdf, structure.document_json
                    )
                    return

                await _update_step(db, job_id, "alt_text", "running")
                job_manager.emit_progress(job_id, step="alt_text", status="running")

                llm_client = make_llm_client(settings)
                alt_text_llm_usage = _empty_llm_usage()

                try:
                    with track_llm_usage() as usage:
                        alt_texts = await generate_alt_text(
                            structure.figures,
                            llm_client,
                            job=job,
                            original_filename=job.original_filename,
                            settings=settings,
                            structure=structure.document_json,
                            pdf_path=working_pdf,
                        )
                    alt_text_llm_usage = _llm_usage_snapshot(usage)
                finally:
                    await llm_client.close()

                structure.document_json, figure_reclassification = _apply_figure_reclassification(
                    structure.document_json,
                    alt_texts,
                    working_pdf=working_pdf,
                )
                if figure_reclassification.get("applied"):
                    job.structure_json = json.dumps(structure.document_json)
                    await db.commit()

                # Save figure semantics to database and continue directly into tagging.
                approved_count = 0
                rejected_count = 0
                reclassified_count = 0
                for alt in alt_texts:
                    if alt.figure_index < 0 or alt.figure_index >= len(structure.figures):
                        continue
                    if alt.status == "reclassified":
                        reclassified_count += 1
                        continue
                    fig = structure.figures[alt.figure_index]
                    status, generated_text = _alt_text_entry_state(
                        alt,
                        caption=fig.caption,
                        auto_approve=settings.auto_approve_generated_alt_text,
                    )

                    if status == "approved":
                        approved_count += 1
                    elif status == "rejected":
                        rejected_count += 1
                    db.add(
                        AltTextEntry(
                            job_id=job_id,
                            figure_index=alt.figure_index,
                            image_path=str(fig.path),
                            generated_text=generated_text or None,
                            status=status,
                        )
                    )
                await db.commit()
                figure_change_specs = figure_applied_change_specs(
                    figures=structure.figures,
                    alt_texts=alt_texts,
                )

                await _update_step(
                    db,
                    job_id,
                    "alt_text",
                    "complete",
                    result=_alt_text_step_result(
                        count=len(alt_texts),
                        approved=approved_count,
                        rejected=rejected_count,
                        reviewable_changes=len(figure_change_specs),
                        reclassified=reclassified_count,
                        auto_approve_enabled=settings.auto_approve_generated_alt_text,
                        llm_usage=alt_text_llm_usage,
                        figure_reclassification=figure_reclassification,
                    ),
                )
                job_manager.emit_progress(
                    job_id,
                    step="alt_text",
                    status="complete",
                    result=_alt_text_step_result(
                        count=len(alt_texts),
                        approved=approved_count,
                        rejected=rejected_count,
                        reviewable_changes=len(figure_change_specs),
                        reclassified=reclassified_count,
                        auto_approve_enabled=settings.auto_approve_generated_alt_text,
                        llm_usage=alt_text_llm_usage,
                    ),
                )

                # All generated alt entries are actionable (approved/rejected), continue directly.
                await run_tagging_and_validation(
                    job_id,
                    db,
                    settings,
                    job_manager,
                    working_pdf,
                    structure.document_json,
                    pre_applied_change_specs=figure_change_specs,
                )
                return

            else:
                skip_result = _alt_text_step_result(
                    count=0,
                    approved=0,
                    rejected=0,
                    reviewable_changes=0,
                    reclassified=0,
                    auto_approve_enabled=settings.auto_approve_generated_alt_text,
                    llm_usage=_empty_llm_usage(),
                    reason="no_figures",
                )
                await _update_step(db, job_id, "alt_text", "skipped", result=skip_result)
                job_manager.emit_progress(
                    job_id, step="alt_text", status="skipped", result=skip_result
                )

            # No figures = skip review, go straight to tagging
            raise_if_model_access_refused()
            await run_tagging_and_validation(
                job_id, db, settings, job_manager, working_pdf, structure.document_json
            )

        except Exception as e:
            logger.exception(f"Pipeline failed for job {job_id}")
            await db.rollback()
            # Sanitize error: strip filesystem paths (multi-segment, e.g. /home/user/...)
            # but preserve single-segment PDF names like /Font, /Subtype
            user_error = re.sub(r"/(?:[^\s/]+/)+[^\s]*", "", str(e)).strip(": ")
            if current_step:
                await _update_step(
                    db,
                    job_id,
                    current_step,
                    "failed",
                    error=user_error or str(e),
                )
            job = await db.get(Job, job_id)
            if job:
                job.status = "failed"
                job.error = user_error or f"Pipeline failed at step: {current_step}"
                await db.commit()
            job_manager.emit_progress(
                job_id,
                step=current_step or "error",
                status="failed",
                message=user_error,
            )


async def run_tagging_and_validation(
    job_id: str,
    db: AsyncSession,
    settings: Settings,
    job_manager: JobManager,
    working_pdf: Path | None = None,
    structure_json: dict | None = None,
    pre_applied_change_specs: list[dict[str, object]] | None = None,
):
    """Run steps 5-6 (tagging + validation), after alt text or a review action."""
    with job_model_access():
        await _run_tagging_and_validation(
            job_id,
            db,
            settings,
            job_manager,
            working_pdf,
            structure_json,
            pre_applied_change_specs,
        )


@dataclass
class Candidate:
    """A PDF and the evaluations and changes belonging to those exact bytes."""

    output_path: Path
    validation: ValidationResult
    validation_report: dict[str, object]
    fidelity_report: dict[str, object]
    tagging_result: TaggingResult
    structure_json: dict
    working_pdf: Path
    fidelity_source_pdf: Path
    produced_by: str
    alt_entries: list[dict[str, object]]
    review_tasks: list[dict[str, object]] = dataclass_field(default_factory=list)
    applied_change_specs: list[dict[str, object]] = dataclass_field(default_factory=list)
    grounded_text_adjudication: dict[str, object] | None = None


def _candidate_job(job: Job, candidate: Candidate) -> Job:
    preview_job = copy.copy(job)
    preview_job.output_path = str(candidate.output_path)
    preview_job.structure_json = json.dumps(candidate.structure_json)
    return preview_job


async def _assess_candidate_fidelity(
    candidate: Candidate,
    *,
    job: Job,
    settings: Settings,
    adjudicate: bool = True,
) -> None:
    candidate.fidelity_report, candidate.review_tasks = await asyncio.to_thread(
        assess_fidelity,
        input_pdf=Path(job.input_path),
        output_pdf=candidate.output_path,
        comparison_source_pdf=candidate.fidelity_source_pdf,
        structure_json=candidate.structure_json,
        alt_entries=candidate.alt_entries,
        validation_report=candidate.validation_report,
        raw_validation_report=candidate.validation.raw_report,
        tagging_metrics=candidate.validation_report["tagging"],
        classification=job.classification,
    )
    if adjudicate:
        (
            candidate.review_tasks,
            candidate.fidelity_report,
            candidate.grounded_text_adjudication,
        ) = await _adjudicate_grounded_text_candidates(
            job=_candidate_job(job, candidate),
            settings=settings,
            review_tasks=candidate.review_tasks,
            fidelity_report=candidate.fidelity_report,
        )


async def evaluate_candidate(
    *,
    job: Job,
    settings: Settings,
    input_path: Path,
    output_path: Path,
    structure_json: dict,
    reviewed_alts: list[dict],
    alt_entries,
    baseline_validation: ValidationResult,
    font_remediation: FontRemediation,
    llm_font_map_auto: dict[str, object],
    produced_by: str,
    fidelity_source_pdf: Path,
    applied_change_specs: list[dict[str, object]],
    tagging_result: TaggingResult | None = None,
    validation: ValidationResult | None = None,
    remediate_fonts: bool = False,
    on_tagged=None,
    on_validated=None,
    adjudicate: bool = True,
) -> Candidate:
    """Tag, validate, build the report, assess fidelity, then adjudicate if needed.

    Font-map and font-lane helpers already tag/patch and validate their outputs;
    accept those results without re-tagging away the repairs.
    """
    if tagging_result is None:
        tagging_result = await tag_pdf(
            input_path=input_path,
            output_path=output_path,
            structure_json=structure_json,
            alt_texts=reviewed_alts,
            original_filename=job.original_filename or "",
        )
        output_path = Path(tagging_result.output_path)
    if on_tagged is not None:
        await on_tagged(tagging_result)
    if validation is None:
        validation = await validate_pdf(
            pdf_path=output_path,
            verapdf_path=settings.verapdf_path,
            flavour=settings.verapdf_flavour,
            timeout_seconds=settings.subprocess_timeout_validation,
        )
    if remediate_fonts:
        (
            tagging_result,
            validation,
            output_path,
            input_path,
            structure_json,
            fidelity_source_pdf,
        ) = await _select_font_lanes(
            job=job,
            settings=settings,
            working_pdf=input_path,
            structure_json=structure_json,
            reviewed_alts=reviewed_alts,
            tagging_result=tagging_result,
            validation=validation,
            font_remediation=font_remediation,
        )
        llm_font_map_auto["enabled"] = bool(settings.auto_apply_llm_font_map)
    # A patch may preserve all tag counts while producing a different PDF.
    tagging_result = copy.copy(tagging_result)
    tagging_result.output_path = Path(output_path)
    candidate = Candidate(
        output_path=Path(output_path),
        validation=validation,
        validation_report=_build_validation_payload(
            baseline_validation=baseline_validation,
            selected_validation=validation,
            settings=settings,
            font_remediation=font_remediation,
            tagging_result=tagging_result,
            llm_font_map_auto=llm_font_map_auto,
        ),
        fidelity_report={},
        tagging_result=tagging_result,
        structure_json=structure_json or {},
        working_pdf=Path(input_path),
        fidelity_source_pdf=fidelity_source_pdf,
        produced_by=produced_by,
        alt_entries=[
            {
                "figure_index": entry.figure_index,
                "generated_text": entry.generated_text,
                "edited_text": entry.edited_text,
                "status": entry.status,
            }
            for entry in alt_entries
        ],
        applied_change_specs=list(applied_change_specs),
    )
    if on_validated is not None:
        await on_validated(candidate)
    await _assess_candidate_fidelity(candidate, job=job, settings=settings, adjudicate=adjudicate)
    return candidate


def _candidate_is_acceptable(
    candidate: Candidate,
    current: Candidate,
    *,
    require_improvement: bool = False,
    grounded_text: bool = False,
) -> bool:
    """Adopt only compliant, non-regressing candidates under the existing policy."""
    if not candidate.validation.compliant:
        return False
    if not _fidelity_not_worse(candidate.fidelity_report, current.fidelity_report):
        return False
    if grounded_text:
        return _grounded_text_retry_improved(current.review_tasks, candidate.review_tasks)
    if require_improvement:
        return _blocking_task_count(candidate.review_tasks) < _blocking_task_count(
            current.review_tasks
        )
    return True


async def _retry_grounded_candidate(
    candidate: Candidate, *, job: Job, settings: Settings, evaluate,
) -> Candidate:
    safe_resolutions = _collect_safe_grounded_text_resolutions(candidate.grounded_text_adjudication)
    if not safe_resolutions or not _blocking_task_count(candidate.review_tasks):
        return candidate
    retry_structure, retry_audit = _apply_grounded_text_resolutions_to_structure(
        candidate.structure_json,
        safe_resolutions,
    )
    if not bool(retry_audit.get("applied")):
        return candidate
    retry = await evaluate(
        input_path=candidate.output_path,
        output_path=get_output_path(job.id, f"accessible_grounded_retry_{job.original_filename}"),
        structure_json=retry_structure,
        fidelity_source_pdf=candidate.fidelity_source_pdf,
        applied_change_specs=candidate.applied_change_specs,
        produced_by="grounded_text",
        adjudicate=False,
    )
    if (
        _candidate_is_acceptable(retry, candidate, require_improvement=True)
        and not _has_grounded_text_candidate_task(retry.review_tasks)
    ):
        return retry
    (
        retry.review_tasks,
        retry.fidelity_report,
        retry.grounded_text_adjudication,
    ) = await _adjudicate_grounded_text_candidates(
        job=_candidate_job(job, retry),
        settings=settings,
        review_tasks=retry.review_tasks,
        fidelity_report=retry.fidelity_report,
    )
    return retry if _candidate_is_acceptable(retry, candidate, grounded_text=True) else candidate


async def _retry_structure_candidate(
    candidate: Candidate, *, job: Job, settings: Settings, evaluate,
) -> Candidate:
    if not candidate.structure_json or not _blocking_task_count(candidate.review_tasks):
        return candidate
    suggested_tasks, structure, applied_specs = await auto_apply_structure_tasks(
        job=_candidate_job(job, candidate),
        settings=settings,
        review_tasks=candidate.review_tasks,
        structure_json=candidate.structure_json,
    )
    candidate.review_tasks = suggested_tasks
    if not applied_specs or structure == candidate.structure_json:
        return candidate
    retry = await evaluate(
        input_path=candidate.output_path,
        output_path=get_output_path(job.id, f"accessible_auto_review_{job.original_filename}"),
        structure_json=structure,
        fidelity_source_pdf=candidate.fidelity_source_pdf,
        applied_change_specs=[*candidate.applied_change_specs, *applied_specs],
        produced_by="auto_structure",
    )
    return retry if _candidate_is_acceptable(retry, candidate, require_improvement=True) else candidate


async def _retry_font_map_candidate(
    candidate: Candidate,
    *,
    job: Job,
    settings: Settings,
    evaluate,
    baseline_validation: ValidationResult,
    font_remediation: FontRemediation,
    llm_font_map_auto: dict[str, object],
):
    audit, validation, output_pdf, metadata_overrides = await _attempt_auto_llm_font_map(
        job=_candidate_job(job, candidate),
        settings=settings,
        output_pdf=candidate.output_path,
        current_validation=candidate.validation,
        review_tasks=candidate.review_tasks,
    )
    llm_font_map_auto.clear()
    llm_font_map_auto.update(audit)
    if validation is not None and output_pdf is not None:
        retry = await evaluate(
            input_path=candidate.output_path,
            output_path=output_pdf,
            structure_json=candidate.structure_json,
            fidelity_source_pdf=candidate.fidelity_source_pdf,
            applied_change_specs=candidate.applied_change_specs,
            produced_by="font_map",
            tagging_result=candidate.tagging_result,
            validation=validation,
            adjudicate=False,
        )
        if _candidate_is_acceptable(retry, candidate):
            return retry, metadata_overrides
        rejection_reason = (
            "fidelity_regression" if retry.validation.compliant else "noncompliant_candidate"
        )
        llm_font_map_auto["applied"] = False
        llm_font_map_auto["reason"] = rejection_reason
        for metadata in metadata_overrides.values():
            font_map_metadata = metadata.get("llm_auto_font_map")
            if isinstance(font_map_metadata, dict):
                font_map_metadata["applied"] = False
                font_map_metadata["reason"] = rejection_reason
    candidate.validation_report = _build_validation_payload(
        baseline_validation=baseline_validation,
        selected_validation=candidate.validation,
        settings=settings,
        font_remediation=font_remediation,
        tagging_result=candidate.tagging_result,
        llm_font_map_auto=llm_font_map_auto,
    )
    return candidate, metadata_overrides


async def _prepare_tagging_inputs(
    job: Job, db: AsyncSession, settings: Settings, working_pdf: Path, structure_json: dict,
):
    """Apply the existing pretag policies and collect figure decisions once."""
    # Approved and rejected figure decisions go to the tagger; fidelity also
    # sees the figures still waiting for a description.
    result = await db.execute(select(AltTextEntry).where(AltTextEntry.job_id == job.id))
    alt_entries = result.scalars().all()
    reviewed_alt_entries = [
        entry for entry in alt_entries if entry.status in ("approved", "rejected")
    ]
    reviewed_alts = [
        {
            "figure_index": a.figure_index,
            "text": a.edited_text or a.generated_text,
            "status": a.status,
            "decorative": a.status == "rejected",
        }
        for a in reviewed_alt_entries
    ]

    structure_json, pretag_grounded_text = await _apply_pretag_grounded_text_resolutions(
        job=job,
        settings=settings,
        working_pdf=working_pdf,
        structure_json=structure_json,
    )
    docling_pretag_router = docling_pretag_ambiguity_router(
        working_pdf=working_pdf,
        structure_json=structure_json,
    )
    structure_json, pretag_table_intelligence = await _apply_pretag_table_intelligence(
        job=job,
        settings=settings,
        structure_json=structure_json,
        targets=list(docling_pretag_router.get("table_targets") or []),
    )
    working_pdf, pretag_widget_rationalization = await _apply_pretag_widget_rationalization(
        job=job,
        settings=settings,
        working_pdf=working_pdf,
        structure_json=structure_json,
        targets=list(docling_pretag_router.get("widget_targets") or []),
    )
    docling_form_router = docling_pretag_ambiguity_router(
        working_pdf=working_pdf,
        structure_json=structure_json,
    )
    docling_pretag_plan = dict(docling_pretag_router.get("plan") or {})
    docling_pretag_plan["forms"] = dict(
        (docling_form_router.get("plan") or {}).get("forms") or {}
    )
    working_pdf, pretag_form_intelligence = await _apply_pretag_form_intelligence(
        job=job,
        settings=settings,
        working_pdf=working_pdf,
        structure_json=structure_json,
        targets=list(docling_form_router.get("form_targets") or []),
    )

    pretag_llm_usage = _empty_llm_usage()
    for audit in (
        pretag_grounded_text,
        pretag_table_intelligence,
        pretag_widget_rationalization,
        pretag_form_intelligence,
    ):
        _accumulate_llm_usage(pretag_llm_usage, audit.get("llm_usage"))

    pretag_details = {
        "pretag_llm_usage": pretag_llm_usage,
        "docling_pretag_plan": docling_pretag_plan,
        "pretag_grounded_text": pretag_grounded_text,
        "pretag_table_intelligence": pretag_table_intelligence,
        "pretag_widget_rationalization": pretag_widget_rationalization,
        "pretag_form_intelligence": pretag_form_intelligence,
    }
    return working_pdf, structure_json, alt_entries, reviewed_alts, pretag_details


def _tagging_step_result(tagging_result, pretag_details):
    pretag_grounded_text = pretag_details["pretag_grounded_text"]
    pretag_table_intelligence = pretag_details["pretag_table_intelligence"]
    pretag_widget_rationalization = pretag_details["pretag_widget_rationalization"]
    pretag_form_intelligence = pretag_details["pretag_form_intelligence"]
    pretag_llm_usage = pretag_details["pretag_llm_usage"]
    docling_pretag_plan = pretag_details["docling_pretag_plan"]
    return {
        "tags_added": tagging_result.tags_added,
        "lang_set": tagging_result.lang_set,
        "struct_elems": tagging_result.struct_elems_created,
        "headings_tagged": tagging_result.headings_tagged,
        "figures_tagged": tagging_result.figures_tagged,
        "decorative_figures_artifacted": tagging_result.decorative_figures_artifacted,
        "tables_tagged": tagging_result.tables_tagged,
        "lists_tagged": tagging_result.lists_tagged,
        "links_tagged": tagging_result.links_tagged,
        "bookmarks_added": tagging_result.bookmarks_added,
        "title_set": tagging_result.title_set,
        "reading_order_diagnostics": getattr(tagging_result, "reading_order_diagnostics", []),
        "grounded_text_auto_applied": bool(pretag_grounded_text.get("applied")),
        "grounded_text_auto_applied_count": int(
            pretag_grounded_text.get("applied_count", 0) or 0
        ),
        "grounded_text_code_auto_applied_count": int(
            pretag_grounded_text.get("applied_code_text_count", 0) or 0
        ),
        "table_intelligence_auto_applied": bool(pretag_table_intelligence.get("applied")),
        "table_intelligence_auto_applied_count": int(
            pretag_table_intelligence.get("applied_count", 0) or 0
        ),
        "table_intelligence_confirmed_count": int(
            pretag_table_intelligence.get("confirmed_count", 0) or 0
        ),
        "table_intelligence_set_headers_count": int(
            pretag_table_intelligence.get("set_headers_count", 0) or 0
        ),
        "widget_rationalization_auto_applied": bool(
            pretag_widget_rationalization.get("applied")
        ),
        "widget_rationalization_auto_applied_count": int(
            pretag_widget_rationalization.get("applied_count", 0) or 0
        ),
        "form_intelligence_auto_applied": bool(pretag_form_intelligence.get("applied")),
        "form_intelligence_auto_applied_count": int(
            pretag_form_intelligence.get("applied_count", 0) or 0
        ),
        "pretag_llm_usage": pretag_llm_usage,
        "docling_pretag_ambiguity_plan": docling_pretag_plan,
        "grounded_text_audit": pretag_grounded_text,
        "table_intelligence_audit": pretag_table_intelligence,
        "widget_rationalization_audit": pretag_widget_rationalization,
        "form_intelligence_audit": pretag_form_intelligence,
    }


async def _select_font_lanes(
    *,
    job: Job,
    settings: Settings,
    working_pdf: Path,
    structure_json: dict,
    reviewed_alts: list[dict],
    tagging_result: TaggingResult,
    validation: ValidationResult,
    font_remediation: FontRemediation,
):
    """Preserve the validation/tag-regression policy for preparatory font lanes."""
    job_id = job.id
    selected_tagging_result = tagging_result
    selected_validation = validation
    selected_output_path = Path(tagging_result.output_path)
    remediation_pdf_features = _inspect_pdf_features(working_pdf)
    font_remediation.document_features = remediation_pdf_features
    font_remediation.first_pass_errors = _error_count(validation)
    font_remediation.first_pass_warnings = _warning_count(validation)
    fidelity_source_pdf = Path(job.input_path)

    if not validation.compliant and _has_font_errors(validation.violations):
        initial_font_diagnostics = _inspect_font_diagnostics(
            selected_output_path,
            validation.violations,
        )
        font_remediation.preflight_diagnostics = initial_font_diagnostics
        unicode_gate = None
        if any(
            v.severity == "error"
            and any(marker in str(v.rule_id) for marker in FONT_UNICODE_RULE_MARKERS)
            for v in validation.violations
        ):
            gate_font_diagnostics = initial_font_diagnostics
            summary = (
                initial_font_diagnostics.get("summary")
                if isinstance(initial_font_diagnostics, dict)
                else None
            )
            profiles = (
                initial_font_diagnostics.get("profiles")
                if isinstance(initial_font_diagnostics, dict)
                else None
            )
            if (
                isinstance(summary, dict)
                and isinstance(profiles, list)
                and int(summary.get("fonts_total", 0) or 0) > len(profiles)
            ):
                gate_font_diagnostics = _inspect_font_diagnostics(
                    selected_output_path,
                    validation.violations,
                    profile_limit=512,
                )
            unicode_gate = _unicode_repair_gate_from_diagnostics(
                gate_font_diagnostics,
                violations=validation.violations,
            )
        font_remediation.unicode_gate = unicode_gate
        planned_lanes, skipped_lanes = _font_remediation_lanes(
            validation.violations,
            classification=job.classification,
            pdf_features=remediation_pdf_features,
            settings=settings,
            unicode_gate=unicode_gate,
        )
        font_remediation.lanes_planned = planned_lanes
        font_remediation.lanes_skipped = skipped_lanes
        font_remediation.eligible = bool(planned_lanes)
        font_remediation.attempted = bool(planned_lanes)

        if not planned_lanes:
            font_remediation.error = "No safe font remediation lane eligible"
        else:
            try:
                best_validation = selected_validation
                best_tagging_result = selected_tagging_result
                best_working_pdf = working_pdf
                best_structure_json = structure_json
                best_tagged_pdf = selected_output_path
                best_output_path = str(best_tagged_pdf)
                selected_lanes: list[str] = []
                ocr_messages: list[str] = []
                ocr_skipped = False

                for lane in planned_lanes:
                    attempt = await _attempt_font_lane(
                        job_id=job_id,
                        job=job,
                        settings=settings,
                        working_pdf=best_working_pdf,
                        tagged_pdf=best_tagged_pdf,
                        structure_json=best_structure_json,
                        reviewed_alts=reviewed_alts,
                        lane=lane,
                        current_tagging_result=best_tagging_result,
                    )

                    lane_result: dict[str, object] = {
                        "lane": lane,
                        "success": bool(attempt.get("success")),
                        "error": attempt.get("error"),
                        "message": attempt.get("message"),
                    }
                    details = attempt.get("details")
                    if isinstance(details, dict) and details:
                        lane_result["details"] = details

                    ocr_message = str(attempt.get("ocr_message", "")).strip()
                    if ocr_message:
                        ocr_messages.append(ocr_message)
                    ocr_skipped = ocr_skipped or bool(attempt.get("ocr_skipped", False))

                    if attempt.get("success"):
                        candidate_validation = attempt["validation"]
                        candidate_tagging_result = attempt["tagging_result"]
                        candidate_errors = _error_count(candidate_validation)
                        candidate_warnings = _warning_count(candidate_validation)
                        improved = _is_better_validation(candidate_validation, best_validation)
                        regressions = _tagging_regressions(
                            candidate_tagging_result,
                            best_tagging_result,
                        )
                        lane_result["errors"] = candidate_errors
                        lane_result["warnings"] = candidate_warnings
                        lane_result["improved"] = improved
                        if regressions:
                            lane_result["regressions"] = regressions

                        if improved and not regressions:
                            best_validation = candidate_validation
                            best_tagging_result = candidate_tagging_result
                            preprocessed_path = attempt.get("preprocessed_path")
                            requires_retag = bool(attempt.get("requires_retag", True))
                            if (
                                requires_retag
                                and isinstance(preprocessed_path, str)
                                and preprocessed_path
                            ):
                                best_working_pdf = Path(preprocessed_path)
                                fidelity_source_pdf = best_working_pdf
                            if isinstance(attempt["output_path"], Path):
                                best_tagged_pdf = attempt["output_path"]
                            else:
                                best_tagged_pdf = Path(str(attempt["output_path"]))
                            candidate_structure_json = attempt.get("structure_json")
                            if isinstance(candidate_structure_json, dict):
                                best_structure_json = candidate_structure_json
                            if not requires_retag:
                                best_working_pdf = best_tagged_pdf
                            best_output_path = str(best_tagged_pdf)
                            selected_lanes.append(lane)

                    font_remediation.lane_results.append(lane_result)

                    if best_validation.compliant:
                        # No need to keep exploring lanes after full compliance.
                        break

                if not any(bool(r.get("success")) for r in font_remediation.lane_results):
                    font_remediation.error = "All font remediation lanes failed"

                font_remediation.ocr_message = " | ".join(ocr_messages)
                font_remediation.ocr_skipped = ocr_skipped
                font_remediation.second_pass_errors = _error_count(best_validation)
                font_remediation.second_pass_warnings = _warning_count(best_validation)

                if selected_lanes:
                    selected_validation = best_validation
                    selected_tagging_result = best_tagging_result
                    selected_output_path = Path(best_output_path)
                    working_pdf = best_tagged_pdf
                    if isinstance(best_structure_json, dict):
                        structure_json = best_structure_json
                    font_remediation.selected_lane = selected_lanes[-1]
                    font_remediation.selected_lanes = selected_lanes
                    if fidelity_source_pdf != Path(job.input_path):
                        font_remediation.selected_preprocessed_path = str(fidelity_source_pdf)
                    font_remediation.applied = True
            except Exception as exc:
                logger.exception(f"Font remediation lane evaluation failed for job {job_id}")
                font_remediation.error = str(exc)

        font_remediation.postflight_diagnostics = _inspect_font_diagnostics(
            selected_output_path,
            selected_validation.violations,
        )

    return (
        selected_tagging_result,
        selected_validation,
        selected_output_path,
        working_pdf,
        structure_json,
        fidelity_source_pdf,
    )


async def _store_candidate_result(
    *,
    db: AsyncSession,
    job: Job,
    job_manager: JobManager,
    candidate: Candidate,
    alt_entries,
    review_task_metadata_overrides: dict[tuple[str, str], dict[str, object]],
) -> None:
    """Persist only the selected PDF and the changes carried by that candidate."""
    job_id = job.id
    job.output_path = str(candidate.output_path)
    job.structure_json = json.dumps(candidate.structure_json)
    validation_payload = candidate.validation_report
    fidelity_report = candidate.fidelity_report
    review_tasks = candidate.review_tasks
    if review_task_metadata_overrides:
        for key, metadata in review_task_metadata_overrides.items():
            review_tasks = _merge_review_task_metadata(
                review_tasks,
                task_type=key[0],
                source=key[1],
                metadata=metadata,
            )
    user_visible_review_tasks, fidelity_report = _prepare_user_review_surface(
        review_tasks,
        fidelity_report,
    )
    validation_payload["fidelity"] = fidelity_report
    job.validation_json = json.dumps(validation_payload)
    job.fidelity_json = json.dumps(fidelity_report)

    await db.execute(delete(ReviewTask).where(ReviewTask.job_id == job_id))
    await db.execute(
        delete(AppliedChange).where(
            AppliedChange.job_id == job_id,
            AppliedChange.review_status == "pending_review",
        )
    )
    alt_entry_by_index = {entry.figure_index: entry for entry in alt_entries}
    for change_spec in candidate.applied_change_specs:
        task_type = str(change_spec.get("task_type") or "review_change")
        metadata = change_spec.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        before = change_spec.get("before")
        if not isinstance(before, dict):
            before = {}
        after = change_spec.get("after")
        if not isinstance(after, dict):
            after = {}
        undo_payload = change_spec.get("undo_payload")
        if not isinstance(undo_payload, dict):
            undo_payload = {}
        if task_type == "figure_semantics":
            figure_index_raw = metadata.get("figure_index", undo_payload.get("figure_index"))
            try:
                figure_index = int(figure_index_raw)
            except (TypeError, ValueError):
                figure_index = -1
            entry = alt_entry_by_index.get(figure_index)
            if entry is not None:
                before = {
                    "generated_text": undo_payload.get("generated_text"),
                    "edited_text": undo_payload.get("edited_text"),
                    "status": undo_payload.get("status"),
                }
                after = {
                    "generated_text": entry.generated_text,
                    "edited_text": entry.edited_text,
                    "status": entry.status,
                }
                undo_payload = {
                    **undo_payload,
                    "entry_id": entry.id,
                    "figure_index": figure_index,
                }
        await add_applied_change(
            db=db,
            job=job,
            change_type=task_type,
            title=str(change_spec.get("title") or "Applied change"),
            detail=str(change_spec.get("detail") or "The app applied a semantic change."),
            importance=str(change_spec.get("importance") or "high"),
            reviewable=bool(change_spec.get("reviewable", True))
            and is_user_visible_applied_change_type(task_type),
            metadata=metadata,
            before=before,
            after=after,
            undo_payload=undo_payload,
        )
    for task in user_visible_review_tasks:
        metadata = task.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        db.add(
            ReviewTask(
                job_id=job_id,
                task_type=str(task.get("task_type") or "review_task"),
                title=str(task.get("title") or "Follow-up review required"),
                detail=str(task.get("detail") or ""),
                severity=str(task.get("severity") or "medium"),
                blocking=bool(task.get("blocking", True)),
                status=str(task.get("status") or "pending_review"),
                source=str(task.get("source") or "fidelity"),
                metadata_json=json.dumps(metadata),
            )
        )

    blocking_task_count = _blocking_task_count(review_tasks)
    await _update_step(
        db,
        job_id,
        "fidelity",
        "complete",
        result={
            "passed": bool(fidelity_report.get("passed", False)),
            "blocking_tasks": blocking_task_count,
            "advisory_tasks": len(review_tasks) - blocking_task_count,
        },
    )
    job_manager.emit_progress(
        job_id,
        step="fidelity",
        status="complete",
        result={
            "passed": bool(fidelity_report.get("passed", False)),
            "blocking_tasks": blocking_task_count,
            "advisory_tasks": len(review_tasks) - blocking_task_count,
        },
    )

    raise_if_model_access_refused()

    # Done!
    final_status = (
        "complete"
        if candidate.validation.compliant and bool(fidelity_report.get("passed", False))
        else "manual_remediation"
    )
    job.status = final_status
    await db.commit()
    job_manager.emit_progress(job_id, step="review", status=final_status)
    logger.info(f"Pipeline complete for job {job_id} with status={final_status}")


async def _run_tagging_and_validation(
    job_id: str,
    db: AsyncSession,
    settings: Settings,
    job_manager: JobManager,
    working_pdf: Path | None,
    structure_json: dict | None,
    pre_applied_change_specs: list[dict[str, object]] | None,
):
    job = await db.get(Job, job_id)
    if not job:
        return

    if working_pdf is None:
        # Check if an OCR'd version exists in the processing directory
        ocred_path = settings.processing_dir / job_id / "ocred.pdf"
        working_pdf = ocred_path if ocred_path.exists() else Path(job.input_path)
    if structure_json is None:
        if job.structure_json:
            structure_json = json.loads(
                job.structure_json
            )  # let JSONDecodeError propagate — fail the job
        else:
            structure_json = {}

    current_step = "validation"
    try:
        job.status = "processing"
        await db.commit()

        baseline_validation = await validate_pdf(
            pdf_path=working_pdf,
            verapdf_path=settings.verapdf_path,
            flavour=settings.verapdf_flavour,
            timeout_seconds=settings.subprocess_timeout_validation,
        )

        raise_if_model_access_refused()

        # ── Step 5: Tagging ──
        current_step = "tagging"
        await _update_step(db, job_id, "tagging", "running")
        job_manager.emit_progress(job_id, step="tagging", status="running")

        (
            working_pdf, structure_json, alt_entries, reviewed_alts, pretag_details,
        ) = await _prepare_tagging_inputs(job, db, settings, working_pdf, structure_json)

        async def on_tagged(tagging_result):
            nonlocal current_step
            await _update_step(
                db, job_id, "tagging", "complete",
                result=_tagging_step_result(tagging_result, pretag_details),
            )
            job_manager.emit_progress(job_id, step="tagging", status="complete")
            current_step = "validation"
            await _update_step(db, job_id, "validation", "running")
            job_manager.emit_progress(job_id, step="validation", status="running")

        font_remediation = FontRemediation()

        async def on_validated(candidate):
            nonlocal current_step
            result = {
                "compliant": candidate.validation.compliant,
                "font_remediation_attempted": bool(font_remediation.attempted),
                "font_remediation_applied": bool(font_remediation.applied),
            }
            await _update_step(
                db, job_id, "validation", "complete",
                result={**result, "violations_count": len(candidate.validation.violations)},
            )
            job_manager.emit_progress(job_id, step="validation", status="complete", result=result)
            current_step = "fidelity"
            await _update_step(db, job_id, "fidelity", "running")
            job_manager.emit_progress(job_id, step="fidelity", status="running")

        llm_font_map_auto = {
            "enabled": False,
            "attempted": False, "applied": False, "reason": "",
        }
        evaluate = partial(
            evaluate_candidate,
            job=job, settings=settings, reviewed_alts=reviewed_alts,
            alt_entries=alt_entries, baseline_validation=baseline_validation,
            font_remediation=font_remediation, llm_font_map_auto=llm_font_map_auto,
        )
        candidate = await evaluate(
            input_path=working_pdf,
            output_path=get_output_path(job_id, f"accessible_{job.original_filename}"),
            structure_json=structure_json,
            fidelity_source_pdf=Path(job.input_path),
            applied_change_specs=pre_applied_change_specs or [],
            produced_by="initial", remediate_fonts=True,
            on_tagged=on_tagged, on_validated=on_validated,
        )
        candidate = await _retry_grounded_candidate(candidate, job=job, settings=settings, evaluate=evaluate)
        candidate = await _retry_structure_candidate(candidate, job=job, settings=settings, evaluate=evaluate)
        # The font-map patch must inherit the selected structure retry's bytes.
        candidate, metadata_overrides = await _retry_font_map_candidate(
            candidate, job=job, settings=settings, evaluate=evaluate,
            baseline_validation=baseline_validation, font_remediation=font_remediation,
            llm_font_map_auto=llm_font_map_auto,
        )
        # Recheck the chosen output, as before, before publishing the review surface.
        await _assess_candidate_fidelity(candidate, job=job, settings=settings)
        await _store_candidate_result(
            db=db, job=job, job_manager=job_manager, candidate=candidate,
            alt_entries=alt_entries, review_task_metadata_overrides=metadata_overrides,
        )

    except Exception as e:
        logger.exception(f"Tagging/validation failed for job {job_id}")
        await db.rollback()
        user_error = re.sub(r"/\S*", "", str(e)).strip(": ")
        await _update_step(
            db,
            job_id,
            current_step,
            "failed",
            error=user_error or str(e),
        )
        job = await db.get(Job, job_id)
        if job:
            job.status = "failed"
            job.error = user_error or "Tagging/validation failed"
            clear_terminal_artifacts(job)
            await db.commit()
        job_manager.emit_progress(
            job_id,
            step=current_step,
            status="failed",
            message=user_error,
        )
