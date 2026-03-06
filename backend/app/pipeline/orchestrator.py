"""Pipeline orchestrator: runs all steps in sequence with progress events."""

import asyncio
import json
import logging
import re
import shutil
from functools import lru_cache
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AltTextEntry, Job, JobStep, ReviewTask
from app.pipeline.alt_text import generate_alt_text
from app.pipeline.classify import classify_pdf
from app.pipeline.fidelity import assess_fidelity
from app.pipeline.ocr import run_ocr
from app.pipeline.structure import extract_structure
from app.pipeline.tagger import tag_pdf
from app.pipeline.validator import validate_pdf
from app.services.file_storage import create_job_dir, get_output_path
from app.services.job_manager import JobManager
from app.services.llm_client import LlmClient

logger = logging.getLogger(__name__)


FONT_RULE_FRAGMENT = "-7.21."
FONT_LANE_REPAIR_DICTS = "repair_font_dicts"
FONT_LANE_REPAIR_TOUNICODE = "repair_tounicode"
FONT_LANE_EMBED = "embed_fonts"
FONT_LANE_OCR_REDO = "ocr_redo"
FONT_LANE_OCR_FORCE = "ocr_force"
FONT_EMBED_RULE_MARKERS = ("-7.21.3.2-", "-7.21.4.")
FONT_UNICODE_RULE_MARKERS = ("-7.21.7-", "-7.21.8-")
FONT_DICT_REPAIR_RULE_MARKERS = ("-7.21.3.2-", "-7.21.4.2-")
FONT_SUBSET_RE = re.compile(r"^[A-Z]{6}\+.+")
FONT_NAME_RE = re.compile(r"[^A-Za-z0-9]+")
HEX_STR_RE = re.compile(r"<([0-9A-Fa-f]+)>")
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
    }
    try:
        import pikepdf

        seen_resources: set[tuple[int, int]] = set()
        seen_fonts: set[tuple[int, int]] = set()
        seen_appearances: set[tuple[int, int]] = set()

        def _obj_key(obj) -> tuple[int, int] | None:
            try:
                obj_num, gen_num = obj.objgen
                if isinstance(obj_num, int) and isinstance(gen_num, int) and obj_num > 0:
                    return obj_num, gen_num
            except Exception:
                return None
            return None

        def _resolve_dictionary(obj):
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
            if not isinstance(resources, pikepdf.Dictionary):
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
                    if subtype == pikepdf.Name("/Type0"):
                        descendants = font_dict.get("/DescendantFonts")
                        if isinstance(descendants, pikepdf.Array) and descendants:
                            cid_font = _resolve_dictionary(descendants[0])
                            if isinstance(cid_font, pikepdf.Dictionary):
                                descriptor = _resolve_dictionary(cid_font.get("/FontDescriptor"))
                    else:
                        descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))

                    if not _has_embedded_font(descriptor):
                        features["unembedded_fonts"] = int(features["unembedded_fonts"]) + 1

            xobjects = _resolve_dictionary(resources.get("/XObject"))
            if not isinstance(xobjects, pikepdf.Dictionary):
                return
            for _, xobject in xobjects.items():
                xobject_dict = _resolve_dictionary(xobject)
                if xobject_dict is None:
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
            if not isinstance(appearance_obj, pikepdf.Dictionary):
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
            features["has_forms"] = bool(pdf.Root.get("/AcroForm"))
            links = 0
            for page in pdf.pages:
                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for annot in annots:
                        try:
                            if annot.get("/Subtype") == pikepdf.Name("/Link"):
                                links += 1
                            _walk_appearance_object(annot.get("/AP"))
                        except Exception:
                            continue
                _walk_resources(_resolve_dictionary(page.get("/Resources")))
            features["link_annots"] = links
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
    if (
        classification_value == "digital"
        and not settings.font_remediation_allow_ocr_on_digital
    ):
        reasons.append("digital document")
    if bool(pdf_features.get("has_forms", False)):
        reasons.append("fillable forms present")
    page_count = int(pdf_features.get("pages", 0))
    if page_count > settings.font_remediation_ocr_max_pages:
        reasons.append(f"page count {page_count} > limit {settings.font_remediation_ocr_max_pages}")
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


def _is_better_validation(candidate, current) -> bool:
    """Compare two validation outcomes and return whether candidate is better."""
    candidate_errors = _error_count(candidate)
    current_errors = _error_count(current)
    candidate_warnings = _warning_count(candidate)
    current_warnings = _warning_count(current)

    if candidate.compliant and not current.compliant:
        return True
    if candidate_errors != current_errors:
        return candidate_errors < current_errors
    if candidate_warnings != current_warnings:
        return candidate_warnings < current_warnings
    return len(candidate.violations) < len(current.violations)


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


def _base_font_name(font_dict) -> str:
    base_font = font_dict.get("/BaseFont")
    if base_font:
        return _strip_subset_prefix(str(base_font))
    return ""


def _simple_font_auto_unicode_policy(font_dict) -> str:
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
    return "blocked"


def _inspect_unicode_repair_gate(pdf_path: Path, violations=None) -> dict[str, object]:
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
    try:
        import pikepdf

        seen_resources: set[tuple[int, int]] = set()
        seen_fonts: set[tuple[int, int]] = set()
        seen_appearances: set[tuple[int, int]] = set()

        def _obj_key(obj) -> tuple[int, int] | None:
            try:
                obj_num, gen_num = obj.objgen
                if isinstance(obj_num, int) and isinstance(gen_num, int) and obj_num > 0:
                    return obj_num, gen_num
            except Exception:
                return None
            return None

        def _resolve_dictionary(obj):
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

        def _note_blocked_example(font_dict) -> None:
            examples = profile["blocked_examples"]
            if not isinstance(examples, list) or len(examples) >= 5:
                return
            name = _base_font_name(font_dict) or "(unnamed)"
            if name not in examples:
                examples.append(name)

        def _walk_resources(resources) -> None:
            if not isinstance(resources, pikepdf.Dictionary):
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
                        has_tounicode = pikepdf.Name("/ToUnicode") in font_dict
                        if not has_tounicode or has_invalid_unicode_rules:
                            profile["safe_type0_candidates"] = int(profile["safe_type0_candidates"]) + 1
                        continue

                    if subtype not in (
                        pikepdf.Name("/Type1"),
                        pikepdf.Name("/MMType1"),
                        pikepdf.Name("/TrueType"),
                    ):
                        continue

                    policy = _simple_font_auto_unicode_policy(font_dict)
                    has_tounicode = pikepdf.Name("/ToUnicode") in font_dict
                    if policy in {"explicit", "standard14"}:
                        if not has_tounicode or has_invalid_unicode_rules:
                            profile["safe_simple_candidates"] = int(profile["safe_simple_candidates"]) + 1
                    else:
                        profile["blocked_simple_fonts"] = int(profile["blocked_simple_fonts"]) + 1
                        _note_blocked_example(font_dict)

            xobjects = _resolve_dictionary(resources.get("/XObject"))
            if not isinstance(xobjects, pikepdf.Dictionary):
                return
            for _, xobject in xobjects.items():
                xobject_dict = _resolve_dictionary(xobject)
                if xobject_dict is None:
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
            if not isinstance(appearance_obj, pikepdf.Dictionary):
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
                _walk_resources(_resolve_dictionary(page.get("/Resources")))
                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for annot in annots:
                        _walk_appearance_object(annot.get("/AP"))
    except Exception as exc:
        profile["reason"] = f"inspection_failed: {exc}"
        return profile

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

    def _obj_key(obj) -> tuple[int, int] | None:
        try:
            obj_num, gen_num = obj.objgen
            if isinstance(obj_num, int) and isinstance(gen_num, int) and obj_num > 0:
                return obj_num, gen_num
        except Exception:
            return None
        return None

    def _resolve_dictionary(obj):
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
        if cid_font.get("/Subtype") != pikepdf.Name("/CIDFontType2"):
            if obj_key:
                seen_fonts.add(obj_key)
            return

        descriptor = _resolve_dictionary(cid_font.get("/FontDescriptor"))
        if _has_embedded_font(descriptor) and pikepdf.Name("/CIDToGIDMap") not in cid_font:
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
        if not isinstance(resources, pikepdf.Dictionary):
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
            if xobject_dict is None:
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
        if not isinstance(appearance_obj, pikepdf.Dictionary):
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
            for page in pdf.pages:
                _walk_resources(_resolve_dictionary(page.get("/Resources")))
                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for annot in annots:
                        _walk_appearance_object(annot.get("/AP"))

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
    if (
        not mapping
        and subtype in (pikepdf.Name("/Type1"), pikepdf.Name("/MMType1"))
        and _simple_font_auto_unicode_policy(font_dict) == "standard14"
    ):
        mapping.update(_named_simple_encoding_map("/StandardEncoding"))

    return {code: text for code, text in mapping.items() if 0 <= code <= 0xFF and _is_valid_unicode_text(text)}


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
            span = max(0, end - start + 1)
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

        if len(seed_text) != 1:
            invalid_entries += max(0, end - start + 1)
            continue
        seed_cp = ord(seed_text)
        span = max(0, end - start + 1)
        for offset in range(span):
            cp = seed_cp + offset
            if cp > 0x10FFFF or cp in (0x0000, 0xFFFE, 0xFEFF) or (0xD800 <= cp <= 0xDFFF):
                invalid_entries += 1
                continue
            mapping[start + offset] = chr(cp)

    return mapping, invalid_entries


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
    }
    seen_fonts: set[tuple[int, int]] = set()
    seen_resources: set[tuple[int, int]] = set()
    seen_appearances: set[tuple[int, int]] = set()
    used_simple_codes: dict[tuple[int, int], set[int]] = {}
    seen_content_streams: set[tuple[int, int]] = set()

    def _obj_key(obj) -> tuple[int, int] | None:
        try:
            obj_num, gen_num = obj.objgen
            if isinstance(obj_num, int) and isinstance(gen_num, int) and obj_num > 0:
                return obj_num, gen_num
        except Exception:
            return None
        return None

    def _resolve_object(obj):
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

    def _font_stream_bytes(descriptor) -> bytes | None:
        if not isinstance(descriptor, pikepdf.Dictionary):
            return None
        for key in ("/FontFile", "/FontFile2", "/FontFile3"):
            stream_obj = _resolve_object(descriptor.get(key))
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

        stream_obj = _resolve_object(cid_to_gid)
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

    def _raw_text_bytes(op: str, operands) -> bytes:
        if op == "Tj" and operands:
            try:
                return bytes(operands[0])
            except Exception:
                return b""
        if op == "TJ" and operands:
            parts: list[bytes] = []
            arr = operands[0]
            if isinstance(arr, pikepdf.Array):
                for item in arr:
                    try:
                        parts.append(bytes(item))
                    except Exception:
                        continue
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

    def _collect_used_simple_font_codes(content_obj, resources) -> None:
        if resources is None:
            return
        resolved_obj = _resolve_object(content_obj)
        if resolved_obj is None:
            return
        content_key = _obj_key(resolved_obj)
        if content_key and content_key in seen_content_streams:
            return
        if content_key:
            seen_content_streams.add(content_key)

        fonts = _resolve_object(resources.get("/Font"))
        xobjects = _resolve_object(resources.get("/XObject"))
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
                current_font = _resolve_object(font_ref)
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
                xobject = _resolve_object(xobjects.get(operands[0]))
                if not isinstance(xobject, pikepdf.Dictionary):
                    continue
                if xobject.get("/Subtype") != pikepdf.Name("/Form"):
                    continue
                child_resources = _resolve_object(xobject.get("/Resources")) or resources
                _collect_used_simple_font_codes(xobject, child_resources)

    def _rebuild_type0_tounicode(pdf, type0_font, cid_font) -> tuple[bool, int, int, int, int]:
        descriptor = _resolve_object(cid_font.get("/FontDescriptor"))
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

        existing_stream = _resolve_object(type0_font.get("/ToUnicode"))
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
        if _simple_font_auto_unicode_policy(font_dict) == "blocked":
            return False, 0, 0, 0, 0, 0
        font_key = _obj_key(font_dict)
        used_codes = used_simple_codes.get(font_key or (-1, -1), set())
        existing_stream = _resolve_object(font_dict.get("/ToUnicode"))
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

        descriptor = _resolve_object(font_dict.get("/FontDescriptor"))
        font_bytes = _font_stream_bytes(descriptor)
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
        if not isinstance(resources, pikepdf.Dictionary):
            return
        resources_key = _obj_key(resources)
        if resources_key and resources_key in seen_resources:
            return
        if resources_key:
            seen_resources.add(resources_key)

        fonts = _resolve_object(resources.get("/Font"))
        if isinstance(fonts, pikepdf.Dictionary):
            for _, font_obj in fonts.items():
                type0_font = _resolve_object(font_obj)
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
                    cid_font = _resolve_object(descendants[0])
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

        xobjects = _resolve_object(resources.get("/XObject"))
        if not isinstance(xobjects, pikepdf.Dictionary):
            return
        for _, xobject in xobjects.items():
            xobject_dict = _resolve_object(xobject)
            if xobject_dict is None:
                continue
            try:
                subtype = xobject_dict.get("/Subtype")
            except Exception:
                continue
            if subtype != pikepdf.Name("/Form"):
                continue
            child_resources = _resolve_object(xobject_dict.get("/Resources"))
            _walk_resources(pdf, child_resources)

    def _walk_appearance_object(pdf, obj) -> None:
        appearance_obj = _resolve_object(obj)
        if not isinstance(appearance_obj, pikepdf.Dictionary):
            return
        appearance_key = _obj_key(appearance_obj)
        if appearance_key and appearance_key in seen_appearances:
            return
        if appearance_key:
            seen_appearances.add(appearance_key)

        _walk_resources(pdf, _resolve_object(appearance_obj.get("/Resources")))
        for key in ("/N", "/R", "/D"):
            child = appearance_obj.get(key)
            if child is not None:
                _walk_appearance_object(pdf, child)

    try:
        with pikepdf.open(str(input_path)) as pdf:
            for page in pdf.pages:
                _collect_used_simple_font_codes(page, _resolve_object(page.get("/Resources")))
                _walk_resources(pdf, _resolve_object(page.get("/Resources")))
                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for annot in annots:
                        _collect_used_simple_font_codes(annot.get("/AP"), _resolve_object(annot.get("/Resources")))
                        _walk_appearance_object(pdf, annot.get("/AP"))

            if stats["maps_rebuilt"] <= 0:
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
                f"stale_removed={stats['stale_entries_removed']})"
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

    def _obj_key(obj) -> tuple[int, int] | None:
        try:
            obj_num, gen_num = obj.objgen
            if isinstance(obj_num, int) and isinstance(gen_num, int) and obj_num > 0:
                return obj_num, gen_num
        except Exception:
            return None
        return None

    def _resolve_dictionary(obj):
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

    def _embed_descriptor_font(descriptor, font_name: str) -> bool:
        if not isinstance(descriptor, pikepdf.Dictionary):
            stats["fonts_unsupported"] += 1
            return False
        if _has_embedded_font(descriptor):
            stats["fonts_already_embedded"] += 1
            return False

        font_bytes, matched_name = _system_font_program(font_name)
        if not font_bytes:
            stats["fonts_missing"] += 1
            return False

        stream = pdf.make_stream(font_bytes)
        stream[pikepdf.Name("/Length1")] = len(font_bytes)
        descriptor[pikepdf.Name("/FontFile2")] = stream
        if pikepdf.Name("/FontName") not in descriptor and font_name:
            descriptor[pikepdf.Name("/FontName")] = pikepdf.Name(f"/{_strip_subset_prefix(font_name)}")
        stats["fonts_embedded"] += 1
        stats["fonts_touched"] += 1
        logger.info(f"Embedded local font program for {font_name} using {matched_name}")
        return True

    def _walk_resources(resources) -> None:
        if not isinstance(resources, pikepdf.Dictionary):
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
                    _embed_descriptor_font(descriptor, font_name)
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
                changed = _embed_descriptor_font(descriptor, font_name)
                if changed and pikepdf.Name("/CIDToGIDMap") not in cid_font:
                    cid_font[pikepdf.Name("/CIDToGIDMap")] = pikepdf.Name("/Identity")
                    stats["cidtogid_added"] += 1

        xobjects = _resolve_dictionary(resources.get("/XObject"))
        if not isinstance(xobjects, pikepdf.Dictionary):
            return
        for _, xobject in xobjects.items():
            xobject_dict = _resolve_dictionary(xobject)
            if xobject_dict is None:
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
        if not isinstance(appearance_obj, pikepdf.Dictionary):
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
            for page in pdf.pages:
                _walk_resources(_resolve_dictionary(page.get("/Resources")))
                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for annot in annots:
                        _walk_appearance_object(annot.get("/AP"))

            if stats["fonts_embedded"] <= 0:
                return False, "No embeddable local TrueType fonts were found", stats

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


async def _rewrite_pdf_with_ghostscript_embed(input_path: Path, output_path: Path) -> tuple[bool, str]:
    """Rewrite PDF through Ghostscript with aggressive font embedding options."""
    gs = shutil.which("gs")
    if not gs:
        return False, "Ghostscript not found in PATH"

    proc = await asyncio.create_subprocess_exec(
        gs,
        "-q",
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-sDEVICE=pdfwrite",
        "-dEmbedAllFonts=true",
        "-dSubsetFonts=true",
        "-o",
        str(output_path),
        str(input_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
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
    preprocess_details: dict[str, int] = {}
    preprocess_skipped = False
    requires_retag = True

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
        rewritten = job_dir / "fontfix_embedded_local.pdf"
        ok, message, stats = await _embed_system_fonts(tagged_pdf, rewritten)
        preprocess_message = message
        preprocess_details = stats
        if ok:
            remediation_input = rewritten
            requires_retag = False
        else:
            rewritten = job_dir / "fontfix_embedded_gs.pdf"
            ok, message = await _rewrite_pdf_with_ghostscript_embed(working_pdf, rewritten)
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
            structure_json=structure_json,
            alt_texts=reviewed_alts,
            original_filename=job.original_filename or "",
        )
        validation_target = tagging_result.output_path
    else:
        remediation_output = remediation_input
        tagging_result = current_tagging_result
        validation_target = remediation_input

    validation = await validate_pdf(
        pdf_path=validation_target,
        verapdf_path=settings.verapdf_path,
        flavour=settings.verapdf_flavour,
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
                ocr_result = await run_ocr(input_path, ocr_output, settings.ocr_language)

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

            job.structure_json = json.dumps(structure.document_json)
            await _update_step(db, job_id, "structure", "complete", result={
                "page_count": structure.page_count,
                "headings": structure.headings_count,
                "tables": structure.tables_count,
                "figures": structure.figures_count,
            })
            job_manager.emit_progress(
                job_id, step="structure", status="complete",
                result={"figures_found": structure.figures_count},
            )

            # ── Step 4: Alt Text Generation ──
            current_step = "alt_text"
            if structure.figures:
                await _update_step(db, job_id, "alt_text", "running")
                job_manager.emit_progress(job_id, step="alt_text", status="running")

                llm_client = LlmClient(
                    base_url=settings.llm_base_url,
                    api_key=settings.llm_api_key,
                    model=settings.llm_model,
                    timeout=settings.llm_timeout,
                )

                try:
                    alt_texts = await generate_alt_text(structure.figures, llm_client)
                finally:
                    await llm_client.close()

                # Save alt text entries to database
                approved_count = 0
                rejected_count = 0
                pending_count = 0
                for alt in alt_texts:
                    if alt.figure_index < 0 or alt.figure_index >= len(structure.figures):
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
                            status = "pending_review"
                        else:
                            status = "approved"

                    if status == "approved":
                        approved_count += 1
                    elif status == "rejected":
                        rejected_count += 1
                    else:
                        pending_count += 1

                    db.add(AltTextEntry(
                        job_id=job_id,
                        figure_index=alt.figure_index,
                        image_path=str(fig.path),
                        generated_text=generated_text or None,
                        status=status,
                    ))
                await db.commit()

                await _update_step(db, job_id, "alt_text", "complete", result={
                    "count": len(alt_texts),
                    "approved": approved_count,
                    "rejected": rejected_count,
                    "pending_review": pending_count,
                    "auto_approve_enabled": settings.auto_approve_generated_alt_text,
                })
                job_manager.emit_progress(
                    job_id, step="alt_text", status="complete",
                    result={
                        "count": len(alt_texts),
                        "approved": approved_count,
                        "rejected": rejected_count,
                        "pending_review": pending_count,
                        "auto_approve_enabled": settings.auto_approve_generated_alt_text,
                    },
                )

                if pending_count > 0:
                    # Pause for review when generation failed or produced undecidable output.
                    job.status = "awaiting_review"
                    await db.commit()
                    job_manager.emit_progress(job_id, step="review", status="awaiting_review")
                    return  # Pipeline resumes after user approval

                # All generated alt entries are actionable (approved/rejected), continue directly.
                await run_tagging_and_validation(
                    job_id, db, settings, job_manager, working_pdf, structure.document_json
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
        })
        job_manager.emit_progress(job_id, step="tagging", status="complete")

        # ── Step 6: Validation ──
        await _update_step(db, job_id, "validation", "running")
        job_manager.emit_progress(job_id, step="validation", status="running")

        validation = await validate_pdf(
            pdf_path=tagging_result.output_path,
            verapdf_path=settings.verapdf_path,
            flavour=settings.verapdf_flavour,
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

        if not validation.compliant and _has_font_errors(validation.violations):
            unicode_gate = None
            if any(
                v.severity == "error"
                and any(marker in str(v.rule_id) for marker in FONT_UNICODE_RULE_MARKERS)
                for v in validation.violations
            ):
                unicode_gate = _inspect_unicode_repair_gate(
                    Path(job.output_path or tagging_result.output_path),
                    validation.violations,
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
                            structure_json=structure_json,
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
                                if isinstance(attempt["output_path"], Path):
                                    best_tagged_pdf = attempt["output_path"]
                                else:
                                    best_tagged_pdf = Path(str(attempt["output_path"]))
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
                        font_remediation["selected_lane"] = selected_lanes[-1]
                        font_remediation["selected_lanes"] = selected_lanes
                        font_remediation["applied"] = True
                except Exception as exc:
                    logger.exception(f"Font remediation lane evaluation failed for job {job_id}")
                    font_remediation["error"] = str(exc)

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

        validation_payload = {
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
            "remediation": {
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
            },
            "tagging": {
                "headings_tagged": selected_tagging_result.headings_tagged,
                "figures_tagged": selected_tagging_result.figures_tagged,
                "decorative_figures_artifacted": selected_tagging_result.decorative_figures_artifacted,
                "tables_tagged": selected_tagging_result.tables_tagged,
                "lists_tagged": selected_tagging_result.lists_tagged,
                "links_tagged": selected_tagging_result.links_tagged,
                "bookmarks_added": selected_tagging_result.bookmarks_added,
                "title_set": selected_tagging_result.title_set,
                "lang_set": selected_tagging_result.lang_set,
            },
            "claims": {
                "automated_validation_only": True,
                "requires_manual_check_for_reading_experience": True,
            },
        }

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
            tagging_metrics=validation_payload["tagging"],
            classification=job.classification,
        )

        validation_payload["fidelity"] = fidelity_report
        job.validation_json = json.dumps(validation_payload)
        job.fidelity_json = json.dumps(fidelity_report)

        await db.execute(delete(ReviewTask).where(ReviewTask.job_id == job_id))
        for task in review_tasks:
            db.add(ReviewTask(
                job_id=job_id,
                task_type=str(task.get("task_type") or "manual_review"),
                title=str(task.get("title") or "Manual review required"),
                detail=str(task.get("detail") or ""),
                severity=str(task.get("severity") or "medium"),
                blocking=bool(task.get("blocking", True)),
                status=str(task.get("status") or "pending_review"),
                source=str(task.get("source") or "fidelity"),
                metadata_json=json.dumps(task.get("metadata", {})),
            ))

        blocking_task_count = len([task for task in review_tasks if bool(task.get("blocking"))])
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
            else "needs_manual_review"
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
