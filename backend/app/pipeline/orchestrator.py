"""Pipeline orchestrator: runs all steps in sequence with progress events."""

import asyncio
import copy
import json
import logging
import re
import shutil
from functools import lru_cache
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pikepdf

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import AltTextEntry, AppliedChange, Job, JobStep, ReviewTask
from app.pipeline.alt_text import figure_applied_change_specs, generate_alt_text
from app.pipeline.classify import classify_pdf
from app.pipeline.fidelity import _table_semantics_risk, assess_fidelity
from app.pipeline.ocr import run_ocr
from app.pipeline.structure import extract_structure
from app.pipeline.subprocess_utils import SubprocessTimeout, communicate_with_timeout
from app.pipeline.tagger import tag_pdf
from app.pipeline.validator import validate_pdf
from app.services.grounded_text_apply import (
    PRETAG_GROUNDED_TEXT_TARGET_LIMIT,
    apply_grounded_text_resolutions_to_structure as _apply_grounded_text_resolutions_to_structure,
    collect_safe_grounded_text_resolutions as _collect_safe_grounded_text_resolutions,
    has_grounded_text_candidate_task as _has_grounded_text_candidate_task,
    should_auto_apply_grounded_code_block as _should_auto_apply_grounded_code_block,
    should_auto_apply_grounded_encoding_block as _should_auto_apply_grounded_encoding_block,
)
from app.services.grounded_text_review import (
    adjudicate_grounded_text_candidates as _adjudicate_grounded_text_candidates,
    apply_grounded_text_adjudication as _apply_grounded_text_adjudication,
    blocking_review_task_count as _blocking_review_task_count,
    recalculate_fidelity_summary as _recalculate_fidelity_summary,
    update_grounded_text_check as _update_grounded_text_check,
)
from app.services.form_fields import apply_field_accessible_names
from app.services.font_review_auto import (
    attempt_auto_llm_font_map as _attempt_auto_llm_font_map,
    fidelity_not_worse as _fidelity_not_worse,
)
from app.services.intelligence_gemini_forms import (
    generate_form_intelligence,
    generate_form_intelligence_for_page,
)
from app.services.intelligence_gemini_tables import generate_table_intelligence
from app.services.file_storage import create_job_dir, get_output_path
from app.services.job_manager import JobManager
from app.services.intelligence_gemini_pages import generate_suspicious_text_intelligence
from app.services.llm_client import LlmClient as _LlmClient, make_llm_client
from app.services.page_intelligence import collect_grounded_text_candidates
from app.services.applied_changes import add_applied_change
from app.services.auto_review_apply import auto_apply_structure_review_tasks
from app.services.runtime_paths import enriched_subprocess_env, resolve_binary
from app.services.semantic_pretag_policy import (
    apply_table_intelligence_to_element as _apply_table_intelligence_to_element,
    form_targets_for_intelligence as _form_targets_for_intelligence,
    should_auto_apply_form_intelligence as _should_auto_apply_form_intelligence,
    should_auto_apply_table_intelligence as _should_auto_apply_table_intelligence,
    should_retry_table_intelligence_aggressively as _should_retry_table_intelligence_aggressively,
    should_retry_table_intelligence_confirm_existing as _should_retry_table_intelligence_confirm_existing,
    table_page_structure_fragments as _table_page_structure_fragments,
    table_targets_with_cells as _table_targets_with_cells,
)
from app.services.toc_suggestions import enhance_toc_structure_with_llm
from app.services.validation_compare import is_better_validation as _is_better_validation

logger = logging.getLogger(__name__)

# Preserved for test monkeypatch compatibility around LLM-backed pre-tag helpers.
LlmClient = _LlmClient


FONT_RULE_FRAGMENT = "-7.21."
FONT_LANE_REPAIR_DICTS = "repair_font_dicts"
FONT_LANE_REPAIR_TOUNICODE = "repair_tounicode"
FONT_LANE_EMBED = "embed_fonts"
FONT_LANE_OCR_REDO = "ocr_redo"
FONT_LANE_OCR_FORCE = "ocr_force"
FONT_EMBED_RULE_MARKERS = ("-7.21.3.2-", "-7.21.4.")
FONT_UNICODE_RULE_MARKERS = ("-7.21.7-", "-7.21.8-")
FONT_DICT_REPAIR_RULE_MARKERS = ("-7.21.3.2-", "-7.21.4.2-")
FIGURE_RECLASSIFY_KINDS = {"table", "form_region", "artifact"}
FONT_SUBSET_RE = re.compile(r"^[A-Z]{6}\+.+")
FONT_NAME_RE = re.compile(r"[^A-Za-z0-9]+")
HEX_STR_RE = re.compile(r"<([0-9A-Fa-f]+)>")
MAX_TOUNICODE_BFRANGE_SPAN = 65536
SAFE_IMPLICIT_STANDARD_BASEFONTS = frozenset({
    "TimesRoman",
    "TimesBold",
    "TimesItalic",
    "TimesBoldItalic",
    "Helvetica",
    "HelveticaBold",
    "HelveticaOblique",
    "HelveticaBoldOblique",
    "Courier",
    "CourierBold",
    "CourierOblique",
    "CourierBoldOblique",
})
SYSTEM_FONT_EXTENSIONS = {".ttf", ".otf", ".ttc"}
SYSTEM_FONT_DIRS = (
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
    Path.home() / "Library/Fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
)
GHOSTSCRIPT_ALWAYS_EMBED_FONTS = (
    "/Courier",
    "/Courier-Bold",
    "/Courier-Oblique",
    "/Courier-BoldOblique",
    "/Helvetica",
    "/Helvetica-Bold",
    "/Helvetica-Oblique",
    "/Helvetica-BoldOblique",
    "/Times-Roman",
    "/Times-Bold",
    "/Times-Italic",
    "/Times-BoldItalic",
    "/Symbol",
    "/ZapfDingbats",
)
GHOSTSCRIPT_TYPE1_SUBSTITUTES = {
    "timesroman": "NimbusRoman-Regular",
    "timesbold": "NimbusRoman-Bold",
    "timesitalic": "NimbusRoman-Italic",
    "timesbolditalic": "NimbusRoman-BoldItalic",
    "helvetica": "NimbusSans-Regular",
    "helveticabold": "NimbusSans-Bold",
    "helveticaoblique": "NimbusSans-Italic",
    "helveticaboldoblique": "NimbusSans-BoldItalic",
    "courier": "NimbusMonoPS-Regular",
    "courierbold": "NimbusMonoPS-Bold",
    "courieroblique": "NimbusMonoPS-Italic",
    "courierboldoblique": "NimbusMonoPS-BoldItalic",
    "symbol": "StandardSymbolsPS",
    "zapfdingbats": "D050000L",
}
GHOSTSCRIPT_TYPE1_DESCRIPTOR_SPECS = {
    "timesroman": {"afm": "n021003l.afm", "flags": 34},
    "timesbold": {"afm": "n021004l.afm", "flags": 34},
    "timesitalic": {"afm": "n021023l.afm", "flags": 98},
    "timesbolditalic": {"afm": "n021024l.afm", "flags": 98},
    "helvetica": {"afm": "n019003l.afm", "flags": 32},
    "helveticabold": {"afm": "n019004l.afm", "flags": 32},
    "helveticaoblique": {"afm": "n019023l.afm", "flags": 96},
    "helveticaboldoblique": {"afm": "n019024l.afm", "flags": 96},
    "courier": {"afm": "n022003l.afm", "flags": 33},
    "courierbold": {"afm": "n022004l.afm", "flags": 33},
    "courieroblique": {"afm": "n022023l.afm", "flags": 97},
    "courierboldoblique": {"afm": "n022024l.afm", "flags": 97},
    "symbol": {"afm": "s050000l.afm", "flags": 4},
    "zapfdingbats": {"afm": "d050000l.afm", "flags": 4},
}
# ──────────────────────────────────────────────────────────────────────────────
# Shared pikepdf helpers (used across multiple font-repair functions)
# ──────────────────────────────────────────────────────────────────────────────


def _obj_key(obj) -> tuple[int, int] | None:
    """Return the (objgen) identity tuple for a pikepdf indirect object."""
    try:
        obj_num, gen_num = obj.objgen
        if isinstance(obj_num, int) and isinstance(gen_num, int) and obj_num > 0:
            return obj_num, gen_num
    except Exception:
        return None
    return None


def _resolve_dictionary(obj):
    """Resolve a pikepdf reference to a Dictionary (or dict-like) object."""
    if obj is None:
        return None
    try:
        if isinstance(obj, pikepdf.Dictionary):
            return obj
        if hasattr(obj, "keys") and hasattr(obj, "get"):
            return obj
        return obj.get_object()
    except Exception:
        return None


def _join_messages(a: str | None, b: str | None) -> str | None:
    """Join two optional message strings with ' | ', dropping None/empty values."""
    if a and b:
        return f"{a} | {b}"
    return a or b


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

        changes.append({
            "rule_id": rule_id,
            "description": source.get("description", "Unknown violation"),
            "severity": source.get("severity", "error"),
            "location": source.get("location"),
            "category": source.get("category"),
            "fix_hint": source.get("fix_hint"),
            "baseline_count": before.get("count", 0) if before else 0,
            "post_count": after.get("count", 0) if after else 0,
            "remediation_status": remediation_status,
        })

    return changes, status_by_rule


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
    font_remediation: dict[str, object],
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
    needs_remediation = len(
        [c for c in changes if c["remediation_status"] == "needs_remediation"]
    )
    auto_remediated = len(
        [c for c in changes if c["remediation_status"] == "auto_remediated"]
    )
    manual_remediated = len(
        [c for c in changes if c["remediation_status"] == "manual_remediated"]
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
        "font_remediation": font_remediation,
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
        "generated_at": datetime.now(timezone.utc).isoformat(),
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
            "lang_set": tagging_result.lang_set,
        },
        "claims": claims_payload,
    }


def _blocking_task_count(review_tasks: list[dict[str, object]]) -> int:
    return len([task for task in review_tasks if bool(task.get("blocking"))])


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


def _apply_figure_reclassification(
    structure_json: dict[str, object],
    alt_texts: list[object],
) -> tuple[dict[str, object], dict[str, object]]:
    audit: dict[str, object] = {
        "attempted": False,
        "applied": False,
        "reason": "",
        "candidate_count": 0,
        "removed_count": 0,
        "removed_indexes": [],
        "removed_kinds": {},
        "pages": [],
    }
    if not isinstance(structure_json, dict):
        audit["reason"] = "missing_structure"
        return structure_json, audit

    reclassified: dict[int, str] = {}
    for item in alt_texts:
        figure_index = getattr(item, "figure_index", None)
        status = str(getattr(item, "status", "") or "").strip()
        resolved_kind = str(getattr(item, "resolved_kind", "") or "").strip()
        if (
            isinstance(figure_index, int)
            and status == "reclassified"
            and resolved_kind in FIGURE_RECLASSIFY_KINDS
        ):
            reclassified[figure_index] = resolved_kind

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
    for element in elements:
        if not isinstance(element, dict):
            updated_elements.append(element)
            continue
        if element.get("type") == "figure" and isinstance(element.get("figure_index"), int):
            figure_index = int(element["figure_index"])
            resolved_kind = reclassified.get(figure_index)
            if resolved_kind:
                removed_indexes.append(figure_index)
                page = element.get("page")
                if isinstance(page, int) and page >= 0:
                    removed_pages.add(page + 1)
                removed_kinds[resolved_kind] = removed_kinds.get(resolved_kind, 0) + 1
                continue
        updated_elements.append(element)

    if not removed_indexes:
        audit["reason"] = "no_matching_structure_elements"
        return structure_json, audit

    updated["elements"] = updated_elements
    audit["applied"] = True
    audit["reason"] = "applied"
    audit["removed_count"] = len(removed_indexes)
    audit["removed_indexes"] = sorted(removed_indexes)
    audit["removed_kinds"] = removed_kinds
    audit["pages"] = sorted(removed_pages)
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

    llm_client = make_llm_client(settings)
    try:
        adjudication = await generate_suspicious_text_intelligence(
            job=job,
            page_numbers=pages_to_check,
            suspicious_blocks=suspicious_blocks,
            llm_client=llm_client,
        )
    except Exception as exc:
        audit["reason"] = f"llm_failed: {exc}"
        return structure_json, audit
    finally:
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
) -> tuple[dict[str, object], dict[str, object]]:
    audit: dict[str, object] = {
        "enabled": bool(settings.auto_apply_table_intelligence),
        "attempted": False,
        "applied": False,
        "reason": "",
        "candidate_count": 0,
        "applied_count": 0,
        "pages": [],
        "review_ids": [],
        "confirmed_count": 0,
        "set_headers_count": 0,
        "aggressive_retry_count": 0,
        "confirm_existing_retry_count": 0,
    }
    if not settings.auto_apply_table_intelligence:
        audit["reason"] = "disabled"
        return structure_json, audit
    if not isinstance(structure_json, dict):
        audit["reason"] = "missing_structure"
        return structure_json, audit

    table_risk = _table_semantics_risk(structure_json)
    targets = table_risk.get("targets")
    if not isinstance(targets, list) or not targets:
        audit["reason"] = "no_candidates"
        return structure_json, audit
    detailed_targets = _table_targets_with_cells(
        structure_json,
        {
            str(target.get("table_review_id") or "").strip()
            for target in targets
            if isinstance(target, dict)
        },
    )
    audit["attempted"] = True
    audit["candidate_count"] = len(targets)

    llm_client = make_llm_client(settings)
    try:
        intelligence_items: list[dict[str, object]] = []
        targets_by_review_id = {
            str(target.get("table_review_id") or "").strip(): target
            for target in targets
            if isinstance(target, dict) and str(target.get("table_review_id") or "").strip()
        }
        for target in targets:
            if not isinstance(target, dict):
                continue
            review_id = str(target.get("table_review_id") or "").strip()
            page = target.get("page")
            page_fragments = _table_page_structure_fragments(
                structure_json,
                page_numbers=[int(page)] if isinstance(page, int) else [],
            )
            detailed_target = dict(detailed_targets.get(review_id) or target)
            if isinstance(target.get("risk_reasons"), list):
                detailed_target["risk_reasons"] = list(target.get("risk_reasons") or [])
            if isinstance(target.get("risk_score"), (int, float)):
                detailed_target["risk_score"] = float(target.get("risk_score"))
            intelligence = await generate_table_intelligence(
                job=job,
                target=detailed_target,
                page_structure_fragments=page_fragments,
                llm_client=llm_client,
            )
            intelligence_items.append(intelligence)
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
            detailed_target = dict(detailed_targets.get(review_id) or target)
            if isinstance(target.get("risk_reasons"), list):
                detailed_target["risk_reasons"] = list(target.get("risk_reasons") or [])
            if isinstance(target.get("risk_score"), (int, float)):
                detailed_target["risk_score"] = float(target.get("risk_score"))
            page = detailed_target.get("page")
            page_fragments = _table_page_structure_fragments(
                structure_json,
                page_numbers=[int(page)] if isinstance(page, int) else [],
            )
            retried_item = await generate_table_intelligence(
                job=job,
                target=detailed_target,
                page_structure_fragments=page_fragments,
                llm_client=llm_client,
                aggressive=True,
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
            detailed_target = dict(detailed_targets.get(review_id) or target)
            if isinstance(target.get("risk_reasons"), list):
                detailed_target["risk_reasons"] = list(target.get("risk_reasons") or [])
            if isinstance(target.get("risk_score"), (int, float)):
                detailed_target["risk_score"] = float(target.get("risk_score"))
            page = detailed_target.get("page")
            page_fragments = _table_page_structure_fragments(
                structure_json,
                page_numbers=[int(page)] if isinstance(page, int) else [],
            )
            retried_item = await generate_table_intelligence(
                job=job,
                target=detailed_target,
                page_structure_fragments=page_fragments,
                llm_client=llm_client,
                aggressive=True,
                confirm_existing=True,
            )
            intelligence_items[idx] = retried_item
            confirm_existing_retried += 1
        audit["confirm_existing_retry_count"] = confirm_existing_retried
    except Exception as exc:
        audit["reason"] = f"llm_failed: {exc}"
        return structure_json, audit
    finally:
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


async def _apply_pretag_form_intelligence(
    *,
    job: Job,
    settings: Settings,
    working_pdf: Path,
    structure_json: dict[str, object],
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
        "pages": [],
        "review_ids": [],
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

    targets = _form_targets_for_intelligence(
        working_pdf=working_pdf,
        structure_json=structure_json,
    )
    if not targets:
        audit["reason"] = "no_candidates"
        return working_pdf, audit

    audit["attempted"] = True
    audit["candidate_count"] = len(targets)
    llm_client = make_llm_client(settings)
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
                return await generate_form_intelligence(
                    job=job,
                    target={key: value for key, value in target.items() if key != "nearby_blocks"},
                    nearby_blocks=list(target.get("nearby_blocks") or []),
                    llm_client=llm_client,
                )

        async def _generate_page(page_number: int, page_targets: list[dict[str, object]]) -> list[dict[str, object]]:
            async with semaphore:
                return await generate_form_intelligence_for_page(
                    job=job,
                    page_number=page_number,
                    targets=page_targets,
                    llm_client=llm_client,
                )

        intelligence_items: list[dict[str, object]] = []
        page_items_by_id: dict[str, dict[str, object]] = {}
        for page_number, page_targets in sorted(targets_by_page.items()):
            try:
                page_items = await _generate_page(page_number, page_targets)
            except Exception:
                audit["page_batch_failed_count"] = int(audit.get("page_batch_failed_count", 0)) + 1
                continue
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
                *[_generate(target) for target in unresolved_targets]
            )
            for item in fallback_items:
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
    audit["reason"] = "applied"
    return patched_pdf, audit


def _font_only_errors(violations) -> bool:
    errors = [v for v in violations if v.severity == "error"]
    if not errors:
        return False
    return all(FONT_RULE_FRAGMENT in str(v.rule_id) for v in errors)


def _has_font_errors(violations) -> bool:
    return any(
        v.severity == "error" and FONT_RULE_FRAGMENT in str(v.rule_id)
        for v in violations
    )


def _inspect_pdf_features(pdf_path: Path) -> dict[str, int | bool]:
    """Collect lightweight PDF features used for remediation risk gating."""
    features: dict[str, int | bool] = {
        "pages": 0,
        "link_annots": 0,
        "has_forms": False,
        "fonts_total": 0,
        "unembedded_fonts": 0,
        "ocr_suspect_fonts": 0,
    }
    try:
        import pikepdf

        seen_resources: set[tuple[int, int]] = set()
        seen_fonts: set[tuple[int, int]] = set()
        seen_appearances: set[tuple[int, int]] = set()

        def _has_embedded_font(descriptor) -> bool:
            if not isinstance(descriptor, pikepdf.Dictionary):
                return False
            return any(
                key in descriptor
                for key in (
                    pikepdf.Name("/FontFile"),
                    pikepdf.Name("/FontFile2"),
                    pikepdf.Name("/FontFile3"),
                )
            )

        def _walk_resources(resources) -> None:
            if not _is_pdf_mapping(resources):
                return
            resources_key = _obj_key(resources)
            if resources_key and resources_key in seen_resources:
                return
            if resources_key:
                seen_resources.add(resources_key)

            fonts = _resolve_dictionary(resources.get("/Font"))
            if isinstance(fonts, pikepdf.Dictionary):
                for _, font_obj in fonts.items():
                    font_dict = _resolve_dictionary(font_obj)
                    if not isinstance(font_dict, pikepdf.Dictionary):
                        continue

                    font_key = _obj_key(font_dict)
                    if font_key and font_key in seen_fonts:
                        continue
                    if font_key:
                        seen_fonts.add(font_key)

                    features["fonts_total"] = int(features["fonts_total"]) + 1
                    subtype = font_dict.get("/Subtype")
                    descriptor = None
                    base_font_name = str(font_dict.get("/BaseFont") or "")
                    if subtype == pikepdf.Name("/Type0"):
                        descendants = font_dict.get("/DescendantFonts")
                        if isinstance(descendants, pikepdf.Array) and descendants:
                            cid_font = _resolve_dictionary(descendants[0])
                            if isinstance(cid_font, pikepdf.Dictionary):
                                descriptor = _resolve_dictionary(cid_font.get("/FontDescriptor"))
                                base_font_name = str(
                                    cid_font.get("/BaseFont")
                                    or font_dict.get("/BaseFont")
                                    or ""
                                )
                    else:
                        descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
                        if isinstance(descriptor, pikepdf.Dictionary):
                            base_font_name = str(
                                font_dict.get("/BaseFont")
                                or descriptor.get("/FontName")
                                or ""
                            )

                    if not _has_embedded_font(descriptor):
                        features["unembedded_fonts"] = int(features["unembedded_fonts"]) + 1
                    if _looks_like_ocr_overlay_font(base_font_name):
                        features["ocr_suspect_fonts"] = int(features["ocr_suspect_fonts"]) + 1

            xobjects = _resolve_dictionary(resources.get("/XObject"))
            if not isinstance(xobjects, pikepdf.Dictionary):
                return
            for _, xobject in xobjects.items():
                xobject_dict = _resolve_dictionary(xobject)
                if not _is_pdf_mapping(xobject_dict):
                    continue
                try:
                    subtype = xobject_dict.get("/Subtype")
                except Exception:
                    continue
                if subtype != pikepdf.Name("/Form"):
                    continue
                _walk_resources(_resolve_dictionary(xobject_dict.get("/Resources")))

        def _walk_appearance_object(obj) -> None:
            appearance_obj = _resolve_dictionary(obj)
            if not _is_pdf_mapping(appearance_obj):
                return
            appearance_key = _obj_key(appearance_obj)
            if appearance_key and appearance_key in seen_appearances:
                return
            if appearance_key:
                seen_appearances.add(appearance_key)

            resources = _resolve_dictionary(appearance_obj.get("/Resources"))
            _walk_resources(resources)
            for key in ("/N", "/R", "/D"):
                child = appearance_obj.get(key)
                if child is not None:
                    _walk_appearance_object(child)

        with pikepdf.open(str(pdf_path)) as pdf:
            features["pages"] = len(pdf.pages)
            acroform = _resolve_dictionary(pdf.Root.get("/AcroForm"))
            has_form_fields = False
            if isinstance(acroform, pikepdf.Dictionary):
                fields = acroform.get("/Fields")
                has_form_fields = isinstance(fields, pikepdf.Array) and len(fields) > 0
            links = 0
            widget_annots = 0
            for page in pdf.pages:
                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for annot in annots:
                        try:
                            subtype = annot.get("/Subtype")
                            if subtype == pikepdf.Name("/Link"):
                                links += 1
                            elif subtype == pikepdf.Name("/Widget"):
                                widget_annots += 1
                        except Exception:
                            continue
            features["link_annots"] = links
            features["has_forms"] = has_form_fields or widget_annots > 0
            _walk_pdf_resource_graph(
                pdf,
                resolve_dictionary=_resolve_dictionary,
                walk_resources=_walk_resources,
                walk_appearance_object=_walk_appearance_object,
            )
    except Exception as exc:
        logger.warning(f"Failed to inspect PDF features for remediation gating: {exc}")
    return features


def _ocr_lane_skip_reasons(
    classification: str | None,
    pdf_features: dict[str, int | bool],
    settings: Settings,
) -> list[str]:
    reasons: list[str] = []
    classification_value = (classification or "").strip().lower()
    ocr_suspect_fonts = int(pdf_features.get("ocr_suspect_fonts", 0) or 0)
    if (
        classification_value == "digital"
        and not settings.font_remediation_allow_ocr_on_digital
        and ocr_suspect_fonts <= 0
    ):
        reasons.append("digital document")
    if bool(pdf_features.get("has_forms", False)):
        reasons.append("fillable forms present")
    page_count = int(pdf_features.get("pages", 0))
    page_limit = settings.font_remediation_ocr_max_pages
    if ocr_suspect_fonts > 0:
        page_limit = max(page_limit, settings.font_remediation_ocr_suspect_max_pages)
    if page_count > page_limit:
        reasons.append(f"page count {page_count} > limit {page_limit}")
    return reasons


def _font_remediation_lanes(
    violations,
    classification: str | None,
    pdf_features: dict[str, int | bool],
    settings: Settings,
    unicode_gate: dict[str, object] | None = None,
) -> tuple[list[str], list[str]]:
    """Choose remediation lanes based on rule family and document risk profile."""
    error_rule_ids = [str(v.rule_id) for v in violations if v.severity == "error"]
    has_embed_rules = any(
        any(marker in rule_id for marker in FONT_EMBED_RULE_MARKERS)
        for rule_id in error_rule_ids
    )
    has_unicode_rules = any(
        any(marker in rule_id for marker in FONT_UNICODE_RULE_MARKERS)
        for rule_id in error_rule_ids
    )
    has_dict_repair_rules = any(
        any(marker in rule_id for marker in FONT_DICT_REPAIR_RULE_MARKERS)
        for rule_id in error_rule_ids
    )
    has_unembedded_fonts = int(pdf_features.get("unembedded_fonts", 0)) > 0
    allow_unicode_lane = has_unicode_rules
    skipped: list[str] = []
    if has_unicode_rules and unicode_gate is not None:
        safe_candidates = int(unicode_gate.get("safe_candidate_count", 0) or 0)
        if safe_candidates <= 0:
            allow_unicode_lane = False
            reason = str(unicode_gate.get("reason", "")).strip() or "no deterministic font candidates"
            skipped.append("ToUnicode repair skipped: " + reason)

    lanes: list[str] = []
    if has_dict_repair_rules:
        lanes.append(FONT_LANE_REPAIR_DICTS)
    if allow_unicode_lane and not has_unembedded_fonts:
        lanes.append(FONT_LANE_REPAIR_TOUNICODE)
    lanes.append(FONT_LANE_EMBED)
    if allow_unicode_lane and has_unembedded_fonts:
        lanes.append(FONT_LANE_REPAIR_TOUNICODE)

    # OCR-based lanes are only used for Unicode/.notdef families and only when low-risk.
    if has_unicode_rules:
        ocr_skip_reasons = _ocr_lane_skip_reasons(classification, pdf_features, settings)
        if ocr_skip_reasons:
            skipped.append("OCR lanes skipped: " + "; ".join(ocr_skip_reasons))
        else:
            lanes.append(FONT_LANE_OCR_REDO)
            if settings.font_remediation_enable_force_ocr:
                lanes.append(FONT_LANE_OCR_FORCE)

    # If we only saw Unicode rules and embed_fonts was not considered useful, keep it anyway
    # as a low-risk first attempt.
    if not has_embed_rules and not has_unicode_rules:
        skipped.append("No known font remediation rule family matched")

    # De-duplicate while preserving order.
    unique_lanes: list[str] = []
    for lane in lanes:
        if lane not in unique_lanes:
            unique_lanes.append(lane)

    return unique_lanes, skipped


def _walk_pdf_resource_graph(
    pdf,
    *,
    resolve_dictionary,
    walk_resources,
    walk_appearance_object,
) -> None:
    import pikepdf

    seen_fields: set[tuple[int, int]] = set()

    def _walk_field(field_obj) -> None:
        field = resolve_dictionary(field_obj)
        if not _is_pdf_mapping(field):
            return

        field_key = getattr(field, "objgen", None)
        if field_key and field_key in seen_fields:
            return
        if field_key:
            seen_fields.add(field_key)

        walk_resources(resolve_dictionary(field.get("/DR")))
        walk_appearance_object(field.get("/AP"))

        kids = field.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                _walk_field(kid)

    for page in pdf.pages:
        walk_resources(resolve_dictionary(page.get("/Resources")))
        annots = page.get("/Annots")
        if isinstance(annots, pikepdf.Array):
            for annot in annots:
                walk_appearance_object(annot.get("/AP"))

    acroform = resolve_dictionary(pdf.Root.get("/AcroForm"))
    if _is_pdf_mapping(acroform):
        walk_resources(resolve_dictionary(acroform.get("/DR")))
        fields = acroform.get("/Fields")
        if isinstance(fields, pikepdf.Array):
            for field in fields:
                _walk_field(field)


def _is_pdf_mapping(obj) -> bool:
    return obj is not None and hasattr(obj, "get") and hasattr(obj, "keys")


def _local_embed_support_kind(font_dict, descendant_subtype=None) -> str:
    import pikepdf

    subtype = font_dict.get("/Subtype")
    if subtype == pikepdf.Name("/TrueType"):
        return "truetype"
    if (
        subtype in (pikepdf.Name("/Type1"), pikepdf.Name("/MMType1"))
        and _normalize_font_name(_base_font_name(font_dict)) in GHOSTSCRIPT_TYPE1_SUBSTITUTES
    ):
        return "type1_standard14"
    if (
        subtype == pikepdf.Name("/Type0")
        and descendant_subtype == pikepdf.Name("/CIDFontType2")
    ):
        return "cidfonttype2"
    return ""


def _embed_lane_should_skip_local(font_diagnostics: dict[str, object] | None) -> bool:
    if not isinstance(font_diagnostics, dict):
        return False
    summary = font_diagnostics.get("summary")
    if not isinstance(summary, dict):
        return False
    return (
        int(summary.get("unembedded_fonts", 0) or 0) > 0
        and int(summary.get("local_embed_candidate_count", 0) or 0) <= 0
    )


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
            regressions.append(f"{label} dropped significantly ({current_count} -> {candidate_count})")

    current_links = _count(current, "links_tagged")
    candidate_links = _count(candidate, "links_tagged")
    if current_links > 0 and candidate_links < current_links:
        regressions.append(f"links decreased ({current_links} -> {candidate_links})")

    return regressions


def _strip_subset_prefix(font_name: str | None) -> str:
    raw = str(font_name or "").strip().lstrip("/")
    if FONT_SUBSET_RE.match(raw):
        return raw.split("+", 1)[1]
    return raw


def _normalize_font_name(font_name: str | None) -> str:
    return FONT_NAME_RE.sub("", _strip_subset_prefix(font_name)).lower()


def _looks_like_ocr_overlay_font(font_name: str | None) -> bool:
    normalized = _normalize_font_name(font_name)
    if not normalized:
        return False
    return any(token in normalized for token in ("ocr", "glyphless", "hidden"))


def _coerce_metric_int(value, default: int = 0) -> int:
    """Parse AFM-style numeric values that may arrive as ints, floats, or numeric strings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return default


def _base_font_name(font_dict) -> str:
    base_font = font_dict.get("/BaseFont")
    if base_font:
        return _strip_subset_prefix(str(base_font))
    return ""


def _simple_font_auto_unicode_policy(font_dict, font_bytes: bytes | None = None) -> str:
    import pikepdf

    encoding = font_dict.get("/Encoding")
    if isinstance(encoding, pikepdf.Dictionary):
        if encoding.get("/BaseEncoding") is not None or encoding.get("/Differences") is not None:
            return "explicit"
    elif encoding is not None:
        return "explicit"

    base_font = FONT_NAME_RE.sub("", _base_font_name(font_dict))
    if base_font in SAFE_IMPLICIT_STANDARD_BASEFONTS:
        return "standard14"
    if (
        font_bytes
        and font_dict.get("/Subtype") in (pikepdf.Name("/Type1"), pikepdf.Name("/MMType1"))
        and _cff_builtin_encoding_map(font_bytes)
    ):
        return "embedded_cff"
    return "blocked"


def _unicode_repair_gate_from_diagnostics(
    font_diagnostics: dict[str, object] | None,
    *,
    violations=None,
) -> dict[str, object]:
    """Decide whether automatic ToUnicode repair is deterministic enough to run."""
    profile: dict[str, object] = {
        "allow_automatic": False,
        "safe_type0_candidates": 0,
        "safe_simple_candidates": 0,
        "blocked_simple_fonts": 0,
        "safe_candidate_count": 0,
        "blocked_candidate_count": 0,
        "blocked_examples": [],
        "reason": "",
    }
    has_invalid_unicode_rules = any(
        v.severity == "error" and "7.21.7-2" in str(v.rule_id)
        for v in (violations or [])
    )
    has_unicode_rule_family = any(
        v.severity == "error"
        and any(marker in str(v.rule_id) for marker in FONT_UNICODE_RULE_MARKERS)
        for v in (violations or [])
    )
    raw_profiles = font_diagnostics.get("profiles") if isinstance(font_diagnostics, dict) else None
    if not isinstance(raw_profiles, list):
        profile["reason"] = "inspection_failed: missing font diagnostics"
        return profile

    for item in raw_profiles:
        if not isinstance(item, dict):
            continue
        subtype = str(item.get("subtype") or "")
        if subtype == "/Type0":
            descendant_subtype = str(item.get("descendant_subtype") or "")
            if (
                descendant_subtype == "/CIDFontType2"
                and bool(item.get("embedded"))
                and (
                    has_unicode_rule_family
                    or not bool(item.get("has_tounicode"))
                    or has_invalid_unicode_rules
                )
            ):
                profile["safe_type0_candidates"] = int(profile["safe_type0_candidates"]) + 1
            continue

        if subtype not in ("/Type1", "/MMType1", "/TrueType"):
            continue

        needs_repair = (
            not bool(item.get("has_tounicode"))
            or int(item.get("invalid_tounicode_entries", 0) or 0) > 0
            or int(item.get("missing_used_code_count", 0) or 0) > 0
            or has_invalid_unicode_rules
        )
        if not needs_repair:
            continue

        policy = str(item.get("auto_unicode_policy") or "")
        repair_evidence = (
            int(item.get("invalid_tounicode_entries", 0) or 0) > 0
            or int(item.get("repairable_missing_used_codes", 0) or 0) > 0
        )
        if policy in {"explicit", "standard14", "embedded_cff"} and repair_evidence:
            profile["safe_simple_candidates"] = int(profile["safe_simple_candidates"]) + 1
            continue

        profile["blocked_simple_fonts"] = int(profile["blocked_simple_fonts"]) + 1
        examples = profile["blocked_examples"]
        if isinstance(examples, list) and len(examples) < 5:
            name = str(item.get("base_font") or "(unnamed)")
            if name not in examples:
                examples.append(name)

    safe_candidates = int(profile["safe_type0_candidates"]) + int(profile["safe_simple_candidates"])
    blocked_candidates = int(profile["blocked_simple_fonts"])
    profile["safe_candidate_count"] = safe_candidates
    profile["blocked_candidate_count"] = blocked_candidates
    profile["allow_automatic"] = safe_candidates > 0
    if safe_candidates > 0:
        profile["reason"] = "deterministic font candidates available"
    elif blocked_candidates > 0:
        examples = profile.get("blocked_examples") or []
        suffix = f" ({', '.join(examples)})" if examples else ""
        profile["reason"] = (
            "unicode issues appear tied to simple fonts without explicit encoding"
            + suffix
        )
    else:
        profile["reason"] = "no deterministic ToUnicode candidates found"
    return profile


def _inspect_unicode_repair_gate(pdf_path: Path, violations=None) -> dict[str, object]:
    diagnostics = _inspect_font_diagnostics(
        pdf_path,
        violations,
        profile_limit=512,
        include_used_code_analysis=True,
    )
    return _unicode_repair_gate_from_diagnostics(diagnostics, violations=violations)


def _font_name_metadata(ttfont) -> dict[str, str]:
    meta = {
        "family": "",
        "subfamily": "",
        "full_name": "",
        "postscript": "",
        "combined": "",
    }
    name_table = ttfont.get("name")
    if name_table is None:
        return meta

    for record in name_table.names:
        if record.nameID not in (1, 2, 4, 6):
            continue
        try:
            text = str(record.toUnicode()).strip()
        except Exception:
            continue
        if not text:
            continue
        normalized = _normalize_font_name(text)
        if record.nameID == 1 and not meta["family"]:
            meta["family"] = normalized
        elif record.nameID == 2 and not meta["subfamily"]:
            meta["subfamily"] = normalized
        elif record.nameID == 4 and not meta["full_name"]:
            meta["full_name"] = normalized
        elif record.nameID == 6 and not meta["postscript"]:
            meta["postscript"] = normalized

    if meta["family"]:
        meta["combined"] = _normalize_font_name(f"{meta['family']}{meta['subfamily']}")
    return meta


def _font_name_keys(ttfont) -> set[str]:
    keys: set[str] = set()
    meta = _font_name_metadata(ttfont)
    for key in (meta["postscript"], meta["full_name"], meta["family"], meta["combined"]):
        if key:
            keys.add(key)
    return keys


@lru_cache(maxsize=1)
def _system_font_index() -> dict[str, list[tuple[str, int, str, str, str, str]]]:
    from fontTools.ttLib import TTCollection, TTFont

    index: dict[str, list[tuple[str, int, str, str, str, str]]] = {}
    for root in SYSTEM_FONT_DIRS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in SYSTEM_FONT_EXTENSIONS:
                continue
            try:
                if path.suffix.lower() == ".ttc":
                    collection = TTCollection(str(path), lazy=False)
                    fonts = list(enumerate(collection.fonts))
                else:
                    collection = None
                    fonts = [(0, TTFont(str(path), lazy=False))]
            except Exception:
                continue

            try:
                for face_index, ttfont in fonts:
                    try:
                        if "glyf" not in ttfont:
                            continue
                        meta = _font_name_metadata(ttfont)
                        candidate = (
                            str(path),
                            face_index,
                            meta["postscript"],
                            meta["full_name"],
                            meta["family"],
                            meta["subfamily"],
                        )
                        for key in _font_name_keys(ttfont):
                            index.setdefault(key, []).append(candidate)
                    finally:
                        try:
                            ttfont.close()
                        except Exception:
                            pass
            finally:
                if collection is not None:
                    try:
                        collection.close()
                    except Exception:
                        pass

    return index


@lru_cache(maxsize=256)
def _system_font_program(font_name: str) -> tuple[bytes | None, str | None]:
    from fontTools.ttLib import TTCollection, TTFont

    normalized = _normalize_font_name(font_name)
    if not normalized:
        return None, None

    def _score_candidate(
        requested: str,
        postscript: str,
        full_name: str,
        family: str,
        subfamily: str,
    ) -> int:
        if requested == postscript:
            return 500
        if requested == full_name:
            return 400
        combined = _normalize_font_name(f"{family}{subfamily}")
        if requested == combined:
            return 300
        if requested == family:
            if subfamily in {"", "regular", "roman", "book", "normal", "plain"}:
                return 200
            return 100
        return 0

    candidates = sorted(
        _system_font_index().get(normalized, []),
        key=lambda item: _score_candidate(normalized, item[2], item[3], item[4], item[5]),
        reverse=True,
    )
    for path_str, face_index, _, _, _, _ in candidates:
        path = Path(path_str)
        try:
            if path.suffix.lower() == ".ttc":
                collection = TTCollection(str(path), lazy=False)
                try:
                    ttfont = collection.fonts[face_index]
                    if "glyf" not in ttfont:
                        continue
                    buffer = BytesIO()
                    ttfont.save(buffer)
                    return buffer.getvalue(), path.name
                finally:
                    try:
                        collection.close()
                    except Exception:
                        pass

            ttfont = TTFont(str(path), lazy=False)
            try:
                if "glyf" not in ttfont:
                    continue
            finally:
                try:
                    ttfont.close()
                except Exception:
                    pass

            return path.read_bytes(), path.name
        except Exception:
            continue

    return None, None


@lru_cache(maxsize=1)
def _ghostscript_resource_font_dir() -> Path | None:
    gs = resolve_binary("gs", explicit=get_settings().ghostscript_path)
    if not gs:
        return None

    gs_path = Path(gs).resolve()
    candidate_suffixes = (
        Path("share/ghostscript/Resource/Font"),
        Path("Resource/Font"),
    )
    for parent in (gs_path.parent,) + tuple(gs_path.parents):
        for suffix in candidate_suffixes:
            candidate = parent / suffix
            if candidate.exists():
                return candidate
    return None


@lru_cache(maxsize=1)
def _ghostscript_font_metrics_dir() -> Path | None:
    gs = resolve_binary("gs", explicit=get_settings().ghostscript_path)
    if not gs:
        return None

    gs_path = Path(gs).resolve()
    candidate_suffixes = (
        Path("share/ghostscript/fonts"),
        Path("fonts"),
    )
    for parent in (gs_path.parent,) + tuple(gs_path.parents):
        for suffix in candidate_suffixes:
            candidate = parent / suffix
            if candidate.exists():
                return candidate
    return None


@lru_cache(maxsize=64)
def _ghostscript_type1_font_program(
    font_name: str,
) -> tuple[bytes | None, str | None, dict[str, int] | None]:
    from fontTools import t1Lib

    normalized = _normalize_font_name(font_name)
    if not normalized:
        return None, None, None

    resource_name = GHOSTSCRIPT_TYPE1_SUBSTITUTES.get(normalized)
    if not resource_name:
        return None, None, None

    resource_dir = _ghostscript_resource_font_dir()
    if resource_dir is None:
        return None, None, None

    path = resource_dir / resource_name
    if not path.exists():
        return None, None, None

    try:
        raw = path.read_bytes()
        chunks = t1Lib.findEncryptedChunks(raw)
    except Exception:
        return None, None, None

    font_bytes = b"".join(chunk for _, chunk in chunks)
    if not font_bytes:
        return None, None, None

    length1 = len(chunks[0][1]) if chunks else len(font_bytes)
    length2 = sum(len(chunk) for encrypted, chunk in chunks if encrypted)
    length3 = max(0, len(font_bytes) - length1 - length2)
    lengths = {
        "Length1": length1,
        "Length2": length2,
        "Length3": length3,
    }
    return font_bytes, path.name, lengths


@lru_cache(maxsize=64)
def _ghostscript_type1_descriptor(
    font_name: str,
) -> dict[str, object] | None:
    from fontTools import afmLib

    normalized = _normalize_font_name(font_name)
    if not normalized:
        return None

    spec = GHOSTSCRIPT_TYPE1_DESCRIPTOR_SPECS.get(normalized)
    metrics_dir = _ghostscript_font_metrics_dir()
    if spec is None or metrics_dir is None:
        return None

    path = metrics_dir / spec["afm"]
    if not path.exists():
        return None

    try:
        afm = afmLib.AFM(str(path))
    except Exception:
        return None

    attrs = afm._attrs
    font_bbox = attrs.get("FontBBox")
    if not isinstance(font_bbox, tuple) or len(font_bbox) != 4:
        return None

    widths_by_code = {
        _coerce_metric_int(charnum): _coerce_metric_int(width)
        for charnum, width, _ in afm._chars.values()
        if isinstance(_coerce_metric_int(charnum, -1), int) and 0 <= _coerce_metric_int(charnum, -1) <= 255
    }
    weight = str(attrs.get("Weight", "") or "").lower()
    stem_v = 120 if any(token in weight for token in ("bold", "demi", "black")) else 80
    descriptor = {
        "Flags": int(spec["flags"]),
        "ItalicAngle": _coerce_metric_int(attrs.get("ItalicAngle", 0), 0),
        "Ascent": _coerce_metric_int(attrs.get("Ascender", 0), 0),
        "Descent": _coerce_metric_int(attrs.get("Descender", 0), 0),
        "CapHeight": _coerce_metric_int(attrs.get("CapHeight", attrs.get("Ascender", 0)), 0),
        "StemV": stem_v,
        "FontBBox": [_coerce_metric_int(value, 0) for value in font_bbox],
        "FirstChar": 0,
        "LastChar": 255,
        "Widths": [_coerce_metric_int(widths_by_code.get(code, 0), 0) for code in range(256)],
        "MissingWidth": _coerce_metric_int(widths_by_code.get(0, widths_by_code.get(32, 0)), 0),
    }
    return descriptor


def _local_font_program(
    font_dict,
    font_name: str,
    *,
    descendant_subtype=None,
) -> tuple[bytes | None, str | None, str | None, dict[str, int] | None]:
    import pikepdf

    support_kind = _local_embed_support_kind(font_dict, descendant_subtype=descendant_subtype)
    if support_kind in {"truetype", "cidfonttype2"}:
        font_bytes, matched_name = _system_font_program(font_name)
        if not font_bytes:
            return None, None, None, None
        return font_bytes, matched_name, str(pikepdf.Name("/FontFile2")), {"Length1": len(font_bytes)}

    if support_kind == "type1_standard14":
        font_bytes, matched_name, lengths = _ghostscript_type1_font_program(font_name)
        if not font_bytes or not lengths:
            return None, None, None, None
        return font_bytes, matched_name, str(pikepdf.Name("/FontFile")), lengths

    return None, None, None, None


def _parse_cid_widths_array(widths_obj) -> dict[int, int]:
    import pikepdf

    widths: dict[int, int] = {}
    if not isinstance(widths_obj, pikepdf.Array):
        return widths

    idx = 0
    while idx < len(widths_obj):
        try:
            start_cid = int(widths_obj[idx])
        except Exception:
            break
        idx += 1
        if idx >= len(widths_obj):
            break

        entry = widths_obj[idx]
        idx += 1
        if isinstance(entry, pikepdf.Array):
            cid = start_cid
            for width_value in entry:
                try:
                    widths[cid] = int(round(float(width_value)))
                except Exception:
                    pass
                cid += 1
            continue

        try:
            end_cid = int(entry)
        except Exception:
            break
        if idx >= len(widths_obj):
            break
        try:
            width = int(round(float(widths_obj[idx])))
        except Exception:
            idx += 1
            continue
        idx += 1
        for cid in range(start_cid, end_cid + 1):
            widths[cid] = width

    return widths


def _render_cid_widths_array(widths: dict[int, int]):
    import pikepdf

    array = pikepdf.Array()
    if not widths:
        return array

    sorted_items = sorted(widths.items())
    run_start, first_width = sorted_items[0]
    run_widths = [first_width]

    for cid, width in sorted_items[1:]:
        if cid == run_start + len(run_widths):
            run_widths.append(width)
            continue
        array.append(run_start)
        array.append(pikepdf.Array(run_widths))
        run_start = cid
        run_widths = [width]

    array.append(run_start)
    array.append(pikepdf.Array(run_widths))
    return array


def _cid_cff_width_key(glyph_name: str | None, fallback_index: int) -> int:
    raw = str(glyph_name or "").strip()
    if raw == ".notdef":
        return 0
    if raw.startswith("cid") and raw[3:].isdigit():
        try:
            return int(raw[3:])
        except ValueError:
            pass
    return fallback_index


def _collect_cid_cff_widths(font_bytes: bytes) -> dict[int, int]:
    from fontTools.cffLib import CFFFontSet
    from fontTools.pens.basePen import NullPen

    widths: dict[int, int] = {}
    if not font_bytes:
        return widths

    font_set = CFFFontSet()
    font_set.decompile(BytesIO(font_bytes), None)
    for font_name in font_set.keys():
        cff_font = font_set[font_name]
        if not getattr(cff_font, "ROS", None):
            continue
        charset = getattr(cff_font, "charset", None)
        char_strings = getattr(cff_font, "CharStrings", None)
        if not isinstance(charset, list) or char_strings is None:
            continue
        for glyph_index, glyph_name in enumerate(charset):
            try:
                char_string = char_strings[glyph_name]
                char_string.draw(NullPen())
            except Exception:
                continue
            width = getattr(char_string, "width", None)
            if isinstance(width, (int, float)):
                cid = _cid_cff_width_key(glyph_name, glyph_index)
                widths[cid] = int(round(width))
    return widths


def _repair_pdf_font_dicts_sync(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    """Apply safe dictionary-level fixes for CID font compliance issues."""
    import pikepdf

    stats = {
        "fonts_touched": 0,
        "cidtogid_added": 0,
        "cidset_removed": 0,
    }
    seen_fonts: set[tuple[int, int]] = set()
    seen_resources: set[tuple[int, int]] = set()
    seen_appearances: set[tuple[int, int]] = set()

    def _has_embedded_font(descriptor) -> bool:
        if not isinstance(descriptor, pikepdf.Dictionary):
            return False
        return any(
            key in descriptor
            for key in (
                pikepdf.Name("/FontFile"),
                pikepdf.Name("/FontFile2"),
                pikepdf.Name("/FontFile3"),
            )
        )

    def _repair_cid_font_dict(cid_font) -> None:
        if not isinstance(cid_font, pikepdf.Dictionary):
            return
        obj_key = _obj_key(cid_font)
        if obj_key and obj_key in seen_fonts:
            return

        changed = False
        subtype = cid_font.get("/Subtype")
        if subtype not in (pikepdf.Name("/CIDFontType2"), pikepdf.Name("/CIDFontType0")):
            if obj_key:
                seen_fonts.add(obj_key)
            return

        descriptor = _resolve_dictionary(cid_font.get("/FontDescriptor"))
        if (
            subtype == pikepdf.Name("/CIDFontType2")
            and _has_embedded_font(descriptor)
            and pikepdf.Name("/CIDToGIDMap") not in cid_font
        ):
            cid_font[pikepdf.Name("/CIDToGIDMap")] = pikepdf.Name("/Identity")
            stats["cidtogid_added"] += 1
            changed = True

        if (
            isinstance(descriptor, pikepdf.Dictionary)
            and pikepdf.Name("/CIDSet") in descriptor
            and FONT_SUBSET_RE.match(str(cid_font.get("/BaseFont", "")).lstrip("/"))
        ):
            del descriptor[pikepdf.Name("/CIDSet")]
            stats["cidset_removed"] += 1
            changed = True

        if changed:
            stats["fonts_touched"] += 1
        if obj_key:
            seen_fonts.add(obj_key)

    def _walk_resources(resources) -> None:
        if not _is_pdf_mapping(resources):
            return

        resources_key = _obj_key(resources)
        if resources_key and resources_key in seen_resources:
            return
        if resources_key:
            seen_resources.add(resources_key)

        fonts = _resolve_dictionary(resources.get("/Font"))
        if isinstance(fonts, pikepdf.Dictionary):
            for _, font_obj in fonts.items():
                font_dict = _resolve_dictionary(font_obj)
                if not isinstance(font_dict, pikepdf.Dictionary):
                    continue

                subtype = font_dict.get("/Subtype")
                if subtype == pikepdf.Name("/Type0"):
                    descendants = font_dict.get("/DescendantFonts")
                    if not isinstance(descendants, pikepdf.Array):
                        continue
                    for descendant in descendants:
                        _repair_cid_font_dict(_resolve_dictionary(descendant))
                elif subtype == pikepdf.Name("/CIDFontType2"):
                    _repair_cid_font_dict(font_dict)

        xobjects = _resolve_dictionary(resources.get("/XObject"))
        if not isinstance(xobjects, pikepdf.Dictionary):
            return
        for _, xobject in xobjects.items():
            xobject_dict = _resolve_dictionary(xobject)
            if not _is_pdf_mapping(xobject_dict):
                continue
            try:
                subtype = xobject_dict.get("/Subtype")
            except Exception:
                continue
            if subtype != pikepdf.Name("/Form"):
                continue
            child_resources = _resolve_dictionary(xobject_dict.get("/Resources"))
            _walk_resources(child_resources)

    def _walk_appearance_object(obj) -> None:
        appearance_obj = _resolve_dictionary(obj)
        if not _is_pdf_mapping(appearance_obj):
            return
        appearance_key = _obj_key(appearance_obj)
        if appearance_key and appearance_key in seen_appearances:
            return
        if appearance_key:
            seen_appearances.add(appearance_key)

        _walk_resources(_resolve_dictionary(appearance_obj.get("/Resources")))
        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                _walk_appearance_object(child)

    try:
        with pikepdf.open(str(input_path)) as pdf:
            _walk_pdf_resource_graph(
                pdf,
                resolve_dictionary=_resolve_dictionary,
                walk_resources=_walk_resources,
                walk_appearance_object=_walk_appearance_object,
            )

            if stats["fonts_touched"] <= 0:
                return False, "No eligible CID font dictionaries found for repair", stats

            pdf.save(str(output_path))
        return (
            True,
            (
                f"Font dictionary repair completed "
                f"(fonts={stats['fonts_touched']}, "
                f"CIDToGIDMap added={stats['cidtogid_added']}, "
                f"CIDSet removed={stats['cidset_removed']})"
            ),
            stats,
        )
    except Exception as exc:
        return False, f"Font dictionary repair failed: {exc}", stats


async def _repair_pdf_font_dicts(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    return await asyncio.to_thread(_repair_pdf_font_dicts_sync, input_path, output_path)


def _sync_pdf_cid_cff_widths_sync(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    """Sync CIDFontType0 width arrays to embedded CFF program widths."""
    import pikepdf

    stats = {
        "fonts_touched": 0,
        "widths_synced": 0,
    }
    seen_fonts: set[tuple[int, int]] = set()
    seen_resources: set[tuple[int, int]] = set()
    seen_appearances: set[tuple[int, int]] = set()

    def _repair_cid_font_widths(cid_font) -> None:
        if not isinstance(cid_font, pikepdf.Dictionary):
            return
        obj_key = _obj_key(cid_font)
        if obj_key and obj_key in seen_fonts:
            return

        try:
            if cid_font.get("/Subtype") != pikepdf.Name("/CIDFontType0"):
                return
            widths_obj = cid_font.get("/W")
            current_widths = _parse_cid_widths_array(widths_obj)
            if not current_widths:
                return

            descriptor = _resolve_dictionary(cid_font.get("/FontDescriptor"))
            if not isinstance(descriptor, pikepdf.Dictionary):
                return
            font_stream = _resolve_dictionary(descriptor.get("/FontFile3"))
            if font_stream is None:
                return
            if font_stream.get("/Subtype") != pikepdf.Name("/CIDFontType0C"):
                return
            try:
                font_bytes = bytes(font_stream.read_bytes())
            except Exception:
                return
            program_widths = _collect_cid_cff_widths(font_bytes)
            if not program_widths:
                return

            updated_widths = dict(current_widths)
            font_changed = False
            for cid, current_width in current_widths.items():
                program_width = program_widths.get(cid)
                if program_width is None or program_width == current_width:
                    continue
                updated_widths[cid] = program_width
                stats["widths_synced"] += 1
                font_changed = True

            if font_changed:
                cid_font[pikepdf.Name("/W")] = _render_cid_widths_array(updated_widths)
                stats["fonts_touched"] += 1
        finally:
            if obj_key:
                seen_fonts.add(obj_key)

    def _walk_resources(resources) -> None:
        if not _is_pdf_mapping(resources):
            return

        resources_key = _obj_key(resources)
        if resources_key and resources_key in seen_resources:
            return
        if resources_key:
            seen_resources.add(resources_key)

        fonts = _resolve_dictionary(resources.get("/Font"))
        if isinstance(fonts, pikepdf.Dictionary):
            for _, font_obj in fonts.items():
                font_dict = _resolve_dictionary(font_obj)
                if not isinstance(font_dict, pikepdf.Dictionary):
                    continue

                subtype = font_dict.get("/Subtype")
                if subtype == pikepdf.Name("/Type0"):
                    descendants = font_dict.get("/DescendantFonts")
                    if not isinstance(descendants, pikepdf.Array):
                        continue
                    for descendant in descendants:
                        _repair_cid_font_widths(_resolve_dictionary(descendant))
                elif subtype == pikepdf.Name("/CIDFontType0"):
                    _repair_cid_font_widths(font_dict)

        xobjects = _resolve_dictionary(resources.get("/XObject"))
        if not isinstance(xobjects, pikepdf.Dictionary):
            return
        for _, xobject in xobjects.items():
            xobject_dict = _resolve_dictionary(xobject)
            if not _is_pdf_mapping(xobject_dict):
                continue
            try:
                subtype = xobject_dict.get("/Subtype")
            except Exception:
                continue
            if subtype != pikepdf.Name("/Form"):
                continue
            child_resources = _resolve_dictionary(xobject_dict.get("/Resources"))
            _walk_resources(child_resources)

    def _walk_appearance_object(obj) -> None:
        appearance_obj = _resolve_dictionary(obj)
        if not _is_pdf_mapping(appearance_obj):
            return
        appearance_key = _obj_key(appearance_obj)
        if appearance_key and appearance_key in seen_appearances:
            return
        if appearance_key:
            seen_appearances.add(appearance_key)

        _walk_resources(_resolve_dictionary(appearance_obj.get("/Resources")))
        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                _walk_appearance_object(child)

    try:
        with pikepdf.open(str(input_path)) as pdf:
            _walk_pdf_resource_graph(
                pdf,
                resolve_dictionary=_resolve_dictionary,
                walk_resources=_walk_resources,
                walk_appearance_object=_walk_appearance_object,
            )
            if stats["widths_synced"] <= 0:
                return False, "No eligible CID CFF width mismatches found", stats
            pdf.save(str(output_path))
        return (
            True,
            (
                f"CID CFF width sync completed "
                f"(fonts={stats['fonts_touched']}, widths synced={stats['widths_synced']})"
            ),
            stats,
        )
    except Exception as exc:
        return False, f"CID CFF width sync failed: {exc}", stats


async def _sync_pdf_cid_cff_widths(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    return await asyncio.to_thread(_sync_pdf_cid_cff_widths_sync, input_path, output_path)


def _is_valid_unicode_text(text: str) -> bool:
    if not text:
        return False
    for ch in text:
        cp = ord(ch)
        if cp in (0x0000, 0xFFFE, 0xFEFF):
            return False
        if cp > 0x10FFFF or (0xD800 <= cp <= 0xDFFF):
            return False
    return True


def _decode_tounicode_hex(value: str) -> str | None:
    try:
        data = bytes.fromhex(value)
    except ValueError:
        return None
    if not data:
        return None
    if len(data) % 2 != 0:
        return None
    try:
        text = data.decode("utf-16-be")
    except UnicodeDecodeError:
        return None
    return text if _is_valid_unicode_text(text) else None


def _glyph_name_to_unicode(glyph_name: str | None) -> str | None:
    from fontTools.agl import toUnicode

    raw = str(glyph_name or "").strip().lstrip("/")
    if not raw or raw == ".notdef":
        return None

    text = toUnicode(raw)
    if not text and "." in raw:
        text = toUnicode(raw.split(".", 1)[0])
    return text if _is_valid_unicode_text(text) else None


def _named_simple_encoding_map(encoding_name) -> dict[int, str]:
    from fontTools.encodings import MacRoman, StandardEncoding

    name = str(encoding_name or "").strip()
    if not name:
        return {}

    mapping: dict[int, str] = {}
    if name == "/StandardEncoding":
        glyph_names = StandardEncoding.StandardEncoding
        for code, glyph_name in enumerate(glyph_names):
            text = _glyph_name_to_unicode(glyph_name)
            if text:
                mapping[code] = text
        return mapping

    if name == "/MacRomanEncoding":
        glyph_names = MacRoman.MacRoman
        for code, glyph_name in enumerate(glyph_names):
            text = _glyph_name_to_unicode(glyph_name)
            if text:
                mapping[code] = text
        return mapping

    if name == "/WinAnsiEncoding":
        for code in range(256):
            try:
                text = bytes([code]).decode("cp1252")
            except Exception:
                continue
            if _is_valid_unicode_text(text):
                mapping[code] = text
        return mapping

    return {}


def _encoding_differences_map(differences) -> dict[int, str]:
    import pikepdf

    if not isinstance(differences, pikepdf.Array):
        return {}

    mapping: dict[int, str] = {}
    current_code: int | None = None
    for item in differences:
        if isinstance(item, int):
            current_code = item
            continue
        if current_code is None:
            continue
        text = _glyph_name_to_unicode(str(item))
        if text:
            mapping[current_code] = text
        current_code += 1
    return mapping


def _cff_builtin_encoding_map(font_bytes: bytes) -> dict[int, str]:
    from fontTools.cffLib import CFFFontSet

    try:
        cff = CFFFontSet()
        cff.decompile(BytesIO(font_bytes), None)
        top = cff[list(cff.keys())[0]]
    except Exception:
        return {}

    encoding = getattr(top, "Encoding", None)
    if not isinstance(encoding, list):
        return {}

    mapping: dict[int, str] = {}
    for code, glyph_name in enumerate(encoding):
        text = _glyph_name_to_unicode(glyph_name)
        if text:
            mapping[code] = text
    return mapping


def _simple_font_unicode_map(font_dict, font_bytes: bytes | None) -> dict[int, str]:
    import pikepdf

    mapping: dict[int, str] = {}

    subtype = font_dict.get("/Subtype")
    encoding = font_dict.get("/Encoding")
    if isinstance(encoding, pikepdf.Dictionary):
        mapping.update(_named_simple_encoding_map(encoding.get("/BaseEncoding")))
        mapping.update(_encoding_differences_map(encoding.get("/Differences")))
    else:
        mapping.update(_named_simple_encoding_map(encoding))

    # Stay conservative in the automatic lane: only use explicit PDF encoding data,
    # or the built-in Latin standard 14 fonts where StandardEncoding is deterministic.
    policy = _simple_font_auto_unicode_policy(font_dict, font_bytes=font_bytes)
    if not mapping and subtype in (pikepdf.Name("/Type1"), pikepdf.Name("/MMType1")):
        if policy == "standard14":
            mapping.update(_named_simple_encoding_map("/StandardEncoding"))
        elif policy == "embedded_cff" and font_bytes:
            mapping.update(_cff_builtin_encoding_map(font_bytes))

    return {code: text for code, text in mapping.items() if 0 <= code <= 0xFF and _is_valid_unicode_text(text)}


def _raw_text_bytes(op: str, operands) -> bytes:
    if op == "Tj" and operands:
        try:
            return bytes(operands[0])
        except Exception:
            return b""
    if op == "TJ" and operands:
        parts: list[bytes] = []
        arr = operands[0]
        if arr is not None:
            try:
                for item in arr:
                    try:
                        parts.append(bytes(item))
                    except Exception:
                        continue
            except Exception:
                pass
        return b"".join(parts)
    if op == "'" and operands:
        try:
            return bytes(operands[0])
        except Exception:
            return b""
    if op == '"' and len(operands) >= 3:
        try:
            return bytes(operands[2])
        except Exception:
            return b""
    return b""


def _simple_font_zero_byte_repair_candidate(
    font_dict,
    *,
    used_codes: set[int],
    existing_map: dict[int, str],
    generated_map: dict[int, str],
) -> bool:
    if 0 not in used_codes:
        return False

    unresolved_used_codes = {
        code
        for code in used_codes
        if code not in existing_map or not _is_valid_unicode_text(existing_map[code])
    } - set(generated_map)
    return unresolved_used_codes == {0}


def _sanitize_text_showing_zero_bytes(op: str, operands) -> tuple[list[object], int]:
    import pikepdf

    removed = 0
    new_operands = list(operands)

    def _strip_zero_bytes(value):
        nonlocal removed
        try:
            raw = bytes(value)
        except Exception:
            return value
        removed_here = raw.count(0)
        if removed_here <= 0:
            return value
        removed += removed_here
        return pikepdf.String(raw.replace(b"\x00", b""))

    if op == "Tj" and new_operands:
        new_operands[0] = _strip_zero_bytes(new_operands[0])
        return new_operands, removed

    if op == "TJ" and new_operands:
        arr = new_operands[0]
        if isinstance(arr, pikepdf.Array):
            new_arr = pikepdf.Array()
            for item in arr:
                new_arr.append(_strip_zero_bytes(item))
            new_operands[0] = new_arr
        return new_operands, removed

    if op == "'" and new_operands:
        new_operands[0] = _strip_zero_bytes(new_operands[0])
        return new_operands, removed

    if op == '"' and len(new_operands) >= 3:
        new_operands[2] = _strip_zero_bytes(new_operands[2])
        return new_operands, removed

    return new_operands, 0


def _parse_tounicode_map(stream_obj) -> dict[int, str]:
    mapping, _ = _parse_tounicode_map_details(stream_obj)
    return mapping


def _parse_tounicode_map_details(stream_obj) -> tuple[dict[int, str], int]:
    if stream_obj is None:
        return {}, 0
    try:
        raw = bytes(stream_obj.read_bytes())
    except Exception:
        return {}, 0

    try:
        text = raw.decode("latin-1")
    except Exception:
        return {}, 0

    mapping: dict[int, str] = {}
    invalid_entries = 0
    mode = ""

    def _bounded_span(start: int, end: int) -> int | None:
        if end < start:
            return None
        span = end - start + 1
        if span > MAX_TOUNICODE_BFRANGE_SPAN:
            return None
        return span

    for raw_line in text.splitlines():
        line = raw_line.split("%", 1)[0].strip()
        if not line:
            continue
        lower = line.lower()
        if lower.endswith("beginbfchar"):
            mode = "bfchar"
            continue
        if lower.endswith("beginbfrange"):
            mode = "bfrange"
            continue
        if lower.endswith("endbfchar") or lower.endswith("endbfrange"):
            mode = ""
            continue
        if not mode:
            continue

        hex_groups = HEX_STR_RE.findall(line)
        if mode == "bfchar":
            if len(hex_groups) < 2:
                continue
            try:
                code = int(hex_groups[0], 16)
            except ValueError:
                continue
            decoded = _decode_tounicode_hex(hex_groups[1])
            if decoded:
                mapping[code] = decoded
            else:
                invalid_entries += 1
            continue

        # bfrange
        if "[" in line and "]" in line:
            if len(hex_groups) < 3:
                continue
            try:
                start = int(hex_groups[0], 16)
                end = int(hex_groups[1], 16)
            except ValueError:
                continue
            targets = hex_groups[2:]
            span = _bounded_span(start, end)
            if span is None:
                invalid_entries += 1
                continue
            for offset, target in enumerate(targets[:span]):
                decoded = _decode_tounicode_hex(target)
                if decoded:
                    mapping[start + offset] = decoded
                else:
                    invalid_entries += 1
            continue

        if len(hex_groups) < 3:
            continue
        try:
            start = int(hex_groups[0], 16)
            end = int(hex_groups[1], 16)
            seed_raw = bytes.fromhex(hex_groups[2])
            if len(seed_raw) % 2 != 0:
                continue
            seed_text = seed_raw.decode("utf-16-be")
        except Exception:
            continue

        span = _bounded_span(start, end)
        if span is None:
            invalid_entries += 1
            continue
        if len(seed_text) != 1:
            invalid_entries += 1
            continue
        seed_cp = ord(seed_text)
        for offset in range(span):
            cp = seed_cp + offset
            if cp > 0x10FFFF or cp in (0x0000, 0xFFFE, 0xFEFF) or (0xD800 <= cp <= 0xDFFF):
                invalid_entries += 1
                continue
            mapping[start + offset] = chr(cp)

    return mapping, invalid_entries


def _inspect_font_diagnostics(
    pdf_path: Path,
    violations=None,
    *,
    profile_limit: int = 24,
    include_used_code_analysis: bool = True,
) -> dict[str, object]:
    """Collect per-font diagnostics for remediation planning and reporting."""
    import pikepdf

    diagnostics: dict[str, object] = {
        "summary": {
            "fonts_total": 0,
            "simple_fonts": 0,
            "type0_fonts": 0,
            "embedded_fonts": 0,
            "unembedded_fonts": 0,
            "fonts_with_tounicode": 0,
            "fonts_missing_tounicode": 0,
            "fonts_with_invalid_tounicode": 0,
            "fonts_with_missing_used_codes": 0,
            "fonts_with_unresolved_used_codes": 0,
            "blocked_auto_unicode_fonts": 0,
            "local_embed_candidate_count": 0,
            "local_embed_missing_program_count": 0,
            "unsupported_unembedded_fonts": 0,
            "invalid_unicode_rule_present": False,
        },
        "profiles": [],
        "error": None,
    }
    diagnostics["summary"]["invalid_unicode_rule_present"] = any(
        getattr(v, "severity", "") == "error" and "7.21.7-2" in str(getattr(v, "rule_id", ""))
        for v in (violations or [])
    )
    simple_unicode_rule_present = any(
        getattr(v, "severity", "") == "error"
        and any(marker in str(getattr(v, "rule_id", "")) for marker in FONT_UNICODE_RULE_MARKERS)
        for v in (violations or [])
    )

    try:
        seen_resources: set[tuple[int, int]] = set()
        seen_fonts: set[tuple[int, int]] = set()
        seen_appearances: set[tuple[int, int]] = set()
        seen_content_streams: set[tuple[int, int]] = set()
        used_simple_codes: dict[tuple[int, int], set[int]] = {}
        profiles_by_key: dict[str, dict[str, object]] = {}
        simple_font_candidates: dict[tuple[int, int], dict[str, object]] = {}

        def _font_stream_bytes(descriptor) -> bytes | None:
            if not isinstance(descriptor, pikepdf.Dictionary):
                return None
            for key in ("/FontFile", "/FontFile2", "/FontFile3"):
                stream_obj = _resolve_dictionary(descriptor.get(key))
                if stream_obj is None:
                    continue
                try:
                    return bytes(stream_obj.read_bytes())
                except Exception:
                    continue
            return None

        def _has_embedded_font(descriptor) -> bool:
            if not isinstance(descriptor, pikepdf.Dictionary):
                return False
            return any(
                key in descriptor
                for key in (
                    pikepdf.Name("/FontFile"),
                    pikepdf.Name("/FontFile2"),
                    pikepdf.Name("/FontFile3"),
                )
            )

        def _collect_used_simple_font_codes(content_obj, resources, *, target_font_keys: set[tuple[int, int]]) -> None:
            if not target_font_keys:
                return
            if resources is None:
                return
            resolved_obj = _resolve_dictionary(content_obj)
            if resolved_obj is None:
                return
            content_key = _obj_key(resolved_obj)
            if content_key and content_key in seen_content_streams:
                return
            if content_key:
                seen_content_streams.add(content_key)

            fonts = _resolve_dictionary(resources.get("/Font"))
            xobjects = _resolve_dictionary(resources.get("/XObject"))
            target_font_in_scope = False
            if isinstance(fonts, pikepdf.Dictionary):
                for font_obj in fonts.values():
                    font_dict = _resolve_dictionary(font_obj)
                    font_key = _obj_key(font_dict) if isinstance(font_dict, pikepdf.Dictionary) else None
                    if font_key and font_key in target_font_keys:
                        target_font_in_scope = True
                        break
            has_form_xobjects = isinstance(xobjects, pikepdf.Dictionary) and bool(xobjects)
            if not target_font_in_scope and not has_form_xobjects:
                return

            current_font = None
            try:
                instructions = pikepdf.parse_content_stream(resolved_obj)
            except Exception:
                return

            for instr in instructions:
                op = str(instr.operator)
                operands = list(instr.operands) if hasattr(instr, "operands") else []
                if op == "Tf" and operands and isinstance(fonts, pikepdf.Dictionary):
                    current_font = _resolve_dictionary(fonts.get(operands[0]))
                    continue

                if op in ("Tj", "TJ", "'", '"') and isinstance(current_font, pikepdf.Dictionary):
                    if current_font.get("/Subtype") not in (
                        pikepdf.Name("/Type1"),
                        pikepdf.Name("/MMType1"),
                        pikepdf.Name("/TrueType"),
                    ):
                        continue
                    font_key = _obj_key(current_font)
                    if not font_key or font_key not in target_font_keys:
                        continue
                    raw = _raw_text_bytes(op, operands)
                    if not raw:
                        continue
                    used_simple_codes.setdefault(font_key, set()).update(raw)
                    continue

                if op == "Do" and operands and isinstance(xobjects, pikepdf.Dictionary):
                    xobject = _resolve_dictionary(xobjects.get(operands[0]))
                    if not isinstance(xobject, pikepdf.Dictionary):
                        continue
                    if xobject.get("/Subtype") != pikepdf.Name("/Form"):
                        continue
                    child_resources = _resolve_dictionary(xobject.get("/Resources")) or resources
                    _collect_used_simple_font_codes(xobject, child_resources)

        def _touch_profile(font_dict, *, descendant_subtype=None, descriptor=None) -> None:
            font_key = _obj_key(font_dict)
            profile_key = f"{font_key[0]} {font_key[1]}" if font_key else f"inline:{id(font_dict)}"
            profile = profiles_by_key.get(profile_key)
            if profile is None:
                base_font = _base_font_name(font_dict)
                subtype = str(font_dict.get("/Subtype") or "")
                has_tounicode = pikepdf.Name("/ToUnicode") in font_dict
                encoding = font_dict.get("/Encoding")
                encoding_name = str(encoding) if encoding is not None and not isinstance(encoding, pikepdf.Dictionary) else (
                    "dict"
                    if isinstance(encoding, pikepdf.Dictionary)
                    else ""
                )
                font_bytes = _font_stream_bytes(descriptor)
                auto_unicode_policy = ""
                if subtype in ("/Type1", "/MMType1", "/TrueType"):
                    auto_unicode_policy = _simple_font_auto_unicode_policy(font_dict, font_bytes=font_bytes)
                embedded = _has_embedded_font(descriptor)
                local_embed_support = _local_embed_support_kind(font_dict, descendant_subtype=descendant_subtype)
                local_font_program_available = False
                if not embedded and local_embed_support and base_font:
                    local_font_program_available = bool(
                        _local_font_program(
                            font_dict,
                            base_font,
                            descendant_subtype=descendant_subtype,
                        )[0]
                    )

                profile = {
                    "object_id": profile_key,
                    "base_font": base_font or "(unnamed)",
                    "subtype": subtype,
                    "descendant_subtype": str(descendant_subtype or "") if descendant_subtype else "",
                    "embedded": embedded,
                    "has_tounicode": has_tounicode,
                    "encoding": encoding_name,
                    "auto_unicode_policy": auto_unicode_policy,
                    "invalid_tounicode_entries": 0,
                    "existing_tounicode_entries": 0,
                    "used_code_count": 0,
                    "missing_used_code_count": 0,
                    "repairable_missing_used_codes": 0,
                    "unresolved_used_code_count": 0,
                    "local_embed_support": local_embed_support,
                    "local_font_program_available": local_font_program_available,
                    "issue_tags": [],
                }
                profiles_by_key[profile_key] = profile

            existing_stream = _resolve_dictionary(font_dict.get("/ToUnicode"))
            existing_map, invalid_entries = _parse_tounicode_map_details(existing_stream)
            profile["invalid_tounicode_entries"] = invalid_entries
            profile["existing_tounicode_entries"] = len(existing_map)
            if font_key and profile["subtype"] in ("/Type1", "/MMType1", "/TrueType"):
                simple_font_candidates[font_key] = {
                    "profile": profile,
                    "font_dict": font_dict,
                    "font_bytes": _font_stream_bytes(descriptor),
                    "existing_map": existing_map,
                }

        def _walk_resources(resources) -> None:
            if not _is_pdf_mapping(resources):
                return
            resources_key = _obj_key(resources)
            if resources_key and resources_key in seen_resources:
                return
            if resources_key:
                seen_resources.add(resources_key)

            fonts = _resolve_dictionary(resources.get("/Font"))
            if isinstance(fonts, pikepdf.Dictionary):
                for _, font_obj in fonts.items():
                    font_dict = _resolve_dictionary(font_obj)
                    if not isinstance(font_dict, pikepdf.Dictionary):
                        continue

                    font_key = _obj_key(font_dict)
                    if font_key and font_key in seen_fonts:
                        continue
                    if font_key:
                        seen_fonts.add(font_key)

                    subtype = font_dict.get("/Subtype")
                    if subtype == pikepdf.Name("/Type0"):
                        descendants = font_dict.get("/DescendantFonts")
                        cid_font = None
                        if isinstance(descendants, pikepdf.Array) and descendants:
                            cid_font = _resolve_dictionary(descendants[0])
                        descriptor = _resolve_dictionary(cid_font.get("/FontDescriptor")) if isinstance(cid_font, pikepdf.Dictionary) else None
                        descendant_subtype = cid_font.get("/Subtype") if isinstance(cid_font, pikepdf.Dictionary) else None
                        _touch_profile(
                            font_dict,
                            descendant_subtype=descendant_subtype,
                            descriptor=descriptor,
                        )
                    else:
                        descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
                        _touch_profile(font_dict, descriptor=descriptor)

            xobjects = _resolve_dictionary(resources.get("/XObject"))
            if not isinstance(xobjects, pikepdf.Dictionary):
                return
            for _, xobject in xobjects.items():
                xobject_dict = _resolve_dictionary(xobject)
                if not _is_pdf_mapping(xobject_dict):
                    continue
                if xobject_dict.get("/Subtype") != pikepdf.Name("/Form"):
                    continue
                _walk_resources(_resolve_dictionary(xobject_dict.get("/Resources")))

        def _walk_appearance_object(obj) -> None:
            appearance_obj = _resolve_dictionary(obj)
            if not _is_pdf_mapping(appearance_obj):
                return
            appearance_key = _obj_key(appearance_obj)
            if appearance_key and appearance_key in seen_appearances:
                return
            if appearance_key:
                seen_appearances.add(appearance_key)

            _walk_resources(_resolve_dictionary(appearance_obj.get("/Resources")))
            for key in ("/N", "/R", "/D"):
                child = appearance_obj.get(key)
                if child is not None:
                    _walk_appearance_object(child)

        with pikepdf.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                page_resources = _resolve_dictionary(page.get("/Resources"))
                _walk_resources(page_resources)

                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for annot in annots:
                        _walk_appearance_object(annot.get("/AP"))

            acroform = _resolve_dictionary(pdf.Root.get("/AcroForm"))
            if _is_pdf_mapping(acroform):
                _walk_resources(_resolve_dictionary(acroform.get("/DR")))

                seen_fields: set[tuple[int, int]] = set()

                def _walk_field(field_obj) -> None:
                    field = _resolve_dictionary(field_obj)
                    if not _is_pdf_mapping(field):
                        return
                    field_key = _obj_key(field)
                    if field_key and field_key in seen_fields:
                        return
                    if field_key:
                        seen_fields.add(field_key)

                    field_resources = _resolve_dictionary(field.get("/DR"))
                    _walk_resources(field_resources)
                    _walk_appearance_object(field.get("/AP"))

                    kids = field.get("/Kids")
                    if isinstance(kids, pikepdf.Array):
                        for kid in kids:
                            _walk_field(kid)

                fields = acroform.get("/Fields")
                if isinstance(fields, pikepdf.Array):
                    for field in fields:
                        _walk_field(field)

            if include_used_code_analysis and simple_font_candidates:
                target_simple_font_keys = {
                    font_key
                    for font_key, entry in simple_font_candidates.items()
                    if (
                        not bool(entry["profile"]["has_tounicode"])
                        or int(entry["profile"]["invalid_tounicode_entries"]) > 0
                        or simple_unicode_rule_present
                    )
                }
                if target_simple_font_keys:
                    seen_content_streams.clear()
                    for page in pdf.pages:
                        page_resources = _resolve_dictionary(page.get("/Resources"))
                        _collect_used_simple_font_codes(
                            page,
                            page_resources,
                            target_font_keys=target_simple_font_keys,
                        )

                        annots = page.get("/Annots")
                        if isinstance(annots, pikepdf.Array):
                            for annot in annots:
                                _collect_used_simple_font_codes(
                                    annot.get("/AP"),
                                    _resolve_dictionary(annot.get("/Resources")),
                                    target_font_keys=target_simple_font_keys,
                                )

                    if isinstance(acroform, pikepdf.Dictionary):
                        seen_fields_for_codes: set[tuple[int, int]] = set()

                        def _walk_field_codes(field_obj) -> None:
                            field = _resolve_dictionary(field_obj)
                            if not isinstance(field, pikepdf.Dictionary):
                                return
                            field_key = _obj_key(field)
                            if field_key and field_key in seen_fields_for_codes:
                                return
                            if field_key:
                                seen_fields_for_codes.add(field_key)

                            field_resources = _resolve_dictionary(field.get("/DR"))
                            _collect_used_simple_font_codes(
                                field.get("/AP"),
                                field_resources,
                                target_font_keys=target_simple_font_keys,
                            )

                            kids = field.get("/Kids")
                            if isinstance(kids, pikepdf.Array):
                                for kid in kids:
                                    _walk_field_codes(kid)

                        fields = acroform.get("/Fields")
                        if isinstance(fields, pikepdf.Array):
                            for field in fields:
                                _walk_field_codes(field)

                    for font_key, entry in simple_font_candidates.items():
                        profile = entry["profile"]
                        used_codes = used_simple_codes.get(font_key, set())
                        profile["used_code_count"] = len(used_codes)
                        if not used_codes:
                            continue
                        existing_map = entry["existing_map"]
                        current_missing_codes = {
                            code
                            for code in used_codes
                            if code not in existing_map or not _is_valid_unicode_text(existing_map[code])
                        }
                        generated_map = {}
                        if str(profile["auto_unicode_policy"]) != "blocked":
                            generated_map = _simple_font_unicode_map(
                                entry["font_dict"],
                                entry["font_bytes"],
                            )
                        repairable_missing = current_missing_codes & set(generated_map)
                        unresolved_missing = current_missing_codes - set(generated_map)
                        profile["missing_used_code_count"] = len(current_missing_codes)
                        profile["repairable_missing_used_codes"] = len(repairable_missing)
                        profile["unresolved_used_code_count"] = len(unresolved_missing)

        profiles = list(profiles_by_key.values())
        for profile in profiles:
            issues: list[str] = []
            if not bool(profile["embedded"]):
                issues.append("unembedded")
                if profile["local_embed_support"] and not profile["local_font_program_available"]:
                    issues.append("local_font_program_missing")
                elif not profile["local_embed_support"]:
                    issues.append("local_embed_unsupported")
            if not bool(profile["has_tounicode"]):
                issues.append("missing_tounicode")
            if int(profile["invalid_tounicode_entries"]) > 0:
                issues.append("invalid_tounicode")
            if int(profile["missing_used_code_count"]) > 0:
                issues.append("missing_used_codes")
            if int(profile["unresolved_used_code_count"]) > 0:
                issues.append("unresolved_used_codes")
            if profile["auto_unicode_policy"] == "blocked":
                issues.append("blocked_auto_unicode")
            profile["issue_tags"] = issues

        profiles.sort(
            key=lambda profile: (
                -len(profile["issue_tags"]),
                -int(profile["unresolved_used_code_count"]),
                -int(profile["invalid_tounicode_entries"]),
                -int(profile["missing_used_code_count"]),
                str(profile["base_font"]).lower(),
            )
        )

        summary = diagnostics["summary"]
        summary["fonts_total"] = len(profiles)
        summary["simple_fonts"] = sum(
            1
            for profile in profiles
            if str(profile["subtype"]) in {"/Type1", "/MMType1", "/TrueType"}
        )
        summary["type0_fonts"] = sum(1 for profile in profiles if str(profile["subtype"]) == "/Type0")
        summary["embedded_fonts"] = sum(1 for profile in profiles if bool(profile["embedded"]))
        summary["unembedded_fonts"] = sum(1 for profile in profiles if not bool(profile["embedded"]))
        summary["fonts_with_tounicode"] = sum(1 for profile in profiles if bool(profile["has_tounicode"]))
        summary["fonts_missing_tounicode"] = sum(1 for profile in profiles if not bool(profile["has_tounicode"]))
        summary["fonts_with_invalid_tounicode"] = sum(
            1 for profile in profiles if int(profile["invalid_tounicode_entries"]) > 0
        )
        summary["fonts_with_missing_used_codes"] = sum(
            1 for profile in profiles if int(profile["missing_used_code_count"]) > 0
        )
        summary["fonts_with_unresolved_used_codes"] = sum(
            1 for profile in profiles if int(profile["unresolved_used_code_count"]) > 0
        )
        summary["blocked_auto_unicode_fonts"] = sum(
            1 for profile in profiles if profile["auto_unicode_policy"] == "blocked"
        )
        summary["local_embed_candidate_count"] = sum(
            1
            for profile in profiles
            if (
                not bool(profile["embedded"])
                and bool(profile["local_embed_support"])
                and bool(profile["local_font_program_available"])
            )
        )
        summary["local_embed_missing_program_count"] = sum(
            1
            for profile in profiles
            if (
                not bool(profile["embedded"])
                and bool(profile["local_embed_support"])
                and not bool(profile["local_font_program_available"])
            )
        )
        summary["unsupported_unembedded_fonts"] = sum(
            1
            for profile in profiles
            if not bool(profile["embedded"]) and not bool(profile["local_embed_support"])
        )
        diagnostics["profiles"] = profiles[:max(0, profile_limit)]
    except Exception as exc:
        diagnostics["error"] = str(exc)

    return diagnostics


def _render_tounicode_cmap(mapping: dict[int, str], code_bytes: int) -> bytes:
    if code_bytes not in (1, 2, 4):
        code_bytes = 2
    width = code_bytes * 2
    max_code = (1 << (code_bytes * 8)) - 1
    entries: list[tuple[int, str]] = []
    for code, value in sorted(mapping.items()):
        if code < 0 or code > max_code or not _is_valid_unicode_text(value):
            continue
        entries.append((code, value))

    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        f"<{'0' * width}> <{max_code:0{width}X}>",
        "endcodespacerange",
    ]

    for i in range(0, len(entries), 100):
        chunk = entries[i:i + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for code, value in chunk:
            dest_hex = value.encode("utf-16-be").hex().upper()
            lines.append(f"<{code:0{width}X}> <{dest_hex}>")
        lines.append("endbfchar")

    lines.extend([
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
        "",
    ])
    return "\n".join(lines).encode("ascii")


def _merge_tounicode_maps(existing: dict[int, str], generated: dict[int, str]) -> tuple[dict[int, str], int]:
    merged = {code: text for code, text in existing.items() if _is_valid_unicode_text(text)}
    overwritten = 0
    for code, text in generated.items():
        if not _is_valid_unicode_text(text):
            continue
        previous = merged.get(code)
        if previous and previous != text:
            overwritten += 1
        merged[code] = text
    return merged, overwritten


def _collect_gid_to_unicode(font_bytes: bytes) -> dict[int, str]:
    from fontTools.ttLib import TTFont

    gid_to_unicode: dict[int, str] = {}
    tt = TTFont(BytesIO(font_bytes), lazy=True)
    try:
        name_to_unicode: dict[str, str] = {}
        cmap_table = tt.get("cmap")
        if cmap_table is not None:
            for cmap in cmap_table.tables:
                table_map = getattr(cmap, "cmap", None)
                if not isinstance(table_map, dict):
                    continue
                for codepoint, glyph_name in table_map.items():
                    if not isinstance(codepoint, int):
                        continue
                    if codepoint < 0 or codepoint > 0x10FFFF:
                        continue
                    try:
                        ch = chr(codepoint)
                    except ValueError:
                        continue
                    if not _is_valid_unicode_text(ch):
                        continue
                    name_to_unicode.setdefault(str(glyph_name), ch)

        num_glyphs = 0
        try:
            maxp = tt.get("maxp")
            num_glyphs = int(getattr(maxp, "numGlyphs", 0))
        except Exception:
            num_glyphs = 0

        for gid in range(max(0, num_glyphs)):
            try:
                glyph_name = tt.getGlyphName(gid)
            except Exception:
                continue

            value = name_to_unicode.get(glyph_name, "")
            if _is_valid_unicode_text(value):
                gid_to_unicode[gid] = value
    finally:
        try:
            tt.close()
        except Exception:
            pass

    return gid_to_unicode


def _repair_pdf_tounicode_sync(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    """Rebuild/augment ToUnicode maps for embedded Type0/CIDFontType2 fonts."""
    import pikepdf

    stats = {
        "fonts_touched": 0,
        "maps_rebuilt": 0,
        "mappings_generated": 0,
        "mappings_total": 0,
        "invalid_entries_removed": 0,
        "mappings_overridden": 0,
        "stale_entries_removed": 0,
        "text_operands_sanitized": 0,
        "zero_bytes_removed": 0,
    }
    seen_fonts: set[tuple[int, int]] = set()
    seen_resources: set[tuple[int, int]] = set()
    seen_appearances: set[tuple[int, int]] = set()
    used_simple_codes: dict[tuple[int, int], set[int]] = {}
    seen_content_streams: set[tuple[int, int]] = set()
    seen_collect_appearances: set[tuple[int, int]] = set()

    def _font_stream_bytes(descriptor) -> bytes | None:
        if not isinstance(descriptor, pikepdf.Dictionary):
            return None
        for key in ("/FontFile", "/FontFile2", "/FontFile3"):
            stream_obj = _resolve_dictionary(descriptor.get(key))
            if stream_obj is None:
                continue
            try:
                return bytes(stream_obj.read_bytes())
            except Exception:
                continue
        return None

    def _cid_to_gid_mapping(cid_font) -> tuple[dict[int, int] | None, bool]:
        cid_to_gid = cid_font.get("/CIDToGIDMap")
        if cid_to_gid is None:
            return None, True
        if cid_to_gid == pikepdf.Name("/Identity"):
            return None, True

        stream_obj = _resolve_dictionary(cid_to_gid)
        if stream_obj is None:
            return None, True
        try:
            raw = bytes(stream_obj.read_bytes())
        except Exception:
            return None, True

        if len(raw) < 2:
            return {}, False

        mapping: dict[int, int] = {}
        pair_count = len(raw) // 2
        for cid in range(pair_count):
            gid = int.from_bytes(raw[cid * 2:(cid + 1) * 2], "big")
            mapping[cid] = gid
        return mapping, False

    def _collect_used_simple_font_codes(content_obj, resources) -> None:
        if resources is None:
            return
        resolved_obj = _resolve_dictionary(content_obj)
        if resolved_obj is None:
            return
        content_key = _obj_key(resolved_obj)
        if content_key and content_key in seen_content_streams:
            return
        if content_key:
            seen_content_streams.add(content_key)

        fonts = _resolve_dictionary(resources.get("/Font"))
        xobjects = _resolve_dictionary(resources.get("/XObject"))
        current_font = None
        try:
            instructions = pikepdf.parse_content_stream(resolved_obj)
        except Exception:
            return

        for instr in instructions:
            op = str(instr.operator)
            operands = list(instr.operands) if hasattr(instr, "operands") else []
            if op == "Tf" and operands and isinstance(fonts, pikepdf.Dictionary):
                font_ref = fonts.get(operands[0])
                current_font = _resolve_dictionary(font_ref)
                continue

            if op in ("Tj", "TJ", "'", '"') and isinstance(current_font, pikepdf.Dictionary):
                if current_font.get("/Subtype") not in (
                    pikepdf.Name("/Type1"),
                    pikepdf.Name("/MMType1"),
                    pikepdf.Name("/TrueType"),
                ):
                    continue
                font_key = _obj_key(current_font)
                if not font_key:
                    continue
                raw = _raw_text_bytes(op, operands)
                if not raw:
                    continue
                used_simple_codes.setdefault(font_key, set()).update(raw)
                continue

            if op == "Do" and operands and isinstance(xobjects, pikepdf.Dictionary):
                xobject = _resolve_dictionary(xobjects.get(operands[0]))
                if not isinstance(xobject, pikepdf.Dictionary):
                    continue
                if xobject.get("/Subtype") != pikepdf.Name("/Form"):
                    continue
                child_resources = _resolve_dictionary(xobject.get("/Resources")) or resources
                _collect_used_simple_font_codes(xobject, child_resources)

    def _collect_used_simple_font_codes_from_appearance(obj, fallback_resources) -> None:
        appearance_obj = _resolve_dictionary(obj)
        if not _is_pdf_mapping(appearance_obj):
            return
        appearance_key = _obj_key(appearance_obj)
        if appearance_key and appearance_key in seen_collect_appearances:
            return
        if appearance_key:
            seen_collect_appearances.add(appearance_key)

        appearance_resources = _resolve_dictionary(appearance_obj.get("/Resources")) or fallback_resources
        if hasattr(appearance_obj, "read_bytes"):
            _collect_used_simple_font_codes(appearance_obj, appearance_resources)

        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                _collect_used_simple_font_codes_from_appearance(child, appearance_resources)

    zero_byte_candidate_fonts: set[tuple[int, int]] = set()
    candidate_seen_resources: set[tuple[int, int]] = set()
    candidate_seen_fonts: set[tuple[int, int]] = set()
    candidate_seen_appearances: set[tuple[int, int]] = set()

    def _collect_zero_byte_candidates(resources) -> None:
        if not _is_pdf_mapping(resources):
            return
        resources_key = _obj_key(resources)
        if resources_key and resources_key in candidate_seen_resources:
            return
        if resources_key:
            candidate_seen_resources.add(resources_key)

        fonts = _resolve_dictionary(resources.get("/Font"))
        if isinstance(fonts, pikepdf.Dictionary):
            for _, font_obj in fonts.items():
                font_dict = _resolve_dictionary(font_obj)
                if not isinstance(font_dict, pikepdf.Dictionary):
                    continue
                if font_dict.get("/Subtype") not in (
                    pikepdf.Name("/Type1"),
                    pikepdf.Name("/MMType1"),
                    pikepdf.Name("/TrueType"),
                ):
                    continue

                font_key = _obj_key(font_dict)
                if font_key and font_key in candidate_seen_fonts:
                    continue
                if font_key:
                    candidate_seen_fonts.add(font_key)

                used_codes = used_simple_codes.get(font_key or (-1, -1), set())
                if 0 not in used_codes:
                    continue

                existing_stream = _resolve_dictionary(font_dict.get("/ToUnicode"))
                existing_map, _ = _parse_tounicode_map_details(existing_stream)
                descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
                font_bytes = _font_stream_bytes(descriptor)
                generated_map = {}
                if _simple_font_auto_unicode_policy(font_dict, font_bytes=font_bytes) != "blocked":
                    generated_map = _simple_font_unicode_map(font_dict, font_bytes)

                if _simple_font_zero_byte_repair_candidate(
                    font_dict,
                    used_codes=used_codes,
                    existing_map=existing_map,
                    generated_map=generated_map,
                ):
                    zero_byte_candidate_fonts.add(font_key)

        xobjects = _resolve_dictionary(resources.get("/XObject"))
        if not isinstance(xobjects, pikepdf.Dictionary):
            return
        for _, xobject in xobjects.items():
            xobject_dict = _resolve_dictionary(xobject)
            if not _is_pdf_mapping(xobject_dict):
                continue
            if xobject_dict.get("/Subtype") != pikepdf.Name("/Form"):
                continue
            child_resources = _resolve_dictionary(xobject_dict.get("/Resources")) or resources
            _collect_zero_byte_candidates(child_resources)

    def _collect_zero_byte_candidates_from_appearance(obj, fallback_resources) -> None:
        appearance_obj = _resolve_dictionary(obj)
        if not _is_pdf_mapping(appearance_obj):
            return
        appearance_key = _obj_key(appearance_obj)
        if appearance_key and appearance_key in candidate_seen_appearances:
            return
        if appearance_key:
            candidate_seen_appearances.add(appearance_key)

        appearance_resources = _resolve_dictionary(appearance_obj.get("/Resources")) or fallback_resources
        _collect_zero_byte_candidates(appearance_resources)

        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                _collect_zero_byte_candidates_from_appearance(child, appearance_resources)

    rewritten_content_streams: set[tuple[int, int]] = set()
    sanitized_font_keys: set[tuple[int, int]] = set()
    sanitized_appearance_streams: set[tuple[int, int]] = set()

    def _write_content_stream(pdf, content_obj, resolved_obj, instructions) -> None:
        stream_bytes = pikepdf.unparse_content_stream(instructions)
        if isinstance(content_obj, pikepdf.Page):
            content_obj["/Contents"] = pdf.make_stream(stream_bytes)
        else:
            resolved_obj.write(stream_bytes)

    def _sanitize_simple_font_zero_bytes(pdf, content_obj, resources) -> None:
        if resources is None or not zero_byte_candidate_fonts:
            return

        resolved_obj = _resolve_dictionary(content_obj)
        if resolved_obj is None:
            return
        content_key = _obj_key(resolved_obj)
        if content_key and content_key in rewritten_content_streams:
            return
        if content_key:
            rewritten_content_streams.add(content_key)

        fonts = _resolve_dictionary(resources.get("/Font"))
        xobjects = _resolve_dictionary(resources.get("/XObject"))
        parse_target = content_obj if isinstance(content_obj, pikepdf.Page) else resolved_obj
        current_font = None
        changed = False
        new_instructions = []

        try:
            instructions = list(pikepdf.parse_content_stream(parse_target))
        except Exception:
            return

        for instr in instructions:
            op = str(instr.operator)
            operands = list(instr.operands) if hasattr(instr, "operands") else []
            current_instruction = instr

            if op == "Tf" and operands and isinstance(fonts, pikepdf.Dictionary):
                current_font = _resolve_dictionary(fonts.get(operands[0]))
            elif op in ("Tj", "TJ", "'", '"') and isinstance(current_font, pikepdf.Dictionary):
                font_key = _obj_key(current_font)
                if font_key in zero_byte_candidate_fonts:
                    new_operands, removed = _sanitize_text_showing_zero_bytes(op, operands)
                    if removed > 0:
                        current_instruction = pikepdf.ContentStreamInstruction(
                            new_operands,
                            instr.operator,
                        )
                        stats["text_operands_sanitized"] += 1
                        stats["zero_bytes_removed"] += removed
                        sanitized_font_keys.add(font_key)
                        changed = True
            elif op == "Do" and operands and isinstance(xobjects, pikepdf.Dictionary):
                xobject = _resolve_dictionary(xobjects.get(operands[0]))
                if isinstance(xobject, pikepdf.Dictionary) and xobject.get("/Subtype") == pikepdf.Name("/Form"):
                    child_resources = _resolve_dictionary(xobject.get("/Resources")) or resources
                    _sanitize_simple_font_zero_bytes(pdf, xobject, child_resources)

            new_instructions.append(current_instruction)

        if changed:
            _write_content_stream(pdf, content_obj, resolved_obj, new_instructions)

    def _sanitize_simple_font_zero_bytes_from_appearance(pdf, obj, fallback_resources) -> None:
        appearance_obj = _resolve_dictionary(obj)
        if not _is_pdf_mapping(appearance_obj):
            return
        appearance_key = _obj_key(appearance_obj)
        if appearance_key and appearance_key in sanitized_appearance_streams:
            return
        if appearance_key:
            sanitized_appearance_streams.add(appearance_key)

        appearance_resources = _resolve_dictionary(appearance_obj.get("/Resources")) or fallback_resources
        if hasattr(appearance_obj, "read_bytes"):
            _sanitize_simple_font_zero_bytes(pdf, appearance_obj, appearance_resources)

        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                _sanitize_simple_font_zero_bytes_from_appearance(pdf, child, appearance_resources)

    def _rebuild_type0_tounicode(pdf, type0_font, cid_font) -> tuple[bool, int, int, int, int]:
        descriptor = _resolve_dictionary(cid_font.get("/FontDescriptor"))
        font_bytes = _font_stream_bytes(descriptor)
        gid_to_unicode: dict[int, str] = {}
        if font_bytes:
            try:
                gid_to_unicode = _collect_gid_to_unicode(font_bytes)
            except Exception:
                gid_to_unicode = {}

        cid_map, is_identity = _cid_to_gid_mapping(cid_font)
        generated: dict[int, str] = {}
        if gid_to_unicode:
            if is_identity:
                for gid, text in gid_to_unicode.items():
                    if gid < 0:
                        continue
                    generated[gid] = text
            else:
                for cid, gid in (cid_map or {}).items():
                    text = gid_to_unicode.get(gid)
                    if text:
                        generated[cid] = text

        existing_stream = _resolve_dictionary(type0_font.get("/ToUnicode"))
        existing_map, invalid_entries = _parse_tounicode_map_details(existing_stream)
        merged_map, overwritten = _merge_tounicode_maps(existing_map, generated)
        if not merged_map:
            return False, 0, len(generated), invalid_entries, overwritten

        if existing_map == merged_map and invalid_entries <= 0 and overwritten <= 0:
            return False, len(merged_map), len(generated), 0, 0

        max_code = max(merged_map.keys(), default=0)
        code_bytes = 1 if max_code <= 0xFF else (2 if max_code <= 0xFFFF else 4)
        cmap_bytes = _render_tounicode_cmap(merged_map, code_bytes)
        type0_font[pikepdf.Name("/ToUnicode")] = pdf.make_stream(cmap_bytes)
        return True, len(merged_map), len(generated), invalid_entries, overwritten

    def _rebuild_simple_font_tounicode(pdf, font_dict) -> tuple[bool, int, int, int, int, int]:
        font_key = _obj_key(font_dict)
        used_codes = used_simple_codes.get(font_key or (-1, -1), set())
        existing_stream = _resolve_dictionary(font_dict.get("/ToUnicode"))
        existing_map, invalid_entries = _parse_tounicode_map_details(existing_stream)
        if not used_codes:
            return False, len(existing_map), 0, invalid_entries, 0, 0

        missing_used_codes = {
            code
            for code in used_codes
            if code not in existing_map or not _is_valid_unicode_text(existing_map[code])
        }

        if not missing_used_codes and invalid_entries <= 0:
            return False, len(existing_map), 0, 0, 0, 0

        descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
        font_bytes = _font_stream_bytes(descriptor)
        if _simple_font_auto_unicode_policy(font_dict, font_bytes=font_bytes) == "blocked":
            return False, 0, 0, invalid_entries, 0, 0
        generated = _simple_font_unicode_map(font_dict, font_bytes)
        if not generated:
            return False, 0, 0, invalid_entries, 0, 0

        target_map = {
            code: text
            for code, text in generated.items()
            if not used_codes or code in used_codes
        }
        if not target_map:
            return False, 0, len(generated), invalid_entries, 0, 0

        overwritten = sum(
            1
            for code, text in target_map.items()
            if existing_map.get(code) and existing_map.get(code) != text
        )
        stale_entries_removed = sum(1 for code in existing_map if code not in target_map)

        if (
            existing_map == target_map
            and invalid_entries <= 0
            and overwritten <= 0
            and stale_entries_removed <= 0
        ):
            return False, len(target_map), len(target_map), 0, 0, 0

        cmap_bytes = _render_tounicode_cmap(target_map, 1)
        font_dict[pikepdf.Name("/ToUnicode")] = pdf.make_stream(cmap_bytes)
        return True, len(target_map), len(target_map), invalid_entries, overwritten, stale_entries_removed

    def _walk_resources(pdf, resources) -> None:
        if not _is_pdf_mapping(resources):
            return
        resources_key = _obj_key(resources)
        if resources_key and resources_key in seen_resources:
            return
        if resources_key:
            seen_resources.add(resources_key)

        fonts = _resolve_dictionary(resources.get("/Font"))
        if isinstance(fonts, pikepdf.Dictionary):
            for _, font_obj in fonts.items():
                type0_font = _resolve_dictionary(font_obj)
                if not isinstance(type0_font, pikepdf.Dictionary):
                    continue
                font_key = _obj_key(type0_font)
                if font_key and font_key in seen_fonts:
                    continue
                if font_key:
                    seen_fonts.add(font_key)

                if type0_font.get("/Subtype") != pikepdf.Name("/Type0"):
                    if type0_font.get("/Subtype") in (
                        pikepdf.Name("/Type1"),
                        pikepdf.Name("/MMType1"),
                        pikepdf.Name("/TrueType"),
                    ):
                        changed, merged_count, generated_count, invalid_entries, overwritten, stale_entries_removed = _rebuild_simple_font_tounicode(
                            pdf,
                            type0_font,
                        )
                    else:
                        continue
                else:
                    descendants = type0_font.get("/DescendantFonts")
                    if not isinstance(descendants, pikepdf.Array) or len(descendants) <= 0:
                        continue
                    cid_font = _resolve_dictionary(descendants[0])
                    if not isinstance(cid_font, pikepdf.Dictionary):
                        continue
                    if cid_font.get("/Subtype") != pikepdf.Name("/CIDFontType2"):
                        continue

                    changed, merged_count, generated_count, invalid_entries, overwritten = _rebuild_type0_tounicode(
                        pdf,
                        type0_font,
                        cid_font,
                    )
                    stale_entries_removed = 0
                if changed:
                    stats["fonts_touched"] += 1
                    stats["maps_rebuilt"] += 1
                stats["mappings_generated"] += generated_count
                stats["mappings_total"] += merged_count
                stats["invalid_entries_removed"] += invalid_entries
                stats["mappings_overridden"] += overwritten
                stats["stale_entries_removed"] += stale_entries_removed

        xobjects = _resolve_dictionary(resources.get("/XObject"))
        if not isinstance(xobjects, pikepdf.Dictionary):
            return
        for _, xobject in xobjects.items():
            xobject_dict = _resolve_dictionary(xobject)
            if not _is_pdf_mapping(xobject_dict):
                continue
            try:
                subtype = xobject_dict.get("/Subtype")
            except Exception:
                continue
            if subtype != pikepdf.Name("/Form"):
                continue
            child_resources = _resolve_dictionary(xobject_dict.get("/Resources"))
            _walk_resources(pdf, child_resources)

    def _walk_appearance_object(pdf, obj) -> None:
        appearance_obj = _resolve_dictionary(obj)
        if not _is_pdf_mapping(appearance_obj):
            return
        appearance_key = _obj_key(appearance_obj)
        if appearance_key and appearance_key in seen_appearances:
            return
        if appearance_key:
            seen_appearances.add(appearance_key)

        _walk_resources(pdf, _resolve_dictionary(appearance_obj.get("/Resources")))
        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                _walk_appearance_object(pdf, child)

    try:
        with pikepdf.open(str(input_path)) as pdf:
            for page in pdf.pages:
                page_resources = _resolve_dictionary(page.get("/Resources"))
                _collect_used_simple_font_codes(page, page_resources)
                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for annot in annots:
                        _collect_used_simple_font_codes_from_appearance(
                            annot.get("/AP"),
                            _resolve_dictionary(annot.get("/Resources")),
                        )

            acroform = _resolve_dictionary(pdf.Root.get("/AcroForm"))
            if _is_pdf_mapping(acroform):
                seen_fields: set[tuple[int, int]] = set()

                def _walk_field(field_obj) -> None:
                    field = _resolve_dictionary(field_obj)
                    if not isinstance(field, pikepdf.Dictionary):
                        return
                    field_key = _obj_key(field)
                    if field_key and field_key in seen_fields:
                        return
                    if field_key:
                        seen_fields.add(field_key)

                    field_resources = _resolve_dictionary(field.get("/DR"))
                    _collect_used_simple_font_codes_from_appearance(field.get("/AP"), field_resources)

                    kids = field.get("/Kids")
                    if isinstance(kids, pikepdf.Array):
                        for kid in kids:
                            _walk_field(kid)

                fields = acroform.get("/Fields")
                if isinstance(fields, pikepdf.Array):
                    for field in fields:
                        _walk_field(field)

            for page in pdf.pages:
                page_resources = _resolve_dictionary(page.get("/Resources"))
                _collect_zero_byte_candidates(page_resources)
                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for annot in annots:
                        _collect_zero_byte_candidates_from_appearance(
                            annot.get("/AP"),
                            _resolve_dictionary(annot.get("/Resources")),
                        )
            if isinstance(acroform, pikepdf.Dictionary):
                _collect_zero_byte_candidates(_resolve_dictionary(acroform.get("/DR")))
                fields = acroform.get("/Fields")
                if isinstance(fields, pikepdf.Array):
                    for field in fields:
                        field_dict = _resolve_dictionary(field)
                        if _is_pdf_mapping(field_dict):
                            _collect_zero_byte_candidates(_resolve_dictionary(field_dict.get("/DR")))
                            _collect_zero_byte_candidates_from_appearance(
                                field_dict.get("/AP"),
                                _resolve_dictionary(field_dict.get("/DR")),
                            )

            if zero_byte_candidate_fonts:
                for page in pdf.pages:
                    page_resources = _resolve_dictionary(page.get("/Resources"))
                    _sanitize_simple_font_zero_bytes(pdf, page, page_resources)
                    annots = page.get("/Annots")
                    if isinstance(annots, pikepdf.Array):
                        for annot in annots:
                            _sanitize_simple_font_zero_bytes_from_appearance(
                                pdf,
                                annot.get("/AP"),
                                _resolve_dictionary(annot.get("/Resources")),
                            )

                if isinstance(acroform, pikepdf.Dictionary):
                    fields = acroform.get("/Fields")
                    if isinstance(fields, pikepdf.Array):
                        for field in fields:
                            field_dict = _resolve_dictionary(field)
                            if not isinstance(field_dict, pikepdf.Dictionary):
                                continue
                            _sanitize_simple_font_zero_bytes_from_appearance(
                                pdf,
                                field_dict.get("/AP"),
                                _resolve_dictionary(field_dict.get("/DR")),
                            )

                for font_key in sanitized_font_keys:
                    used_simple_codes.setdefault(font_key, set()).discard(0)

            for page in pdf.pages:
                _walk_resources(pdf, _resolve_dictionary(page.get("/Resources")))
                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for annot in annots:
                        _walk_appearance_object(pdf, annot.get("/AP"))

            if isinstance(acroform, pikepdf.Dictionary):
                _walk_resources(pdf, _resolve_dictionary(acroform.get("/DR")))
                seen_fields = set()

                def _walk_field_repairs(field_obj) -> None:
                    field = _resolve_dictionary(field_obj)
                    if not isinstance(field, pikepdf.Dictionary):
                        return
                    field_key = _obj_key(field)
                    if field_key and field_key in seen_fields:
                        return
                    if field_key:
                        seen_fields.add(field_key)

                    field_resources = _resolve_dictionary(field.get("/DR"))
                    _walk_resources(pdf, field_resources)
                    _walk_appearance_object(pdf, field.get("/AP"))

                    kids = field.get("/Kids")
                    if isinstance(kids, pikepdf.Array):
                        for kid in kids:
                            _walk_field_repairs(kid)

                fields = acroform.get("/Fields")
                if isinstance(fields, pikepdf.Array):
                    for field in fields:
                        _walk_field_repairs(field)

            if stats["maps_rebuilt"] <= 0 and stats["zero_bytes_removed"] <= 0:
                return False, "No eligible ToUnicode repairs were applied", stats

            pdf.save(str(output_path))

        return (
            True,
            (
                f"ToUnicode repair completed "
                f"(fonts={stats['fonts_touched']}, maps={stats['maps_rebuilt']}, "
                f"generated={stats['mappings_generated']}, total={stats['mappings_total']}, "
                f"invalid_removed={stats['invalid_entries_removed']}, "
                f"overridden={stats['mappings_overridden']}, "
                f"stale_removed={stats['stale_entries_removed']}, "
                f"text_ops_sanitized={stats['text_operands_sanitized']}, "
                f"zero_bytes_removed={stats['zero_bytes_removed']})"
            ),
            stats,
        )
    except Exception as exc:
        return False, f"ToUnicode repair failed: {exc}", stats


async def _repair_pdf_tounicode(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    return await asyncio.to_thread(_repair_pdf_tounicode_sync, input_path, output_path)


def _embed_system_fonts_sync(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    import pikepdf

    stats = {
        "fonts_touched": 0,
        "fonts_embedded": 0,
        "fonts_missing": 0,
        "fonts_already_embedded": 0,
        "fonts_unsupported": 0,
        "cidtogid_added": 0,
    }
    seen_resources: set[tuple[int, int]] = set()
    seen_fonts: set[tuple[int, int]] = set()
    seen_appearances: set[tuple[int, int]] = set()

    def _has_embedded_font(descriptor) -> bool:
        if not isinstance(descriptor, pikepdf.Dictionary):
            return False
        return any(
            key in descriptor
            for key in (
                pikepdf.Name("/FontFile"),
                pikepdf.Name("/FontFile2"),
                pikepdf.Name("/FontFile3"),
            )
        )

    def _ensure_type1_descriptor(font_dict, descriptor, font_name: str):
        if isinstance(descriptor, pikepdf.Dictionary):
            return descriptor
        if _local_embed_support_kind(font_dict) != "type1_standard14":
            return descriptor

        descriptor_data = _ghostscript_type1_descriptor(font_name)
        if not descriptor_data:
            return descriptor

        descriptor = pikepdf.Dictionary({
            "/Type": pikepdf.Name("/FontDescriptor"),
            "/FontName": pikepdf.Name(f"/{_strip_subset_prefix(font_name)}"),
            "/Flags": int(descriptor_data["Flags"]),
            "/ItalicAngle": int(descriptor_data["ItalicAngle"]),
            "/Ascent": int(descriptor_data["Ascent"]),
            "/Descent": int(descriptor_data["Descent"]),
            "/CapHeight": int(descriptor_data["CapHeight"]),
            "/StemV": int(descriptor_data["StemV"]),
            "/FontBBox": pikepdf.Array(descriptor_data["FontBBox"]),
        })
        font_dict[pikepdf.Name("/FontDescriptor")] = descriptor
        return descriptor

    def _apply_type1_width_metrics(font_dict, descriptor, font_name: str) -> None:
        if _local_embed_support_kind(font_dict) != "type1_standard14":
            return

        descriptor_data = _ghostscript_type1_descriptor(font_name)
        if not descriptor_data:
            return

        existing_widths = font_dict.get("/Widths")
        existing_first_char = font_dict.get("/FirstChar")
        existing_last_char = font_dict.get("/LastChar")
        if (
            isinstance(existing_widths, pikepdf.Array)
            and len(existing_widths) > 0
            and existing_first_char is not None
            and existing_last_char is not None
        ):
            if isinstance(descriptor, pikepdf.Dictionary) and pikepdf.Name("/MissingWidth") not in descriptor:
                descriptor[pikepdf.Name("/MissingWidth")] = int(
                    descriptor_data.get("MissingWidth", 0) or 0
                )
            return

        first_char = int(descriptor_data.get("FirstChar", 0) or 0)
        last_char = int(descriptor_data.get("LastChar", 255) or 255)
        widths = descriptor_data.get("Widths")
        if isinstance(widths, list) and widths:
            font_dict[pikepdf.Name("/FirstChar")] = first_char
            font_dict[pikepdf.Name("/LastChar")] = last_char
            font_dict[pikepdf.Name("/Widths")] = pikepdf.Array(int(width) for width in widths)
        if isinstance(descriptor, pikepdf.Dictionary):
            descriptor[pikepdf.Name("/MissingWidth")] = int(descriptor_data.get("MissingWidth", 0) or 0)

    def _embed_descriptor_font(font_dict, descriptor, font_name: str, *, descendant_subtype=None) -> bool:
        descriptor = _ensure_type1_descriptor(font_dict, descriptor, font_name)
        if not isinstance(descriptor, pikepdf.Dictionary):
            stats["fonts_unsupported"] += 1
            return False
        if _has_embedded_font(descriptor):
            stats["fonts_already_embedded"] += 1
            return False

        font_bytes, matched_name, fontfile_key, length_info = _local_font_program(
            font_dict,
            font_name,
            descendant_subtype=descendant_subtype,
        )
        if not font_bytes or not fontfile_key or not length_info:
            stats["fonts_missing"] += 1
            return False

        stream = pdf.make_stream(font_bytes)
        for key, value in length_info.items():
            stream[pikepdf.Name(f"/{key}")] = int(value)
        descriptor[pikepdf.Name(fontfile_key)] = stream
        _apply_type1_width_metrics(font_dict, descriptor, font_name)
        if pikepdf.Name("/FontName") not in descriptor and font_name:
            descriptor[pikepdf.Name("/FontName")] = pikepdf.Name(f"/{_strip_subset_prefix(font_name)}")
        stats["fonts_embedded"] += 1
        stats["fonts_touched"] += 1
        logger.info(f"Embedded local font program for {font_name} using {matched_name}")
        return True

    def _walk_resources(resources) -> None:
        if not _is_pdf_mapping(resources):
            return
        resources_key = _obj_key(resources)
        if resources_key and resources_key in seen_resources:
            return
        if resources_key:
            seen_resources.add(resources_key)

        fonts = _resolve_dictionary(resources.get("/Font"))
        if isinstance(fonts, pikepdf.Dictionary):
            for _, font_obj in fonts.items():
                font_dict = _resolve_dictionary(font_obj)
                if not isinstance(font_dict, pikepdf.Dictionary):
                    continue

                font_key = _obj_key(font_dict)
                if font_key and font_key in seen_fonts:
                    continue
                if font_key:
                    seen_fonts.add(font_key)

                subtype = font_dict.get("/Subtype")
                if subtype == pikepdf.Name("/TrueType"):
                    descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
                    font_name = str(
                        font_dict.get("/BaseFont")
                        or (descriptor.get("/FontName") if isinstance(descriptor, pikepdf.Dictionary) else "")
                    )
                    _embed_descriptor_font(font_dict, descriptor, font_name)
                    continue

                if subtype in (pikepdf.Name("/Type1"), pikepdf.Name("/MMType1")):
                    descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
                    font_name = str(
                        font_dict.get("/BaseFont")
                        or (descriptor.get("/FontName") if isinstance(descriptor, pikepdf.Dictionary) else "")
                    )
                    changed = _embed_descriptor_font(font_dict, descriptor, font_name)
                    if not changed and _local_embed_support_kind(font_dict) != "type1_standard14":
                        stats["fonts_unsupported"] += 1
                    continue

                if subtype != pikepdf.Name("/Type0"):
                    stats["fonts_unsupported"] += 1
                    continue

                descendants = font_dict.get("/DescendantFonts")
                if not isinstance(descendants, pikepdf.Array) or not descendants:
                    stats["fonts_unsupported"] += 1
                    continue
                cid_font = _resolve_dictionary(descendants[0])
                if not isinstance(cid_font, pikepdf.Dictionary):
                    stats["fonts_unsupported"] += 1
                    continue
                if cid_font.get("/Subtype") != pikepdf.Name("/CIDFontType2"):
                    stats["fonts_unsupported"] += 1
                    continue

                descriptor = _resolve_dictionary(cid_font.get("/FontDescriptor"))
                font_name = str(cid_font.get("/BaseFont") or font_dict.get("/BaseFont"))
                changed = _embed_descriptor_font(
                    font_dict,
                    descriptor,
                    font_name,
                    descendant_subtype=cid_font.get("/Subtype"),
                )
                if changed and pikepdf.Name("/CIDToGIDMap") not in cid_font:
                    cid_font[pikepdf.Name("/CIDToGIDMap")] = pikepdf.Name("/Identity")
                    stats["cidtogid_added"] += 1

        xobjects = _resolve_dictionary(resources.get("/XObject"))
        if not isinstance(xobjects, pikepdf.Dictionary):
            return
        for _, xobject in xobjects.items():
            xobject_dict = _resolve_dictionary(xobject)
            if not _is_pdf_mapping(xobject_dict):
                continue
            try:
                subtype = xobject_dict.get("/Subtype")
            except Exception:
                continue
            if subtype != pikepdf.Name("/Form"):
                continue
            _walk_resources(_resolve_dictionary(xobject_dict.get("/Resources")))

    def _walk_appearance_object(obj) -> None:
        appearance_obj = _resolve_dictionary(obj)
        if not _is_pdf_mapping(appearance_obj):
            return
        appearance_key = _obj_key(appearance_obj)
        if appearance_key and appearance_key in seen_appearances:
            return
        if appearance_key:
            seen_appearances.add(appearance_key)

        _walk_resources(_resolve_dictionary(appearance_obj.get("/Resources")))
        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                _walk_appearance_object(child)

    try:
        with pikepdf.open(str(input_path)) as pdf:
            _walk_pdf_resource_graph(
                pdf,
                resolve_dictionary=_resolve_dictionary,
                walk_resources=_walk_resources,
                walk_appearance_object=_walk_appearance_object,
            )

            if stats["fonts_embedded"] <= 0:
                return False, "No embeddable local fonts were found", stats

            pdf.save(str(output_path))
        return (
            True,
            (
                f"Local font embedding completed "
                f"(embedded={stats['fonts_embedded']}, "
                f"missing={stats['fonts_missing']}, "
                f"unsupported={stats['fonts_unsupported']}, "
                f"CIDToGIDMap added={stats['cidtogid_added']})"
            ),
            stats,
        )
    except Exception as exc:
        return False, f"Local font embedding failed: {exc}", stats


async def _embed_system_fonts(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    return await asyncio.to_thread(_embed_system_fonts_sync, input_path, output_path)


def _ghostscript_embed_command(gs: str, input_path: Path, output_path: Path) -> tuple[str, ...]:
    always_embed = " ".join(GHOSTSCRIPT_ALWAYS_EMBED_FONTS)
    distiller_params = (
        f"<< /NeverEmbed [ ] /AlwaysEmbed [ {always_embed} ] >> setdistillerparams"
    )
    return (
        gs,
        "-q",
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-sDEVICE=pdfwrite",
        "-dEmbedAllFonts=true",
        "-dEmbedSubstituteFonts=true",
        "-dSubsetFonts=true",
        "-o",
        str(output_path),
        "-c",
        distiller_params,
        "-f",
        str(input_path),
    )


async def _rewrite_pdf_with_ghostscript_embed(
    input_path: Path,
    output_path: Path,
    timeout_seconds: int = 120,
) -> tuple[bool, str]:
    """Rewrite PDF through Ghostscript with aggressive font embedding options."""
    gs = resolve_binary("gs", explicit=get_settings().ghostscript_path)
    if not gs:
        return False, "Ghostscript not found in PATH"

    proc = await asyncio.create_subprocess_exec(
        *_ghostscript_embed_command(gs, input_path, output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=enriched_subprocess_env(),
    )
    try:
        stdout, stderr = await communicate_with_timeout(proc, timeout_seconds)
    except SubprocessTimeout:
        return False, f"Ghostscript timed out after {timeout_seconds}s"

    if proc.returncode != 0:
        output = (stderr or stdout).decode("utf-8", errors="replace").strip()
        return False, output or f"Ghostscript failed (exit {proc.returncode})"

    if not output_path.exists() or output_path.stat().st_size <= 0:
        return False, "Ghostscript produced no output"

    return True, "Ghostscript font-embed rewrite completed"


async def _attempt_font_lane(
    job_id: str,
    job: Job,
    settings: Settings,
    working_pdf: Path,
    tagged_pdf: Path,
    structure_json: dict,
    reviewed_alts: list[dict],
    lane: str,
    current_tagging_result=None,
):
    """Run one targeted font remediation lane and return tagged+validated output."""
    job_dir = create_job_dir(job_id)
    remediation_input: Path = working_pdf
    preprocess_message = ""
    preprocess_details: dict[str, object] = {}
    preprocess_skipped = False
    requires_retag = True
    effective_structure_json = structure_json

    if lane == FONT_LANE_REPAIR_DICTS:
        repaired = job_dir / "fontfix_repair_dicts.pdf"
        ok, message, stats = await _repair_pdf_font_dicts(tagged_pdf, repaired)
        preprocess_message = message
        preprocess_details = stats
        if not ok:
            return {
                "lane": lane,
                "attempted": True,
                "success": False,
                "error": message,
                "ocr_skipped": False,
                "ocr_message": "",
                "details": preprocess_details,
            }
        remediation_input = repaired
        requires_retag = False
    elif lane == FONT_LANE_REPAIR_TOUNICODE:
        repaired = job_dir / "fontfix_repair_tounicode.pdf"
        ok, message, stats = await _repair_pdf_tounicode(tagged_pdf, repaired)
        preprocess_message = message
        preprocess_details = stats
        if not ok:
            return {
                "lane": lane,
                "attempted": True,
                "success": False,
                "error": message,
                "ocr_skipped": False,
                "ocr_message": "",
                "details": preprocess_details,
            }
        remediation_input = repaired
        requires_retag = False
    elif lane == FONT_LANE_EMBED:
        embed_diagnostics = _inspect_font_diagnostics(
            tagged_pdf,
            include_used_code_analysis=False,
        )
        if _embed_lane_should_skip_local(embed_diagnostics):
            preprocess_message = "Local font embedding skipped: no supported local candidates for unresolved unembedded fonts"
            preprocess_details = {
                "local_embed_skipped": True,
                "diagnostics_summary": embed_diagnostics.get("summary", {}),
            }
            rewritten = job_dir / "fontfix_embedded_gs.pdf"
            ok, message = await _rewrite_pdf_with_ghostscript_embed(working_pdf, rewritten, timeout_seconds=settings.subprocess_timeout_ghostscript)
            preprocess_message = _join_messages(preprocess_message, message)
            if not ok:
                return {
                    "lane": lane,
                    "attempted": True,
                    "success": False,
                    "error": message,
                    "ocr_skipped": False,
                    "ocr_message": "",
                    "details": preprocess_details,
                }
            remediation_input = rewritten
        else:
            rewritten = job_dir / "fontfix_embedded_local.pdf"
            ok, message, stats = await _embed_system_fonts(tagged_pdf, rewritten)
            preprocess_message = message
            preprocess_details = stats
            if ok:
                remediation_input = rewritten
                requires_retag = False
            else:
                rewritten = job_dir / "fontfix_embedded_gs.pdf"
                ok, message = await _rewrite_pdf_with_ghostscript_embed(working_pdf, rewritten, timeout_seconds=settings.subprocess_timeout_ghostscript)
                preprocess_message = message
                if not ok:
                    return {
                        "lane": lane,
                        "attempted": True,
                        "success": False,
                        "error": message,
                        "ocr_skipped": False,
                        "ocr_message": "",
                        "details": preprocess_details,
                    }
                remediation_input = rewritten
    elif lane in (FONT_LANE_OCR_REDO, FONT_LANE_OCR_FORCE):
        mode = "redo" if lane == FONT_LANE_OCR_REDO else "force"
        ocr_output = job_dir / f"fontfix_{mode}_ocred.pdf"
        ocr_result = await run_ocr(
            input_path=working_pdf,
            output_path=ocr_output,
            language=settings.ocr_language,
            mode=mode,
            rotate_pages=settings.ocr_rotate_pages,
            deskew=settings.ocr_deskew,
            timeout_seconds=settings.subprocess_timeout_ocr,
        )
        preprocess_message = ocr_result.message
        preprocess_skipped = ocr_result.skipped
        if not ocr_result.success:
            return {
                "lane": lane,
                "attempted": True,
                "success": False,
                "error": ocr_result.message,
                "ocr_skipped": ocr_result.skipped,
                "ocr_message": ocr_result.message,
            }
        remediation_input = ocr_result.output_path
        if requires_retag:
            try:
                refresh_dir = job_dir / f"{lane}_structure"
                refresh_dir.mkdir(parents=True, exist_ok=True)
                refreshed_structure = await extract_structure(remediation_input, refresh_dir)
                candidate_structure_json = refreshed_structure.document_json
                if isinstance(candidate_structure_json, dict):
                    original_elements = structure_json.get("elements", []) if isinstance(structure_json, dict) else []
                    refreshed_elements = candidate_structure_json.get("elements", [])
                    original_figures = sum(
                        1
                        for element in original_elements
                        if isinstance(element, dict) and element.get("type") == "figure"
                    )
                    refreshed_figures = sum(
                        1
                        for element in refreshed_elements
                        if isinstance(element, dict) and element.get("type") == "figure"
                    )
                    approved_alts = [
                        entry for entry in reviewed_alts
                        if isinstance(entry, dict) and entry.get("status") == "approved"
                    ]
                    if approved_alts and original_figures != refreshed_figures:
                        preprocess_details["structure_refreshed"] = False
                        preprocess_details["structure_refresh_skipped"] = "figure_count_changed"
                        preprocess_details["original_figures"] = original_figures
                        preprocess_details["refreshed_figures"] = refreshed_figures
                    else:
                        effective_structure_json = candidate_structure_json
                        preprocess_details["structure_refreshed"] = True
                        preprocess_details["structure_elements"] = len(
                            refreshed_elements if isinstance(refreshed_elements, list) else []
                        )
            except Exception as exc:
                preprocess_details["structure_refreshed"] = False
                preprocess_details["structure_refresh_error"] = str(exc)
    else:
        return {
            "lane": lane,
            "attempted": True,
            "success": False,
            "error": f"Unsupported font remediation lane: {lane}",
            "ocr_skipped": False,
            "ocr_message": "",
        }

    if requires_retag:
        remediation_output = get_output_path(job_id, f"accessible_{lane}_{job.original_filename}")
        tagging_result = await tag_pdf(
            input_path=remediation_input,
            output_path=remediation_output,
            structure_json=effective_structure_json,
            alt_texts=reviewed_alts,
            original_filename=job.original_filename or "",
        )
        validation_target = tagging_result.output_path
    else:
        remediation_output = remediation_input
        tagging_result = current_tagging_result
        validation_target = remediation_input

    if lane in (FONT_LANE_OCR_REDO, FONT_LANE_OCR_FORCE):
        post_ocr_repaired = job_dir / f"{lane}_post_font_dicts.pdf"
        ok, post_message, post_stats = await _repair_pdf_font_dicts(validation_target, post_ocr_repaired)
        if ok:
            validation_target = post_ocr_repaired
            remediation_output = post_ocr_repaired
            preprocess_details["post_ocr_font_dicts"] = post_stats
            preprocess_message = _join_messages(preprocess_message, post_message)

        post_ocr_widths = job_dir / f"{lane}_post_width_sync.pdf"
        ok, width_message, width_stats = await _sync_pdf_cid_cff_widths(validation_target, post_ocr_widths)
        if ok:
            validation_target = post_ocr_widths
            remediation_output = post_ocr_widths
            preprocess_details["post_ocr_width_sync"] = width_stats
            preprocess_message = _join_messages(preprocess_message, width_message)

    validation = await validate_pdf(
        pdf_path=validation_target,
        verapdf_path=settings.verapdf_path,
        flavour=settings.verapdf_flavour,
        timeout_seconds=settings.subprocess_timeout_validation,
    )

    return {
        "lane": lane,
        "attempted": True,
        "success": True,
        "ocr_skipped": preprocess_skipped,
        "ocr_message": preprocess_message if lane in (FONT_LANE_OCR_REDO, FONT_LANE_OCR_FORCE) else "",
        "message": preprocess_message,
        "details": preprocess_details,
        "requires_retag": requires_retag,
        "preprocessed_path": str(remediation_input),
        "structure_json": effective_structure_json,
        "output_path": remediation_output,
        "tagging_result": tagging_result,
        "validation": validation,
    }


async def _update_step(
    db: AsyncSession,
    job_id: str,
    step_name: str,
    status: str,
    result: dict | None = None,
    error: str | None = None,
):
    """Update a job step's status in the database."""
    stmt = select(JobStep).where(
        JobStep.job_id == job_id, JobStep.step_name == step_name
    )
    row = await db.execute(stmt)
    step = row.scalar_one_or_none()
    if step is None:
        step = JobStep(job_id=job_id, step_name=step_name)
        db.add(step)
        await db.flush()

    step.status = status
    if status == "running":
        step.started_at = datetime.now(timezone.utc)
    if status in ("complete", "failed", "skipped"):
        step.completed_at = datetime.now(timezone.utc)
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
            await _update_step(db, job_id, "classify", "complete", result={
                "type": classification.type,
                "confidence": classification.confidence,
                "pages_with_text": classification.pages_with_text,
                "total_pages": classification.total_pages,
            })
            job_manager.emit_progress(
                job_id, step="classify", status="complete",
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
                    settings.ocr_language,
                    rotate_pages=settings.ocr_rotate_pages,
                    deskew=settings.ocr_deskew,
                    timeout_seconds=settings.subprocess_timeout_ocr,
                )

                if ocr_result.success:
                    working_pdf = ocr_result.output_path
                    await _update_step(db, job_id, "ocr", "complete", result={
                        "skipped": ocr_result.skipped,
                        "message": ocr_result.message,
                    })
                    job_manager.emit_progress(job_id, step="ocr", status="complete")
                else:
                    await _update_step(db, job_id, "ocr", "failed", error=ocr_result.message)
                    job_manager.emit_progress(
                        job_id, step="ocr", status="failed", message=ocr_result.message,
                    )
                    raise RuntimeError(
                        f"OCR failed for {classification.type} document: {ocr_result.message}"
                    )
            else:
                await _update_step(db, job_id, "ocr", "skipped")
                job_manager.emit_progress(job_id, step="ocr", status="skipped")

            # ── Step 3: Structure Extraction ──
            current_step = "structure"
            await _update_step(db, job_id, "structure", "running")
            job_manager.emit_progress(job_id, step="structure", status="running")

            structure = await extract_structure(working_pdf, job_dir)
            if structure.processed_pdf_path:
                working_pdf = structure.processed_pdf_path

            toc_llm_assist = {
                "attempted": False,
                "applied": False,
                "reason": "disabled",
                "groups_considered": 0,
                "groups_applied": 0,
            }
            if settings.assist_toc_with_llm:
                llm_client = make_llm_client(settings)
                try:
                    structure.document_json, toc_llm_assist = await enhance_toc_structure_with_llm(
                        pdf_path=working_pdf,
                        structure_json=structure.document_json,
                        original_filename=job.original_filename or input_path.name,
                        llm_client=llm_client,
                    )
                except Exception as exc:
                    logger.warning(f"TOC LLM assist failed for {job.original_filename}: {exc}")
                    toc_llm_assist = {
                        "attempted": True,
                        "applied": False,
                        "reason": str(exc),
                        "groups_considered": 0,
                        "groups_applied": 0,
                    }
                finally:
                    await llm_client.close()

            elements = structure.document_json.get("elements", [])
            headings_count = sum(1 for el in elements if el.get("type") == "heading")
            tables_count = sum(1 for el in elements if el.get("type") == "table")
            toc_caption_count = sum(1 for el in elements if el.get("type") == "toc_caption")
            toc_item_count = sum(1 for el in elements if el.get("type") in {"toc_item", "toc_item_table"})

            job.structure_json = json.dumps(structure.document_json)
            await _update_step(db, job_id, "structure", "complete", result={
                "page_count": structure.page_count,
                "headings": headings_count,
                "tables": tables_count,
                "figures": structure.figures_count,
                "toc_captions": toc_caption_count,
                "toc_items": toc_item_count,
                "toc_llm_assist": toc_llm_assist,
            })
            job_manager.emit_progress(
                job_id, step="structure", status="complete",
                result={
                    "figures_found": structure.figures_count,
                    "toc_captions": toc_caption_count,
                    "toc_items": toc_item_count,
                    "toc_llm_assist_applied": bool(toc_llm_assist.get("applied", False)),
                },
            )

            # ── Step 4: Alt Text Generation ──
            current_step = "alt_text"
            if structure.figures:
                await _update_step(db, job_id, "alt_text", "running")
                job_manager.emit_progress(job_id, step="alt_text", status="running")

                llm_client = make_llm_client(settings)

                try:
                    alt_texts = await generate_alt_text(
                        structure.figures,
                        llm_client,
                        job=job,
                        original_filename=job.original_filename,
                    )
                finally:
                    await llm_client.close()

                structure.document_json, figure_reclassification = _apply_figure_reclassification(
                    structure.document_json,
                    alt_texts,
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
                    generated_text = (alt.generated_text or "").strip()
                    status = alt.status
                    caption_fallback = (fig.caption or "").strip()

                    if settings.auto_approve_generated_alt_text:
                        normalized = generated_text.lower()
                        if normalized == "decorative":
                            status = "rejected"
                            generated_text = ""
                        elif not generated_text and caption_fallback:
                            status = "approved"
                            generated_text = caption_fallback
                        elif not generated_text or (generated_text.startswith("[") and generated_text.endswith("]")):
                            status = "approved"
                        else:
                            status = "approved"

                    if status == "approved":
                        approved_count += 1
                    elif status == "rejected":
                        rejected_count += 1
                    db.add(AltTextEntry(
                        job_id=job_id,
                        figure_index=alt.figure_index,
                        image_path=str(fig.path),
                        generated_text=generated_text or None,
                        status=status,
                    ))
                await db.commit()
                figure_change_specs = figure_applied_change_specs(
                    figures=structure.figures,
                    alt_texts=alt_texts,
                )

                await _update_step(db, job_id, "alt_text", "complete", result={
                    "count": len(alt_texts),
                    "approved": approved_count,
                    "rejected": rejected_count,
                    "reviewable_changes": len(figure_change_specs),
                    "reclassified": reclassified_count,
                    "figure_reclassification": figure_reclassification,
                    "auto_approve_enabled": settings.auto_approve_generated_alt_text,
                })
                job_manager.emit_progress(
                    job_id, step="alt_text", status="complete",
                    result={
                        "count": len(alt_texts),
                        "approved": approved_count,
                        "rejected": rejected_count,
                        "reviewable_changes": len(figure_change_specs),
                        "reclassified": reclassified_count,
                        "auto_approve_enabled": settings.auto_approve_generated_alt_text,
                    },
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
                await _update_step(db, job_id, "alt_text", "skipped")
                job_manager.emit_progress(job_id, step="alt_text", status="skipped")

            # No figures = skip review, go straight to tagging
            await run_tagging_and_validation(
                job_id, db, settings, job_manager, working_pdf, structure.document_json
            )

        except Exception as e:
            logger.exception(f"Pipeline failed for job {job_id}")
            await db.rollback()
            # Sanitize error: strip server paths
            user_error = re.sub(r"/\S*", "", str(e)).strip(": ")
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
                job_id, step=current_step or "error", status="failed", message=user_error,
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
    """Run steps 5-6 (tagging + validation). Called after review approval."""
    job = await db.get(Job, job_id)
    if not job:
        return

    if working_pdf is None:
        # Check if an OCR'd version exists in the processing directory
        ocred_path = settings.processing_dir / job_id / "ocred.pdf"
        working_pdf = ocred_path if ocred_path.exists() else Path(job.input_path)
    if structure_json is None:
        structure_json = json.loads(job.structure_json) if job.structure_json else {}

    try:
        job.status = "processing"
        await db.commit()

        baseline_validation = await validate_pdf(
            pdf_path=working_pdf,
            verapdf_path=settings.verapdf_path,
            flavour=settings.verapdf_flavour,
            timeout_seconds=settings.subprocess_timeout_validation,
        )

        # ── Step 5: Tagging ──
        await _update_step(db, job_id, "tagging", "running")
        job_manager.emit_progress(job_id, step="tagging", status="running")

        output_path = get_output_path(job_id, f"accessible_{job.original_filename}")

        # Gather approved alt texts
        result = await db.execute(
            select(AltTextEntry).where(
                AltTextEntry.job_id == job_id,
                AltTextEntry.status.in_(("approved", "rejected")),
            )
        )
        reviewed_alt_entries = result.scalars().all()
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
        structure_json, pretag_table_intelligence = await _apply_pretag_table_intelligence(
            job=job,
            settings=settings,
            structure_json=structure_json,
        )
        working_pdf, pretag_form_intelligence = await _apply_pretag_form_intelligence(
            job=job,
            settings=settings,
            working_pdf=working_pdf,
            structure_json=structure_json,
        )
        job.structure_json = json.dumps(structure_json)

        tagging_result = await tag_pdf(
            input_path=working_pdf,
            output_path=output_path,
            structure_json=structure_json,
            alt_texts=reviewed_alts,
            original_filename=job.original_filename or "",
        )

        job.output_path = str(tagging_result.output_path)
        await _update_step(db, job_id, "tagging", "complete", result={
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
            "grounded_text_auto_applied": bool(pretag_grounded_text.get("applied")),
            "grounded_text_auto_applied_count": int(pretag_grounded_text.get("applied_count", 0) or 0),
            "grounded_text_code_auto_applied_count": int(pretag_grounded_text.get("applied_code_text_count", 0) or 0),
            "table_intelligence_auto_applied": bool(pretag_table_intelligence.get("applied")),
            "table_intelligence_auto_applied_count": int(pretag_table_intelligence.get("applied_count", 0) or 0),
            "table_intelligence_confirmed_count": int(pretag_table_intelligence.get("confirmed_count", 0) or 0),
            "table_intelligence_set_headers_count": int(pretag_table_intelligence.get("set_headers_count", 0) or 0),
            "form_intelligence_auto_applied": bool(pretag_form_intelligence.get("applied")),
            "form_intelligence_auto_applied_count": int(pretag_form_intelligence.get("applied_count", 0) or 0),
        })
        job_manager.emit_progress(job_id, step="tagging", status="complete")

        # ── Step 6: Validation ──
        await _update_step(db, job_id, "validation", "running")
        job_manager.emit_progress(job_id, step="validation", status="running")

        validation = await validate_pdf(
            pdf_path=tagging_result.output_path,
            verapdf_path=settings.verapdf_path,
            flavour=settings.verapdf_flavour,
            timeout_seconds=settings.subprocess_timeout_validation,
        )

        selected_tagging_result = tagging_result
        selected_validation = validation
        remediation_pdf_features = _inspect_pdf_features(working_pdf)
        font_remediation = {
            "attempted": False,
            "eligible": False,
            "applied": False,
            "strategy": "safe_first_non_destructive",
            "document_features": remediation_pdf_features,
            "unicode_gate": None,
            "preflight_diagnostics": None,
            "postflight_diagnostics": None,
            "first_pass_errors": _error_count(validation),
            "first_pass_warnings": _warning_count(validation),
            "second_pass_errors": None,
            "second_pass_warnings": None,
            "lanes_planned": [],
            "lanes_skipped": [],
            "lane_results": [],
            "selected_lane": None,
            "selected_lanes": [],
            "error": None,
            "ocr_message": "",
            "ocr_skipped": False,
        }
        fidelity_source_pdf = Path(job.input_path)

        if not validation.compliant and _has_font_errors(validation.violations):
            initial_font_diagnostics = _inspect_font_diagnostics(
                Path(job.output_path or tagging_result.output_path),
                validation.violations,
            )
            font_remediation["preflight_diagnostics"] = initial_font_diagnostics
            unicode_gate = None
            if any(
                v.severity == "error"
                and any(marker in str(v.rule_id) for marker in FONT_UNICODE_RULE_MARKERS)
                for v in validation.violations
            ):
                gate_font_diagnostics = initial_font_diagnostics
                summary = initial_font_diagnostics.get("summary") if isinstance(initial_font_diagnostics, dict) else None
                profiles = initial_font_diagnostics.get("profiles") if isinstance(initial_font_diagnostics, dict) else None
                if (
                    isinstance(summary, dict)
                    and isinstance(profiles, list)
                    and int(summary.get("fonts_total", 0) or 0) > len(profiles)
                ):
                    gate_font_diagnostics = _inspect_font_diagnostics(
                        Path(job.output_path or tagging_result.output_path),
                        validation.violations,
                        profile_limit=512,
                    )
                unicode_gate = _unicode_repair_gate_from_diagnostics(
                    gate_font_diagnostics,
                    violations=validation.violations,
                )
            font_remediation["unicode_gate"] = unicode_gate
            planned_lanes, skipped_lanes = _font_remediation_lanes(
                validation.violations,
                classification=job.classification,
                pdf_features=remediation_pdf_features,
                settings=settings,
                unicode_gate=unicode_gate,
            )
            font_remediation["lanes_planned"] = planned_lanes
            font_remediation["lanes_skipped"] = skipped_lanes
            font_remediation["eligible"] = bool(planned_lanes)
            font_remediation["attempted"] = bool(planned_lanes)

            if not planned_lanes:
                font_remediation["error"] = "No safe font remediation lane eligible"
            else:
                try:
                    best_validation = selected_validation
                    best_tagging_result = selected_tagging_result
                    best_working_pdf = working_pdf
                    best_structure_json = structure_json
                    best_tagged_pdf = Path(job.output_path or tagging_result.output_path)
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
                                if requires_retag and isinstance(preprocessed_path, str) and preprocessed_path:
                                    best_working_pdf = Path(preprocessed_path)
                                    fidelity_source_pdf = best_working_pdf
                                if isinstance(attempt["output_path"], Path):
                                    best_tagged_pdf = attempt["output_path"]
                                else:
                                    best_tagged_pdf = Path(str(attempt["output_path"]))
                                candidate_structure_json = attempt.get("structure_json")
                                if isinstance(candidate_structure_json, dict):
                                    best_structure_json = candidate_structure_json
                                best_output_path = str(best_tagged_pdf)
                                selected_lanes.append(lane)

                        font_remediation["lane_results"].append(lane_result)

                        if best_validation.compliant:
                            # No need to keep exploring lanes after full compliance.
                            break

                    if not any(bool(r.get("success")) for r in font_remediation["lane_results"]):
                        font_remediation["error"] = "All font remediation lanes failed"

                    font_remediation["ocr_message"] = " | ".join(ocr_messages)
                    font_remediation["ocr_skipped"] = ocr_skipped
                    font_remediation["second_pass_errors"] = _error_count(best_validation)
                    font_remediation["second_pass_warnings"] = _warning_count(best_validation)

                    if selected_lanes:
                        selected_validation = best_validation
                        selected_tagging_result = best_tagging_result
                        job.output_path = best_output_path
                        if isinstance(best_structure_json, dict):
                            structure_json = best_structure_json
                            job.structure_json = json.dumps(best_structure_json)
                        font_remediation["selected_lane"] = selected_lanes[-1]
                        font_remediation["selected_lanes"] = selected_lanes
                        if fidelity_source_pdf != Path(job.input_path):
                            font_remediation["selected_preprocessed_path"] = str(fidelity_source_pdf)
                        font_remediation["applied"] = True
                except Exception as exc:
                    logger.exception(f"Font remediation lane evaluation failed for job {job_id}")
                    font_remediation["error"] = str(exc)

            font_remediation["postflight_diagnostics"] = _inspect_font_diagnostics(
                Path(job.output_path or tagging_result.output_path),
                selected_validation.violations,
            )

        llm_font_map_auto: dict[str, object] = {
            "enabled": bool(settings.auto_apply_llm_font_map),
            "attempted": False,
            "applied": False,
            "reason": "",
        }
        validation_payload = _build_validation_payload(
            baseline_validation=baseline_validation,
            selected_validation=selected_validation,
            settings=settings,
            font_remediation=font_remediation,
            tagging_result=selected_tagging_result,
            llm_font_map_auto=llm_font_map_auto,
        )

        await _update_step(db, job_id, "validation", "complete", result={
            "compliant": selected_validation.compliant,
            "violations_count": len(selected_validation.violations),
            "font_remediation_attempted": bool(font_remediation["attempted"]),
            "font_remediation_applied": bool(font_remediation["applied"]),
        })
        job_manager.emit_progress(
            job_id, step="validation", status="complete",
            result={
                "compliant": selected_validation.compliant,
                "font_remediation_attempted": bool(font_remediation["attempted"]),
                "font_remediation_applied": bool(font_remediation["applied"]),
            },
        )

        await _update_step(db, job_id, "fidelity", "running")
        job_manager.emit_progress(job_id, step="fidelity", status="running")

        fidelity_report, review_tasks = assess_fidelity(
            input_pdf=Path(job.input_path),
            output_pdf=Path(job.output_path or tagging_result.output_path),
            comparison_source_pdf=fidelity_source_pdf,
            structure_json=structure_json or {},
            alt_entries=[
                {
                    "figure_index": entry.figure_index,
                    "generated_text": entry.generated_text,
                    "edited_text": entry.edited_text,
                    "status": entry.status,
                }
                for entry in reviewed_alt_entries
            ],
            validation_report=validation_payload,
            raw_validation_report=selected_validation.raw_report,
            tagging_metrics=validation_payload["tagging"],
            classification=job.classification,
        )
        review_tasks, fidelity_report, grounded_text_adjudication = await _adjudicate_grounded_text_candidates(
            job=job,
            settings=settings,
            review_tasks=review_tasks,
            fidelity_report=fidelity_report,
        )
        safe_grounded_resolutions = _collect_safe_grounded_text_resolutions(grounded_text_adjudication)
        if safe_grounded_resolutions and _blocking_review_task_count(review_tasks) > 0:
            retry_structure_json, retry_audit = _apply_grounded_text_resolutions_to_structure(
                structure_json or {},
                safe_grounded_resolutions,
            )
            if bool(retry_audit.get("applied")):
                retry_output_path = get_output_path(job_id, f"accessible_grounded_retry_{job.original_filename}")
                retry_tagging_result = await tag_pdf(
                    input_path=working_pdf,
                    output_path=retry_output_path,
                    structure_json=retry_structure_json,
                    alt_texts=reviewed_alts,
                    original_filename=job.original_filename or "",
                )
                retry_validation = await validate_pdf(
                    pdf_path=retry_tagging_result.output_path,
                    verapdf_path=settings.verapdf_path,
                    flavour=settings.verapdf_flavour,
                    timeout_seconds=settings.subprocess_timeout_validation,
                )
                retry_validation_payload = _build_validation_payload(
                    baseline_validation=baseline_validation,
                    selected_validation=retry_validation,
                    settings=settings,
                    font_remediation=font_remediation,
                    tagging_result=retry_tagging_result,
                    llm_font_map_auto=llm_font_map_auto,
                )
                retry_fidelity_report, retry_review_tasks = assess_fidelity(
                    input_pdf=Path(job.input_path),
                    output_pdf=retry_tagging_result.output_path,
                    comparison_source_pdf=fidelity_source_pdf,
                    structure_json=retry_structure_json,
                    alt_entries=[
                        {
                            "figure_index": entry.figure_index,
                            "generated_text": entry.generated_text,
                            "edited_text": entry.edited_text,
                            "status": entry.status,
                        }
                        for entry in reviewed_alt_entries
                    ],
                    validation_report=retry_validation_payload,
                    raw_validation_report=retry_validation.raw_report,
                    tagging_metrics=retry_validation_payload["tagging"],
                    classification=job.classification,
                )
                retry_blocking_count = _blocking_review_task_count(retry_review_tasks)
                if (
                    retry_validation.compliant
                    and retry_blocking_count < _blocking_review_task_count(review_tasks)
                    and not _has_grounded_text_candidate_task(retry_review_tasks)
                ):
                    structure_json = retry_structure_json
                    job.structure_json = json.dumps(retry_structure_json)
                    job.output_path = str(retry_tagging_result.output_path)
                    selected_tagging_result = retry_tagging_result
                    selected_validation = retry_validation
                    validation_payload = retry_validation_payload
                    review_tasks = retry_review_tasks
                    fidelity_report = retry_fidelity_report
                else:
                    retry_review_tasks, retry_fidelity_report, _ = await _adjudicate_grounded_text_candidates(
                        job=job,
                        settings=settings,
                        review_tasks=retry_review_tasks,
                        fidelity_report=retry_fidelity_report,
                    )
                    if retry_validation.compliant and _blocking_review_task_count(retry_review_tasks) < _blocking_review_task_count(review_tasks):
                        structure_json = retry_structure_json
                        job.structure_json = json.dumps(retry_structure_json)
                        job.output_path = str(retry_tagging_result.output_path)
                        selected_tagging_result = retry_tagging_result
                        selected_validation = retry_validation
                        validation_payload = retry_validation_payload
                        review_tasks = retry_review_tasks
                        fidelity_report = retry_fidelity_report

        review_task_metadata_overrides: dict[tuple[str, str], dict[str, object]] = {}
        auto_audit, candidate_validation, candidate_output_pdf, metadata_overrides = await _attempt_auto_llm_font_map(
            job=job,
            settings=settings,
            output_pdf=Path(job.output_path or tagging_result.output_path),
            current_validation=selected_validation,
            review_tasks=review_tasks,
        )
        llm_font_map_auto = auto_audit
        review_task_metadata_overrides.update(metadata_overrides)
        auto_applied_change_specs: list[dict[str, object]] = list(pre_applied_change_specs or [])
        validation_payload = _build_validation_payload(
            baseline_validation=baseline_validation,
            selected_validation=selected_validation,
            settings=settings,
            font_remediation=font_remediation,
            tagging_result=selected_tagging_result,
            llm_font_map_auto=llm_font_map_auto,
        )

        if structure_json and _blocking_task_count(review_tasks) > 0:
            suggested_review_tasks, auto_structure_json, applied_specs = await auto_apply_structure_review_tasks(
                job=job,
                settings=settings,
                review_tasks=review_tasks,
                structure_json=structure_json,
            )
            review_tasks = suggested_review_tasks
            if applied_specs and auto_structure_json != structure_json:
                auto_output_path = get_output_path(job_id, f"accessible_auto_review_{job.original_filename}")
                auto_tagging_result = await tag_pdf(
                    input_path=working_pdf,
                    output_path=auto_output_path,
                    structure_json=auto_structure_json,
                    alt_texts=reviewed_alts,
                    original_filename=job.original_filename or "",
                )
                auto_validation = await validate_pdf(
                    pdf_path=auto_tagging_result.output_path,
                    verapdf_path=settings.verapdf_path,
                    flavour=settings.verapdf_flavour,
                    timeout_seconds=settings.subprocess_timeout_validation,
                )
                auto_validation_payload = _build_validation_payload(
                    baseline_validation=baseline_validation,
                    selected_validation=auto_validation,
                    settings=settings,
                    font_remediation=font_remediation,
                    tagging_result=auto_tagging_result,
                    llm_font_map_auto=llm_font_map_auto,
                )
                auto_fidelity_report, auto_review_tasks = assess_fidelity(
                    input_pdf=Path(job.input_path),
                    output_pdf=auto_tagging_result.output_path,
                    comparison_source_pdf=fidelity_source_pdf,
                    structure_json=auto_structure_json,
                    alt_entries=[
                        {
                            "figure_index": entry.figure_index,
                            "generated_text": entry.generated_text,
                            "edited_text": entry.edited_text,
                            "status": entry.status,
                        }
                        for entry in reviewed_alt_entries
                    ],
                    validation_report=auto_validation_payload,
                    raw_validation_report=auto_validation.raw_report,
                    tagging_metrics=auto_validation_payload["tagging"],
                    classification=job.classification,
                )
                auto_review_tasks, auto_fidelity_report, _ = await _adjudicate_grounded_text_candidates(
                    job=job,
                    settings=settings,
                    review_tasks=auto_review_tasks,
                    fidelity_report=auto_fidelity_report,
                )
                if (
                    auto_validation.compliant
                    and _blocking_task_count(auto_review_tasks) < _blocking_task_count(review_tasks)
                ):
                    structure_json = auto_structure_json
                    job.structure_json = json.dumps(auto_structure_json)
                    job.output_path = str(auto_tagging_result.output_path)
                    selected_tagging_result = auto_tagging_result
                    selected_validation = auto_validation
                    validation_payload = auto_validation_payload
                    fidelity_report = auto_fidelity_report
                    review_tasks = auto_review_tasks
                    auto_applied_change_specs.extend(applied_specs)

        if candidate_validation is not None and candidate_output_pdf is not None:
            candidate_validation_payload = _build_validation_payload(
                baseline_validation=baseline_validation,
                selected_validation=candidate_validation,
                settings=settings,
                font_remediation=font_remediation,
                tagging_result=selected_tagging_result,
                llm_font_map_auto=llm_font_map_auto,
            )
            candidate_fidelity_report, candidate_review_tasks = assess_fidelity(
                input_pdf=Path(job.input_path),
                output_pdf=candidate_output_pdf,
                comparison_source_pdf=fidelity_source_pdf,
                structure_json=structure_json or {},
                alt_entries=[
                    {
                        "figure_index": entry.figure_index,
                        "generated_text": entry.generated_text,
                        "edited_text": entry.edited_text,
                        "status": entry.status,
                    }
                    for entry in reviewed_alt_entries
                ],
                validation_report=candidate_validation_payload,
                raw_validation_report=candidate_validation.raw_report,
                tagging_metrics=candidate_validation_payload["tagging"],
                classification=job.classification,
            )
            if _fidelity_not_worse(candidate_fidelity_report, fidelity_report):
                selected_validation = candidate_validation
                job.output_path = str(candidate_output_pdf)
                validation_payload = candidate_validation_payload
                fidelity_report = candidate_fidelity_report
                review_tasks = candidate_review_tasks
            else:
                llm_font_map_auto["applied"] = False
                llm_font_map_auto["reason"] = "fidelity_regression"
                validation_payload = _build_validation_payload(
                    baseline_validation=baseline_validation,
                    selected_validation=selected_validation,
                    settings=settings,
                    font_remediation=font_remediation,
                    tagging_result=selected_tagging_result,
                    llm_font_map_auto=llm_font_map_auto,
                )

        validation_payload["fidelity"] = fidelity_report
        if review_task_metadata_overrides:
            for key, metadata in review_task_metadata_overrides.items():
                review_tasks = _merge_review_task_metadata(
                    review_tasks,
                    task_type=key[0],
                    source=key[1],
                    metadata=metadata,
                )
        job.validation_json = json.dumps(validation_payload)
        job.fidelity_json = json.dumps(fidelity_report)

        await db.execute(delete(ReviewTask).where(ReviewTask.job_id == job_id))
        await db.execute(
            delete(AppliedChange).where(
                AppliedChange.job_id == job_id,
                AppliedChange.review_status == "pending_review",
            )
        )
        reviewed_alt_by_index = {entry.figure_index: entry for entry in reviewed_alt_entries}
        for change_spec in auto_applied_change_specs:
            task_type = str(change_spec.get("task_type") or "review_change")
            metadata = change_spec.get("metadata") if isinstance(change_spec.get("metadata"), dict) else {}
            before = change_spec.get("before") if isinstance(change_spec.get("before"), dict) else {}
            after = change_spec.get("after") if isinstance(change_spec.get("after"), dict) else {}
            undo_payload = change_spec.get("undo_payload") if isinstance(change_spec.get("undo_payload"), dict) else {}
            if task_type == "figure_semantics":
                figure_index_raw = metadata.get("figure_index", undo_payload.get("figure_index"))
                try:
                    figure_index = int(figure_index_raw)
                except (TypeError, ValueError):
                    figure_index = -1
                entry = reviewed_alt_by_index.get(figure_index)
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
                title=str(change_spec.get("title") or "Applied recommendation"),
                detail=str(change_spec.get("detail") or "The model applied a semantic recommendation."),
                importance=str(change_spec.get("importance") or "high"),
                reviewable=True,
                metadata=metadata,
                before=before,
                after=after,
                undo_payload=undo_payload,
            )
        for task in review_tasks:
            metadata = task.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            db.add(ReviewTask(
                job_id=job_id,
                task_type=str(task.get("task_type") or "review_task"),
                title=str(task.get("title") or "Recommendation review required"),
                detail=str(task.get("detail") or ""),
                severity=str(task.get("severity") or "medium"),
                blocking=bool(task.get("blocking", True)),
                status=str(task.get("status") or "pending_review"),
                source=str(task.get("source") or "fidelity"),
                metadata_json=json.dumps(metadata),
            ))

        blocking_task_count = _blocking_task_count(review_tasks)
        await _update_step(db, job_id, "fidelity", "complete", result={
            "passed": bool(fidelity_report.get("passed", False)),
            "blocking_tasks": blocking_task_count,
            "advisory_tasks": len(review_tasks) - blocking_task_count,
        })
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

        # Done!
        final_status = (
            "complete"
            if selected_validation.compliant and bool(fidelity_report.get("passed", False))
            else "awaiting_recommendation_review"
        )
        job.status = final_status
        await db.commit()
        job_manager.emit_progress(job_id, step="review", status=final_status)
        logger.info(f"Pipeline complete for job {job_id} with status={final_status}")

    except Exception as e:
        logger.exception(f"Tagging/validation failed for job {job_id}")
        await db.rollback()
        user_error = re.sub(r"/\S*", "", str(e)).strip(": ")
        job = await db.get(Job, job_id)
        if job:
            job.status = "failed"
            job.error = user_error or "Tagging/validation failed"
            await db.commit()
        job_manager.emit_progress(
            job_id, step="error", status="failed", message=user_error,
        )
