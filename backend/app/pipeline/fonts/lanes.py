"""Select and execute font remediation lanes without changing release policy."""

from pathlib import Path

from app.config import Settings
from app.models import Job
from app.pipeline.fonts.diagnostics import _inspect_font_diagnostics
from app.pipeline.fonts.embedding import _embed_system_fonts, _rewrite_pdf_with_ghostscript_embed
from app.pipeline.fonts.repair import _repair_pdf_font_dicts, _sync_pdf_cid_cff_widths
from app.pipeline.fonts.rules import (
    FONT_DICT_REPAIR_RULE_MARKERS,
    FONT_EMBED_RULE_MARKERS,
    FONT_LANE_EMBED,
    FONT_LANE_OCR_FORCE,
    FONT_LANE_OCR_REDO,
    FONT_LANE_REPAIR_DICTS,
    FONT_LANE_REPAIR_TOUNICODE,
    FONT_RULE_FRAGMENT,
    FONT_UNICODE_RULE_MARKERS,
)
from app.pipeline.fonts.tounicode import _repair_pdf_tounicode
from app.pipeline.ocr import run_ocr
from app.pipeline.structure import extract_structure
from app.pipeline.tagger import tag_pdf
from app.pipeline.validator import validate_pdf
from app.services.file_storage import create_job_dir, get_output_path


def _join_messages(a: str | None, b: str | None) -> str | None:
    """Join two optional message strings with ' | ', dropping None/empty values."""
    if a and b:
        return f"{a} | {b}"
    return a or b


def _has_font_errors(violations) -> bool:
    return any(v.severity == "error" and FONT_RULE_FRAGMENT in str(v.rule_id) for v in violations)


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
        any(marker in rule_id for marker in FONT_EMBED_RULE_MARKERS) for rule_id in error_rule_ids
    )
    has_unicode_rules = any(
        any(marker in rule_id for marker in FONT_UNICODE_RULE_MARKERS) for rule_id in error_rule_ids
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
            reason = (
                str(unicode_gate.get("reason", "")).strip() or "no deterministic font candidates"
            )
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
            ok, message = await _rewrite_pdf_with_ghostscript_embed(
                working_pdf, rewritten, timeout_seconds=settings.subprocess_timeout_ghostscript
            )
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
                ok, message = await _rewrite_pdf_with_ghostscript_embed(
                    working_pdf, rewritten, timeout_seconds=settings.subprocess_timeout_ghostscript
                )
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
            language=job.ocr_language or settings.ocr_language,
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
                    original_elements = (
                        structure_json.get("elements", [])
                        if isinstance(structure_json, dict)
                        else []
                    )
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
                        entry
                        for entry in reviewed_alts
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
        ok, post_message, post_stats = await _repair_pdf_font_dicts(
            validation_target, post_ocr_repaired
        )
        if ok:
            validation_target = post_ocr_repaired
            remediation_output = post_ocr_repaired
            preprocess_details["post_ocr_font_dicts"] = post_stats
            preprocess_message = _join_messages(preprocess_message, post_message)

        post_ocr_widths = job_dir / f"{lane}_post_width_sync.pdf"
        ok, width_message, width_stats = await _sync_pdf_cid_cff_widths(
            validation_target, post_ocr_widths
        )
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
        "ocr_message": preprocess_message
        if lane in (FONT_LANE_OCR_REDO, FONT_LANE_OCR_FORCE)
        else "",
        "message": preprocess_message,
        "details": preprocess_details,
        "requires_retag": requires_retag,
        "preprocessed_path": str(remediation_input),
        "structure_json": effective_structure_json,
        "output_path": remediation_output,
        "tagging_result": tagging_result,
        "validation": validation,
    }
