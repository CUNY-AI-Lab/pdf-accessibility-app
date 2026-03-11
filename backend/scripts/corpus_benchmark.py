#!/usr/bin/env python3
"""Run structure/tagging/validation benchmarks across a local PDF corpus."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pikepdf
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.review import keep_applied_change
from app.config import get_settings
from app.models import AppliedChange, Base, Job, JobStep, ReviewTask
from app.pipeline.classify import classify_pdf
from app.pipeline.ocr import run_ocr
from app.pipeline.orchestrator import run_pipeline
from app.pipeline.structure import extract_structure
from app.pipeline.tagger import tag_pdf
from app.pipeline.validator import validate_pdf
from app.services.file_storage import cleanup_job_files
from app.services.job_manager import JobManager
from app.services.llm_client import track_llm_usage

ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT_DIR / "data" / "benchmarks"
DEFAULT_SCAN_ROOTS = [
    Path("/Users/stephenzweibel/Downloads"),
    Path("/Users/stephenzweibel/Documents"),
    Path("/Users/stephenzweibel/Desktop"),
]
EXCLUDE_PARTS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    ".trash",
    "Library",
}
EXCLUDE_NAME_SUFFIXES = (
    "_tagged.pdf",
    "_fontfix_tagged.pdf",
    "_fontfix_ocred.pdf",
    "_fontfix_repair_dicts.pdf",
    "_fontfix_repair_tounicode.pdf",
    ".gsfix.pdf",
    ".repaired.pdf",
)
EXCLUDE_NAME_EXACT = {
    "repaired_input.pdf",
}
FONT_LANE_EMBED = "embed_fonts"
FONT_LANE_REPAIR_DICTS = "repair_font_dicts"
FONT_LANE_REPAIR_TOUNICODE = "repair_tounicode"
FONT_LANE_OCR_REDO = "ocr_redo"
FONT_LANE_OCR_FORCE = "ocr_force"
FONT_EMBED_RULE_MARKERS = ("-7.21.3.2-", "-7.21.4.")
FONT_UNICODE_RULE_MARKERS = ("-7.21.7-", "-7.21.8-")
FONT_DICT_REPAIR_RULE_MARKERS = ("-7.21.3.2-", "-7.21.4.2-")
FONT_SUBSET_RE = re.compile(r"^[A-Z]{6}\+.+")
HEX_STR_RE = re.compile(r"<([0-9A-Fa-f]+)>")
PIPELINE_STEPS = ("classify", "ocr", "structure", "alt_text", "tagging", "validation", "fidelity")


@dataclass
class DocMetrics:
    source_path: str
    file_size_bytes: int
    pages: int
    link_annots_in_source: int
    source_has_forms: bool
    classification: str
    structure_secs: float
    tagging_secs: float
    validation_secs: float
    total_secs: float
    llm_requests: int
    llm_prompt_tokens: int
    llm_completion_tokens: int
    llm_total_tokens: int
    llm_cost_usd: float
    structure_ok: bool
    tag_ok: bool
    validation_ok: bool
    error: str
    baseline_validator: str
    baseline_compliant: bool
    baseline_validation_errors: int
    baseline_validation_warnings: int
    validator: str
    final_status: str
    compliant: bool
    fidelity_passed: bool
    validation_errors: int
    validation_warnings: int
    validation_errors_reduced: int
    validation_warnings_reduced: int
    review_tasks_total: int
    blocking_review_tasks: int
    blocking_validation_tasks: int
    blocking_fidelity_tasks: int
    advisory_review_tasks: int
    font_lane_attempted: bool
    font_lane_applied: bool
    font_lane_first_errors: int
    font_lane_first_warnings: int
    font_lane_second_errors: int
    font_lane_second_warnings: int
    elements_total: int
    elements_headings: int
    elements_figures: int
    elements_tables: int
    elements_list_items: int
    elements_toc_captions: int
    elements_toc_items: int
    tags_total: int
    struct_elems_created: int
    headings_tagged: int
    figures_tagged: int
    decorative_figures_artifacted: int
    tables_tagged: int
    lists_tagged: int
    links_tagged: int
    bookmarks_added: int
    toc_llm_assist_attempted: bool
    toc_llm_assist_applied: bool
    toc_llm_groups_considered: int
    toc_llm_groups_applied: int
    heading_coverage: float
    figure_coverage: float
    table_coverage: float
    list_coverage: float
    link_coverage: float


def _should_skip(path: Path) -> bool:
    lowered = [part.lower() for part in path.parts]
    if any(ex.lower() in lowered for ex in EXCLUDE_PARTS):
        return True

    name = path.name.lower()
    if name.startswith("accessible_"):
        return True
    if name in EXCLUDE_NAME_EXACT:
        return True
    if any(name.endswith(pattern) for pattern in EXCLUDE_NAME_SUFFIXES):
        return True

    return False


def discover_pdfs(
    *,
    exclude_wac: bool = False,
    scan_roots: list[Path] | None = None,
) -> list[Path]:
    roots = scan_roots if scan_roots is not None else list(DEFAULT_SCAN_ROOTS)
    found: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.lower() == ".pdf" and not _should_skip(root):
                found[str(root.resolve())] = root
            continue
        for path in root.rglob("*.pdf"):
            if _should_skip(path):
                continue
            key = str(path.resolve())
            found[key] = path

    wac = ROOT_DIR / "test_wac.pdf"
    if scan_roots is None and wac.exists() and not exclude_wac:
        found[str(wac.resolve())] = wac

    # most-recent first
    ordered = sorted(
        found.values(),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    return ordered


def _safe_ratio(num: int, denom: int) -> float:
    if denom <= 0:
        return 1.0
    return num / denom


def _violation_count(validation, severity: str) -> int:
    total = 0
    for violation in validation.violations:
        if severity == "error" and violation.severity != "error":
            continue
        if severity != "error" and violation.severity == "error":
            continue
        count = violation.count if isinstance(violation.count, int) and violation.count > 0 else 1
        total += count
    return total


def _font_only_errors(validation) -> bool:
    errors = [v for v in validation.violations if v.severity == "error"]
    if not errors:
        return False
    return all("-7.21." in str(v.rule_id) for v in errors)


def _has_font_errors(validation) -> bool:
    return any(v.severity == "error" and "-7.21." in str(v.rule_id) for v in validation.violations)


def _ocr_lane_skip_reasons(
    classification: str,
    page_count: int,
    link_annots: int,
    has_forms: bool,
    settings,
) -> list[str]:
    reasons: list[str] = []
    if classification == "digital" and not settings.font_remediation_allow_ocr_on_digital:
        reasons.append("digital document")
    if has_forms:
        reasons.append("fillable forms present")
    if link_annots > 0:
        reasons.append("link annotations present")
    if page_count > settings.font_remediation_ocr_max_pages:
        reasons.append(f"page count {page_count} > limit {settings.font_remediation_ocr_max_pages}")
    return reasons


def _font_remediation_lanes(
    validation,
    classification: str,
    page_count: int,
    link_annots: int,
    has_forms: bool,
    settings,
) -> list[str]:
    error_rule_ids = [str(v.rule_id) for v in validation.violations if v.severity == "error"]
    has_unicode_rules = any(
        any(marker in rule_id for marker in FONT_UNICODE_RULE_MARKERS) for rule_id in error_rule_ids
    )
    has_dict_repair_rules = any(
        any(marker in rule_id for marker in FONT_DICT_REPAIR_RULE_MARKERS)
        for rule_id in error_rule_ids
    )

    lanes: list[str] = []
    if has_dict_repair_rules:
        lanes.append(FONT_LANE_REPAIR_DICTS)
    if has_unicode_rules:
        lanes.append(FONT_LANE_REPAIR_TOUNICODE)
    lanes.append(FONT_LANE_EMBED)
    if has_unicode_rules:
        ocr_skip_reasons = _ocr_lane_skip_reasons(
            classification,
            page_count,
            link_annots,
            has_forms,
            settings,
        )
        if not ocr_skip_reasons:
            lanes.append(FONT_LANE_OCR_REDO)
            if settings.font_remediation_enable_force_ocr:
                lanes.append(FONT_LANE_OCR_FORCE)

    return lanes


def _is_better_validation(candidate, current) -> bool:
    candidate_errors = _violation_count(candidate, "error")
    current_errors = _violation_count(current, "error")
    candidate_warnings = _violation_count(candidate, "warning")
    current_warnings = _violation_count(current, "warning")

    if candidate.compliant and not current.compliant:
        return True
    if candidate_errors != current_errors:
        return candidate_errors < current_errors
    if candidate_warnings != current_warnings:
        return candidate_warnings < current_warnings
    return len(candidate.violations) < len(current.violations)


def _tagging_regressions(candidate, current) -> list[str]:
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


def _repair_pdf_font_dicts(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, dict[str, int]]:
    stats = {
        "fonts_touched": 0,
        "cidtogid_added": 0,
        "cidset_removed": 0,
    }
    seen_fonts: set[tuple[int, int]] = set()
    seen_resources: set[tuple[int, int]] = set()

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

    try:
        with pikepdf.open(str(input_path)) as pdf:
            for page in pdf.pages:
                _walk_resources(_resolve_dictionary(page.get("/Resources")))

            if stats["fonts_touched"] <= 0:
                return False, stats
            pdf.save(str(output_path))
        return True, stats
    except Exception:
        return False, stats


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
    if not data or len(data) % 2 != 0:
        return None
    try:
        text = data.decode("utf-16-be")
    except UnicodeDecodeError:
        return None
    return text if _is_valid_unicode_text(text) else None


def _parse_tounicode_map(stream_obj) -> dict[int, str]:
    if stream_obj is None:
        return {}
    try:
        raw = bytes(stream_obj.read_bytes())
    except Exception:
        return {}
    try:
        text = raw.decode("latin-1")
    except Exception:
        return {}

    mapping: dict[int, str] = {}
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
            continue

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
            continue
        seed_cp = ord(seed_text)
        span = max(0, end - start + 1)
        for offset in range(span):
            cp = seed_cp + offset
            if cp > 0x10FFFF or cp in (0x0000, 0xFFFE, 0xFEFF) or (0xD800 <= cp <= 0xDFFF):
                continue
            mapping[start + offset] = chr(cp)

    return mapping


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
        chunk = entries[i : i + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for code, value in chunk:
            dest_hex = value.encode("utf-16-be").hex().upper()
            lines.append(f"<{code:0{width}X}> <{dest_hex}>")
        lines.append("endbfchar")
    lines.extend(
        [
            "endcmap",
            "CMapName currentdict /CMap defineresource pop",
            "end",
            "end",
            "",
        ]
    )
    return "\n".join(lines).encode("ascii")


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


def _repair_pdf_tounicode(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, dict[str, int]]:
    stats = {
        "fonts_touched": 0,
        "maps_rebuilt": 0,
        "mappings_generated": 0,
        "mappings_total": 0,
    }
    seen_fonts: set[tuple[int, int]] = set()
    seen_resources: set[tuple[int, int]] = set()

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
        for key in ("/FontFile2", "/FontFile3"):
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
        if cid_to_gid is None or cid_to_gid == pikepdf.Name("/Identity"):
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
            mapping[cid] = int.from_bytes(raw[cid * 2 : (cid + 1) * 2], "big")
        return mapping, False

    def _merge_tounicode_maps(
        existing: dict[int, str], generated: dict[int, str]
    ) -> dict[int, str]:
        merged = {code: text for code, text in existing.items() if _is_valid_unicode_text(text)}
        for code, text in generated.items():
            if not _is_valid_unicode_text(text):
                continue
            if code not in merged:
                merged[code] = text
        return merged

    def _rebuild_type0_tounicode(pdf, type0_font, cid_font) -> tuple[bool, int, int]:
        descriptor = _resolve_object(cid_font.get("/FontDescriptor"))
        font_bytes = _font_stream_bytes(descriptor)
        if not font_bytes:
            return False, 0, 0

        try:
            gid_to_unicode = _collect_gid_to_unicode(font_bytes)
        except Exception:
            return False, 0, 0
        if not gid_to_unicode:
            return False, 0, 0

        cid_map, is_identity = _cid_to_gid_mapping(cid_font)
        generated: dict[int, str] = {}
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
        if not generated:
            return False, 0, 0

        existing_stream = _resolve_object(type0_font.get("/ToUnicode"))
        existing_map = _parse_tounicode_map(existing_stream)
        merged_map = _merge_tounicode_maps(existing_map, generated)
        if not merged_map:
            return False, 0, 0
        if existing_map == merged_map:
            return False, len(merged_map), len(generated)

        max_code = max(merged_map.keys(), default=0)
        code_bytes = 1 if max_code <= 0xFF else (2 if max_code <= 0xFFFF else 4)
        cmap_bytes = _render_tounicode_cmap(merged_map, code_bytes)
        type0_font[pikepdf.Name("/ToUnicode")] = pdf.make_stream(cmap_bytes)
        return True, len(merged_map), len(generated)

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
                    continue
                descendants = type0_font.get("/DescendantFonts")
                if not isinstance(descendants, pikepdf.Array) or len(descendants) <= 0:
                    continue
                cid_font = _resolve_object(descendants[0])
                if not isinstance(cid_font, pikepdf.Dictionary):
                    continue
                if cid_font.get("/Subtype") != pikepdf.Name("/CIDFontType2"):
                    continue

                changed, merged_count, generated_count = _rebuild_type0_tounicode(
                    pdf, type0_font, cid_font
                )
                if changed:
                    stats["fonts_touched"] += 1
                    stats["maps_rebuilt"] += 1
                stats["mappings_generated"] += generated_count
                stats["mappings_total"] += merged_count

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

    try:
        with pikepdf.open(str(input_path)) as pdf:
            for page in pdf.pages:
                _walk_resources(pdf, _resolve_object(page.get("/Resources")))
            if stats["maps_rebuilt"] <= 0:
                return False, stats
            pdf.save(str(output_path))
        return True, stats
    except Exception:
        return False, stats


async def _rewrite_pdf_with_ghostscript_embed(input_path: Path, output_path: Path) -> bool:
    gs = shutil.which("gs")
    if not gs:
        return False

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
    await proc.communicate()
    return proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0


def _count_source_features(path: Path) -> tuple[int, int, bool]:
    try:
        with pikepdf.open(str(path)) as pdf:
            total = 0
            for page in pdf.pages:
                annots = page.get("/Annots")
                if not isinstance(annots, pikepdf.Array):
                    continue
                for annot in annots:
                    try:
                        if annot.get("/Subtype") == pikepdf.Name("/Link"):
                            total += 1
                    except Exception:
                        continue
            has_forms = bool(pdf.Root.get("/AcroForm"))
            return len(pdf.pages), total, has_forms
    except Exception:
        return -1, 0, False


def _parse_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _violation_payload_count(violation: dict) -> int:
    count = violation.get("count", 0)
    return count if isinstance(count, int) and count > 0 else 1


def _build_rule_backlog(workflow_db_path: Path) -> list[dict]:
    if not workflow_db_path.exists():
        return []

    backlog_by_rule: dict[str, dict] = {}
    with sqlite3.connect(workflow_db_path) as conn:
        rows = conn.execute(
            """
            select filename, validation_json
            from jobs
            where validation_json is not null and validation_json != ''
            """
        ).fetchall()

    for filename, raw_validation in rows:
        validation_json = _parse_json(raw_validation)
        violations = validation_json.get("violations", [])
        if not isinstance(violations, list):
            continue

        for violation in violations:
            if not isinstance(violation, dict):
                continue
            if str(violation.get("severity", "")).strip() != "error":
                continue

            rule_id = str(violation.get("rule_id", "") or "").strip()
            if not rule_id:
                continue

            count = _violation_payload_count(violation)
            entry = backlog_by_rule.setdefault(
                rule_id,
                {
                    "rule_id": rule_id,
                    "description": str(violation.get("description", "") or "").strip(),
                    "category": str(violation.get("category", "") or "").strip() or None,
                    "fix_hint": str(violation.get("fix_hint", "") or "").strip() or None,
                    "total_errors": 0,
                    "documents": {},
                },
            )
            entry["total_errors"] += count
            entry["documents"][filename] = entry["documents"].get(filename, 0) + count

    backlog: list[dict] = []
    for entry in backlog_by_rule.values():
        doc_items = sorted(
            entry["documents"].items(),
            key=lambda item: (-item[1], item[0].lower()),
        )
        backlog.append(
            {
                "rule_id": entry["rule_id"],
                "description": entry["description"],
                "category": entry["category"],
                "fix_hint": entry["fix_hint"],
                "total_errors": entry["total_errors"],
                "documents_affected": len(doc_items),
                "documents": [
                    {"filename": filename, "errors": errors} for filename, errors in doc_items
                ],
            }
        )

    backlog.sort(
        key=lambda entry: (
            -entry["total_errors"],
            -entry["documents_affected"],
            entry["rule_id"],
        )
    )
    return backlog


def _build_review_backlog(workflow_db_path: Path) -> list[dict]:
    if not workflow_db_path.exists():
        return []

    backlog_by_task: dict[tuple[str, str, bool], dict] = {}
    with sqlite3.connect(workflow_db_path) as conn:
        rows = conn.execute(
            """
            select jobs.filename, review_tasks.task_type, review_tasks.title,
                   review_tasks.severity, review_tasks.source, review_tasks.blocking
            from review_tasks
            join jobs on jobs.id = review_tasks.job_id
            """
        ).fetchall()

    for filename, task_type, title, severity, source, blocking in rows:
        key = (
            str(task_type or "").strip() or "manual_review",
            str(source or "").strip() or "fidelity",
            bool(blocking),
        )
        entry = backlog_by_task.setdefault(
            key,
            {
                "task_type": key[0],
                "title": str(title or "").strip() or "Manual review task",
                "severity": str(severity or "").strip() or "medium",
                "source": key[1],
                "blocking": key[2],
                "documents": {},
            },
        )
        entry["documents"][filename] = entry["documents"].get(filename, 0) + 1

    backlog: list[dict] = []
    for entry in backlog_by_task.values():
        doc_items = sorted(
            entry["documents"].items(),
            key=lambda item: (-item[1], item[0].lower()),
        )
        backlog.append(
            {
                "task_type": entry["task_type"],
                "title": entry["title"],
                "severity": entry["severity"],
                "source": entry["source"],
                "blocking": entry["blocking"],
                "documents_affected": len(doc_items),
                "occurrences": sum(count for _, count in doc_items),
                "documents": [
                    {"filename": filename, "occurrences": count} for filename, count in doc_items
                ],
            }
        )

    backlog.sort(
        key=lambda entry: (
            0 if entry["blocking"] else 1,
            -entry["documents_affected"],
            -entry["occurrences"],
            entry["task_type"],
        )
    )
    return backlog


def _step_duration_secs(step: JobStep | None) -> float:
    if not step or not step.started_at or not step.completed_at:
        return 0.0
    return max(0.0, (step.completed_at - step.started_at).total_seconds())


async def _init_workflow_db(db_path: Path) -> tuple:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_maker


async def _finalize_review_if_needed(
    job_id: str,
    session_maker: async_sessionmaker[AsyncSession],
    settings,
    job_manager: JobManager,
) -> None:
    while True:
        async with session_maker() as db:
            job = await db.get(Job, job_id)
            if not job:
                return
            if job.status == "processing":
                pass
            elif job.status not in {"complete", "manual_remediation"}:
                return
            change_result = await db.execute(
                select(AppliedChange)
                .where(
                    AppliedChange.job_id == job_id,
                    AppliedChange.review_status == "pending_review",
                    AppliedChange.reviewable.is_(True),
                )
                .order_by(AppliedChange.created_at.asc())
            )
            pending_changes = change_result.scalars().all()
            if pending_changes:
                for change in pending_changes:
                    await keep_applied_change(job_id=job_id, change_id=change.id, db=db)
                continue
            return

        await asyncio.sleep(0.1)


async def benchmark_one_workflow(
    pdf_path: Path,
    run_dir: Path,
    settings,
    session_maker: async_sessionmaker[AsyncSession],
    job_manager: JobManager,
) -> DocMetrics:
    started = time.perf_counter()
    pages, source_links, source_has_forms = _count_source_features(pdf_path)
    size = pdf_path.stat().st_size if pdf_path.exists() else 0

    structure_secs = 0.0
    tagging_secs = 0.0
    validation_secs = 0.0
    structure_ok = False
    tag_ok = False
    validation_ok = False
    error = ""

    baseline_validator = "unknown"
    baseline_compliant = False
    baseline_validation_errors = 0
    baseline_validation_warnings = 0
    validator = "unknown"
    final_status = "unknown"
    compliant = False
    fidelity_passed = False
    validation_errors = 0
    validation_warnings = 0
    validation_errors_reduced = 0
    validation_warnings_reduced = 0
    review_tasks_total = 0
    blocking_review_tasks = 0
    blocking_validation_tasks = 0
    blocking_fidelity_tasks = 0
    advisory_review_tasks = 0

    font_lane_attempted = False
    font_lane_applied = False
    font_lane_first_errors = 0
    font_lane_first_warnings = 0
    font_lane_second_errors = 0
    font_lane_second_warnings = 0

    classification_type = "unknown"
    elements_total = 0
    elements_headings = 0
    elements_figures = 0
    elements_tables = 0
    elements_list_items = 0
    elements_toc_captions = 0
    elements_toc_items = 0
    tags_total = 0
    struct_elems_created = 0
    headings_tagged = 0
    figures_tagged = 0
    decorative_figures_artifacted = 0
    tables_tagged = 0
    lists_tagged = 0
    links_tagged = 0
    bookmarks_added = 0
    toc_llm_assist_attempted = False
    toc_llm_assist_applied = False
    toc_llm_groups_considered = 0
    toc_llm_groups_applied = 0
    llm_requests = 0
    llm_prompt_tokens = 0
    llm_completion_tokens = 0
    llm_total_tokens = 0
    llm_cost_usd = 0.0

    job_id = str(uuid.uuid4())

    try:
        async with session_maker() as db:
            job = Job(
                id=job_id,
                filename=pdf_path.name,
                original_filename=pdf_path.name,
                status="queued",
                input_path=str(pdf_path),
                file_size_bytes=size,
            )
            db.add(job)
            for step_name in PIPELINE_STEPS:
                db.add(JobStep(job_id=job_id, step_name=step_name))
            await db.commit()

        with track_llm_usage() as llm_usage:
            await run_pipeline(job_id, session_maker, settings, job_manager)
            await _finalize_review_if_needed(job_id, session_maker, settings, job_manager)
        llm_requests = llm_usage.request_count
        llm_prompt_tokens = llm_usage.prompt_tokens
        llm_completion_tokens = llm_usage.completion_tokens
        llm_total_tokens = llm_usage.total_tokens
        llm_cost_usd = llm_usage.cost_usd

        async with session_maker() as db:
            job = await db.get(Job, job_id)
            if not job:
                raise RuntimeError("Workflow benchmark job not found after execution")

            if job.error:
                error = re.sub(r"\s+", " ", str(job.error)).strip()

            classification_type = (job.classification or "unknown").strip() or "unknown"
            structure_json = _parse_json(job.structure_json)
            validation_json = _parse_json(job.validation_json)
            fidelity_json = _parse_json(job.fidelity_json)
            final_status = str(job.status or "unknown")

            elements = structure_json.get("elements")
            if isinstance(elements, list):
                elements_total = len(elements)
                elements_headings = sum(
                    1 for e in elements if isinstance(e, dict) and e.get("type") == "heading"
                )
                elements_figures = sum(
                    1 for e in elements if isinstance(e, dict) and e.get("type") == "figure"
                )
                elements_tables = sum(
                    1 for e in elements if isinstance(e, dict) and e.get("type") == "table"
                )
                elements_list_items = sum(
                    1 for e in elements if isinstance(e, dict) and e.get("type") == "list_item"
                )
                elements_toc_captions = sum(
                    1 for e in elements if isinstance(e, dict) and e.get("type") == "toc_caption"
                )
                elements_toc_items = sum(
                    1
                    for e in elements
                    if isinstance(e, dict) and e.get("type") in {"toc_item", "toc_item_table"}
                )

            steps_result = await db.execute(select(JobStep).where(JobStep.job_id == job_id))
            steps = {step.step_name: step for step in steps_result.scalars().all()}
            structure_step = steps.get("structure")
            tagging_step = steps.get("tagging")
            validation_step = steps.get("validation")

            structure_ok = bool(structure_step and structure_step.status == "complete")
            tag_ok = bool(tagging_step and tagging_step.status == "complete")
            validation_ok = bool(validation_step and validation_step.status == "complete")

            structure_secs = _step_duration_secs(structure_step)
            tagging_secs = _step_duration_secs(tagging_step)
            validation_secs = _step_duration_secs(validation_step)

            structure_metrics = (
                _parse_json(structure_step.result_json)
                if structure_step and structure_step.result_json
                else {}
            )
            if isinstance(structure_metrics, dict):
                toc_assist = structure_metrics.get("toc_llm_assist", {})
                if isinstance(toc_assist, dict):
                    toc_llm_assist_attempted = bool(toc_assist.get("attempted", False))
                    toc_llm_assist_applied = bool(toc_assist.get("applied", False))
                    toc_llm_groups_considered = int(toc_assist.get("groups_considered", 0) or 0)
                    toc_llm_groups_applied = int(toc_assist.get("groups_applied", 0) or 0)

            tagging_metrics = validation_json.get("tagging", {})
            if isinstance(tagging_metrics, dict):
                headings_tagged = int(tagging_metrics.get("headings_tagged", 0) or 0)
                figures_tagged = int(tagging_metrics.get("figures_tagged", 0) or 0)
                decorative_figures_artifacted = int(
                    tagging_metrics.get("decorative_figures_artifacted", 0) or 0
                )
                tables_tagged = int(tagging_metrics.get("tables_tagged", 0) or 0)
                lists_tagged = int(tagging_metrics.get("lists_tagged", 0) or 0)
                links_tagged = int(tagging_metrics.get("links_tagged", 0) or 0)
                bookmarks_added = int(tagging_metrics.get("bookmarks_added", 0) or 0)
                struct_elems_created = (
                    headings_tagged
                    + figures_tagged
                    + tables_tagged
                    + lists_tagged
                    + links_tagged
                    + decorative_figures_artifacted
                )
                tags_total = struct_elems_created

            baseline_metrics = validation_json.get("baseline", {})
            if isinstance(baseline_metrics, dict):
                baseline_compliant = bool(baseline_metrics.get("compliant", False))
                baseline_validator = str(baseline_metrics.get("validator", "unknown") or "unknown")
                summary = baseline_metrics.get("summary", {})
                if isinstance(summary, dict):
                    baseline_validation_errors = int(summary.get("errors", 0) or 0)
                    baseline_validation_warnings = int(summary.get("warnings", 0) or 0)

            validator = str(validation_json.get("validator", "unknown") or "unknown")
            compliant = bool(validation_json.get("compliant", False))
            fidelity_passed = bool(fidelity_json.get("passed", False))

            summary_metrics = validation_json.get("summary", {})
            if isinstance(summary_metrics, dict):
                validation_errors = int(summary_metrics.get("errors", 0) or 0)
                validation_warnings = int(summary_metrics.get("warnings", 0) or 0)

            remediation_metrics = validation_json.get("remediation", {})
            if isinstance(remediation_metrics, dict):
                validation_errors_reduced = int(remediation_metrics.get("errors_reduced", 0) or 0)
                validation_warnings_reduced = int(
                    remediation_metrics.get("warnings_reduced", 0) or 0
                )
                font_metrics = remediation_metrics.get("font_remediation", {})
                if isinstance(font_metrics, dict):
                    font_lane_attempted = bool(font_metrics.get("attempted", False))
                    font_lane_applied = bool(font_metrics.get("applied", False))
                    font_lane_first_errors = int(font_metrics.get("first_pass_errors", 0) or 0)
                    font_lane_first_warnings = int(font_metrics.get("first_pass_warnings", 0) or 0)
                    font_lane_second_errors = int(font_metrics.get("second_pass_errors", 0) or 0)
                    font_lane_second_warnings = int(
                        font_metrics.get("second_pass_warnings", 0) or 0
                    )

            review_tasks_result = await db.execute(
                select(ReviewTask).where(ReviewTask.job_id == job_id)
            )
            review_tasks = review_tasks_result.scalars().all()
            review_tasks_total = len(review_tasks)
            blocking_review_tasks = sum(1 for task in review_tasks if bool(task.blocking))
            blocking_validation_tasks = sum(
                1
                for task in review_tasks
                if bool(task.blocking) and str(task.source) == "validation"
            )
            blocking_fidelity_tasks = sum(
                1
                for task in review_tasks
                if bool(task.blocking) and str(task.source) != "validation"
            )
            advisory_review_tasks = review_tasks_total - blocking_review_tasks

            output_path = Path(job.output_path) if job.output_path else None
            if output_path and output_path.exists():
                dest = run_dir / f"{pdf_path.stem}_workflow_output.pdf"
                shutil.copy2(output_path, dest)

            if not validation_json and not error:
                if job.status == "manual_remediation":
                    blocking_pending = sum(
                        1
                        for task in review_tasks
                        if bool(task.blocking) and str(task.status) == "pending_review"
                    )
                    pending_changes = await db.execute(
                        select(AppliedChange).where(
                            AppliedChange.job_id == job_id,
                            AppliedChange.review_status == "pending_review",
                            AppliedChange.reviewable.is_(True),
                        )
                    )
                    pending_change_count = len(pending_changes.scalars().all())
                    if blocking_pending or pending_change_count:
                        pending_parts: list[str] = []
                        if pending_change_count:
                            pending_parts.append(
                                f"{pending_change_count} applied change review item(s)"
                            )
                        if blocking_pending:
                            pending_parts.append(f"{blocking_pending} blocking review item(s)")
                        error = "Review still pending: " + " and ".join(pending_parts)
                    else:
                        error = (
                            "Workflow finished in review without validation or pending review items"
                        )
                else:
                    error = f"Workflow finished with status={job.status} but no validation payload"

    except Exception as exc:
        error = re.sub(r"\s+", " ", str(exc)).strip()
    finally:
        cleanup_job_files(job_id, input_path=None)

    total_secs = time.perf_counter() - started
    return DocMetrics(
        source_path=str(pdf_path),
        file_size_bytes=size,
        pages=pages,
        link_annots_in_source=source_links,
        source_has_forms=source_has_forms,
        classification=classification_type,
        structure_secs=round(structure_secs, 3),
        tagging_secs=round(tagging_secs, 3),
        validation_secs=round(validation_secs, 3),
        total_secs=round(total_secs, 3),
        llm_requests=llm_requests,
        llm_prompt_tokens=llm_prompt_tokens,
        llm_completion_tokens=llm_completion_tokens,
        llm_total_tokens=llm_total_tokens,
        llm_cost_usd=round(llm_cost_usd, 6),
        structure_ok=structure_ok,
        tag_ok=tag_ok,
        validation_ok=validation_ok,
        error=error,
        baseline_validator=baseline_validator,
        baseline_compliant=baseline_compliant,
        baseline_validation_errors=baseline_validation_errors,
        baseline_validation_warnings=baseline_validation_warnings,
        validator=validator,
        final_status=final_status,
        compliant=compliant,
        fidelity_passed=fidelity_passed,
        validation_errors=validation_errors,
        validation_warnings=validation_warnings,
        validation_errors_reduced=validation_errors_reduced,
        validation_warnings_reduced=validation_warnings_reduced,
        review_tasks_total=review_tasks_total,
        blocking_review_tasks=blocking_review_tasks,
        blocking_validation_tasks=blocking_validation_tasks,
        blocking_fidelity_tasks=blocking_fidelity_tasks,
        advisory_review_tasks=advisory_review_tasks,
        font_lane_attempted=font_lane_attempted,
        font_lane_applied=font_lane_applied,
        font_lane_first_errors=font_lane_first_errors,
        font_lane_first_warnings=font_lane_first_warnings,
        font_lane_second_errors=font_lane_second_errors,
        font_lane_second_warnings=font_lane_second_warnings,
        elements_total=elements_total,
        elements_headings=elements_headings,
        elements_figures=elements_figures,
        elements_tables=elements_tables,
        elements_list_items=elements_list_items,
        elements_toc_captions=elements_toc_captions,
        elements_toc_items=elements_toc_items,
        tags_total=tags_total,
        struct_elems_created=struct_elems_created,
        headings_tagged=headings_tagged,
        figures_tagged=figures_tagged,
        decorative_figures_artifacted=decorative_figures_artifacted,
        tables_tagged=tables_tagged,
        lists_tagged=lists_tagged,
        links_tagged=links_tagged,
        bookmarks_added=bookmarks_added,
        toc_llm_assist_attempted=toc_llm_assist_attempted,
        toc_llm_assist_applied=toc_llm_assist_applied,
        toc_llm_groups_considered=toc_llm_groups_considered,
        toc_llm_groups_applied=toc_llm_groups_applied,
        heading_coverage=round(_safe_ratio(headings_tagged, elements_headings), 3),
        figure_coverage=round(_safe_ratio(figures_tagged, elements_figures), 3),
        table_coverage=round(_safe_ratio(tables_tagged, elements_tables), 3),
        list_coverage=round(_safe_ratio(lists_tagged, elements_list_items), 3),
        link_coverage=round(_safe_ratio(links_tagged, source_links), 3),
    )


async def benchmark_one(pdf_path: Path, run_dir: Path, settings) -> DocMetrics:
    started = time.perf_counter()
    structure_secs = 0.0
    tagging_secs = 0.0
    validation_secs = 0.0
    structure_ok = False
    tag_ok = False
    validation_ok = False
    error = ""
    baseline_validator = "unknown"
    baseline_compliant = False
    baseline_validation_errors = 0
    baseline_validation_warnings = 0
    validator = "unknown"
    final_status = "unknown"
    compliant = False
    fidelity_passed = False
    validation_errors = 0
    validation_warnings = 0
    validation_errors_reduced = 0
    validation_warnings_reduced = 0
    review_tasks_total = 0
    blocking_review_tasks = 0
    blocking_validation_tasks = 0
    blocking_fidelity_tasks = 0
    advisory_review_tasks = 0
    font_lane_attempted = False
    font_lane_applied = False
    font_lane_first_errors = 0
    font_lane_first_warnings = 0
    font_lane_second_errors = 0
    font_lane_second_warnings = 0

    pages, source_links, source_has_forms = _count_source_features(pdf_path)
    size = pdf_path.stat().st_size if pdf_path.exists() else 0
    classification_type = "unknown"

    elements_total = 0
    elements_headings = 0
    elements_figures = 0
    elements_tables = 0
    elements_list_items = 0

    tags_total = 0
    struct_elems_created = 0
    headings_tagged = 0
    figures_tagged = 0
    decorative_figures_artifacted = 0
    tables_tagged = 0
    lists_tagged = 0
    links_tagged = 0
    bookmarks_added = 0

    structure = None
    tagged_path = run_dir / f"{pdf_path.stem}_tagged.pdf"
    tag_input_path = pdf_path

    try:
        try:
            classification = await classify_pdf(pdf_path)
            classification_type = classification.type
        except Exception:
            classification_type = "unknown"

        baseline_validation = await validate_pdf(
            pdf_path=pdf_path,
            verapdf_path=settings.verapdf_path,
            flavour=settings.verapdf_flavour,
        )
        baseline_compliant = baseline_validation.compliant
        baseline_validation_errors = _violation_count(baseline_validation, "error")
        baseline_validation_warnings = _violation_count(baseline_validation, "warning")
        baseline_validator = (
            "veraPDF"
            if baseline_validation.raw_report.get("report")
            else baseline_validation.raw_report.get("validator", "unknown")
        )

        t0 = time.perf_counter()
        structure = await extract_structure(pdf_path, run_dir)
        structure_secs = time.perf_counter() - t0
        structure_ok = True
        if structure.processed_pdf_path:
            tag_input_path = structure.processed_pdf_path

        elements = structure.document_json.get("elements", [])
        elements_total = len(elements)
        elements_headings = sum(1 for e in elements if e.get("type") == "heading")
        elements_figures = sum(1 for e in elements if e.get("type") == "figure")
        elements_tables = sum(1 for e in elements if e.get("type") == "table")
        elements_list_items = sum(1 for e in elements if e.get("type") == "list_item")

        t1 = time.perf_counter()
        tagging = await tag_pdf(
            input_path=tag_input_path,
            output_path=tagged_path,
            structure_json=structure.document_json,
            alt_texts=[],
            original_filename=pdf_path.name,
        )
        tagging_secs = time.perf_counter() - t1
        tag_ok = True

        tags_total = tagging.tags_added
        struct_elems_created = tagging.struct_elems_created
        headings_tagged = tagging.headings_tagged
        figures_tagged = tagging.figures_tagged
        decorative_figures_artifacted = tagging.decorative_figures_artifacted
        tables_tagged = tagging.tables_tagged
        lists_tagged = tagging.lists_tagged
        links_tagged = tagging.links_tagged
        bookmarks_added = tagging.bookmarks_added

        t2 = time.perf_counter()
        validation = await validate_pdf(
            pdf_path=tagged_path,
            verapdf_path=settings.verapdf_path,
            flavour=settings.verapdf_flavour,
        )
        validation_secs = time.perf_counter() - t2
        validation_ok = True

        selected_validation = validation
        selected_tagging = tagging
        font_lane_first_errors = _violation_count(validation, "error")
        font_lane_first_warnings = _violation_count(validation, "warning")
        font_lane_second_errors = font_lane_first_errors
        font_lane_second_warnings = font_lane_first_warnings

        if not validation.compliant and _has_font_errors(validation):
            planned_lanes = _font_remediation_lanes(
                validation,
                classification=classification_type,
                page_count=pages,
                link_annots=source_links,
                has_forms=source_has_forms,
                settings=settings,
            )
            font_lane_attempted = bool(planned_lanes)
            for lane in planned_lanes:
                remediation_input = tag_input_path
                if lane == FONT_LANE_REPAIR_DICTS:
                    repaired = run_dir / f"{pdf_path.stem}_fontfix_repair_dicts.pdf"
                    repaired_ok, _ = _repair_pdf_font_dicts(tag_input_path, repaired)
                    if not repaired_ok:
                        continue
                    remediation_input = repaired
                elif lane == FONT_LANE_REPAIR_TOUNICODE:
                    repaired = run_dir / f"{pdf_path.stem}_fontfix_repair_tounicode.pdf"
                    repaired_ok, _ = _repair_pdf_tounicode(tag_input_path, repaired)
                    if not repaired_ok:
                        continue
                    remediation_input = repaired
                elif lane == FONT_LANE_EMBED:
                    rewritten = run_dir / f"{pdf_path.stem}_fontfix_embedded.pdf"
                    rewritten_ok = await _rewrite_pdf_with_ghostscript_embed(
                        tag_input_path, rewritten
                    )
                    if not rewritten_ok:
                        continue
                    remediation_input = rewritten
                elif lane in (FONT_LANE_OCR_REDO, FONT_LANE_OCR_FORCE):
                    mode = "redo" if lane == FONT_LANE_OCR_REDO else "force"
                    ocr_output = run_dir / f"{pdf_path.stem}_fontfix_{mode}_ocred.pdf"
                    ocr_result = await run_ocr(
                        input_path=tag_input_path,
                        output_path=ocr_output,
                        language=settings.ocr_language,
                        mode=mode,
                    )
                    if not ocr_result.success:
                        continue
                    remediation_input = ocr_result.output_path
                else:
                    continue

                lane_tagged = run_dir / f"{pdf_path.stem}_{lane}_tagged.pdf"
                lane_tagging = await tag_pdf(
                    input_path=remediation_input,
                    output_path=lane_tagged,
                    structure_json=structure.document_json,
                    alt_texts=[],
                    original_filename=pdf_path.name,
                )
                candidate_validation = await validate_pdf(
                    pdf_path=lane_tagging.output_path,
                    verapdf_path=settings.verapdf_path,
                    flavour=settings.verapdf_flavour,
                )
                regressions = _tagging_regressions(lane_tagging, selected_tagging)
                if (
                    _is_better_validation(candidate_validation, selected_validation)
                    and not regressions
                ):
                    selected_validation = candidate_validation
                    selected_tagging = lane_tagging
                    tags_total = lane_tagging.tags_added
                    struct_elems_created = lane_tagging.struct_elems_created
                    headings_tagged = lane_tagging.headings_tagged
                    figures_tagged = lane_tagging.figures_tagged
                    decorative_figures_artifacted = lane_tagging.decorative_figures_artifacted
                    tables_tagged = lane_tagging.tables_tagged
                    lists_tagged = lane_tagging.lists_tagged
                    links_tagged = lane_tagging.links_tagged
                    bookmarks_added = lane_tagging.bookmarks_added
                    font_lane_applied = True
                    if selected_validation.compliant:
                        break

            font_lane_second_errors = _violation_count(selected_validation, "error")
            font_lane_second_warnings = _violation_count(selected_validation, "warning")

        compliant = selected_validation.compliant
        validation_errors = _violation_count(selected_validation, "error")
        validation_warnings = _violation_count(selected_validation, "warning")
        validation_errors_reduced = baseline_validation_errors - validation_errors
        validation_warnings_reduced = baseline_validation_warnings - validation_warnings
        validator = (
            "veraPDF"
            if selected_validation.raw_report.get("report")
            else selected_validation.raw_report.get("validator", "unknown")
        )
        final_status = "complete" if compliant else "manual_remediation"
        fidelity_passed = compliant
    except Exception as exc:
        error = re.sub(r"\s+", " ", str(exc)).strip()

    total_secs = time.perf_counter() - started
    return DocMetrics(
        source_path=str(pdf_path),
        file_size_bytes=size,
        pages=pages,
        link_annots_in_source=source_links,
        source_has_forms=source_has_forms,
        classification=classification_type,
        structure_secs=round(structure_secs, 3),
        tagging_secs=round(tagging_secs, 3),
        validation_secs=round(validation_secs, 3),
        total_secs=round(total_secs, 3),
        structure_ok=structure_ok,
        tag_ok=tag_ok,
        validation_ok=validation_ok,
        error=error,
        baseline_validator=baseline_validator,
        baseline_compliant=baseline_compliant,
        baseline_validation_errors=baseline_validation_errors,
        baseline_validation_warnings=baseline_validation_warnings,
        validator=validator,
        final_status=final_status,
        compliant=compliant,
        fidelity_passed=fidelity_passed,
        validation_errors=validation_errors,
        validation_warnings=validation_warnings,
        validation_errors_reduced=validation_errors_reduced,
        validation_warnings_reduced=validation_warnings_reduced,
        review_tasks_total=review_tasks_total,
        blocking_review_tasks=blocking_review_tasks,
        blocking_validation_tasks=blocking_validation_tasks,
        blocking_fidelity_tasks=blocking_fidelity_tasks,
        advisory_review_tasks=advisory_review_tasks,
        font_lane_attempted=font_lane_attempted,
        font_lane_applied=font_lane_applied,
        font_lane_first_errors=font_lane_first_errors,
        font_lane_first_warnings=font_lane_first_warnings,
        font_lane_second_errors=font_lane_second_errors,
        font_lane_second_warnings=font_lane_second_warnings,
        elements_total=elements_total,
        elements_headings=elements_headings,
        elements_figures=elements_figures,
        elements_tables=elements_tables,
        elements_list_items=elements_list_items,
        tags_total=tags_total,
        struct_elems_created=struct_elems_created,
        headings_tagged=headings_tagged,
        figures_tagged=figures_tagged,
        decorative_figures_artifacted=decorative_figures_artifacted,
        tables_tagged=tables_tagged,
        lists_tagged=lists_tagged,
        links_tagged=links_tagged,
        bookmarks_added=bookmarks_added,
        heading_coverage=round(_safe_ratio(headings_tagged, elements_headings), 3),
        figure_coverage=round(_safe_ratio(figures_tagged, elements_figures), 3),
        table_coverage=round(_safe_ratio(tables_tagged, elements_tables), 3),
        list_coverage=round(_safe_ratio(lists_tagged, elements_list_items), 3),
        link_coverage=round(_safe_ratio(links_tagged, source_links), 3),
    )


def write_outputs(output_dir: Path, rows: list[DocMetrics], mode: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "corpus_summary.csv"
    json_path = output_dir / "corpus_summary.json"
    md_path = output_dir / "corpus_report.md"
    rule_backlog_path = output_dir / "rule_backlog.json"
    review_backlog_path = output_dir / "review_backlog.json"

    dict_rows = [asdict(r) for r in rows]
    fields = list(dict_rows[0].keys()) if dict_rows else []

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(dict_rows)

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(dict_rows, f, indent=2)

    rule_backlog = _build_rule_backlog(output_dir / "workflow_benchmark.sqlite3")
    with rule_backlog_path.open("w", encoding="utf-8") as f:
        json.dump(rule_backlog, f, indent=2)

    review_backlog = _build_review_backlog(output_dir / "workflow_benchmark.sqlite3")
    with review_backlog_path.open("w", encoding="utf-8") as f:
        json.dump(review_backlog, f, indent=2)

    total = len(rows)
    completed = [r for r in rows if r.error == ""]
    failed = [r for r in rows if r.error != ""]
    compliant = [r for r in completed if r.compliant]
    fidelity_passed = [r for r in completed if r.fidelity_passed]
    release_ready = [r for r in completed if r.final_status == "complete"]
    manual_remediation = [r for r in completed if r.final_status == "manual_remediation"]
    non_compliant = [r for r in completed if not r.compliant]
    font_attempted = [r for r in completed if r.font_lane_attempted]
    font_applied = [r for r in completed if r.font_lane_applied]
    toc_assist_attempted = [r for r in completed if r.toc_llm_assist_attempted]
    toc_assist_applied = [r for r in completed if r.toc_llm_assist_applied]
    font_improved = [
        r
        for r in completed
        if r.font_lane_attempted
        and (
            r.font_lane_second_errors < r.font_lane_first_errors
            or (
                r.font_lane_second_errors == r.font_lane_first_errors
                and r.font_lane_second_warnings < r.font_lane_first_warnings
            )
        )
    ]
    link_gaps = sorted(
        [r for r in completed if r.link_annots_in_source > 0 and r.link_coverage < 1.0],
        key=lambda r: r.link_coverage,
    )
    heading_gaps = sorted(
        [r for r in completed if r.elements_headings > 0 and r.heading_coverage < 1.0],
        key=lambda r: r.heading_coverage,
    )

    with md_path.open("w", encoding="utf-8") as f:
        f.write("# Corpus Accessibility Benchmark Report\n\n")
        f.write(f"Generated: {datetime.now().isoformat()}\n\n")
        f.write(f"Mode: `{mode}`\n\n")
        f.write("## Summary\n\n")
        baseline_errors = sum(r.baseline_validation_errors for r in completed)
        baseline_warnings = sum(r.baseline_validation_warnings for r in completed)
        remediated_errors = sum(r.validation_errors for r in completed)
        remediated_warnings = sum(r.validation_warnings for r in completed)
        total_llm_requests = sum(r.llm_requests for r in completed)
        total_llm_prompt_tokens = sum(r.llm_prompt_tokens for r in completed)
        total_llm_completion_tokens = sum(r.llm_completion_tokens for r in completed)
        total_llm_tokens = sum(r.llm_total_tokens for r in completed)
        total_llm_cost = sum(r.llm_cost_usd for r in completed)
        error_delta = baseline_errors - remediated_errors
        warning_delta = baseline_warnings - remediated_warnings
        f.write(f"- PDFs processed: {total}\n")
        f.write(f"- Successful runs: {len(completed)}\n")
        f.write(f"- Failed runs: {len(failed)}\n")
        f.write(f"- Compliant outputs: {len(compliant)} / {len(completed) if completed else 0}\n")
        f.write(
            f"- Fidelity-passed outputs: {len(fidelity_passed)} / {len(completed) if completed else 0}\n"
        )
        f.write(
            f"- Release-ready outputs: {len(release_ready)} / {len(completed) if completed else 0}\n"
        )
        f.write(f"- Manual remediation required: {len(manual_remediation)}\n")
        f.write(f"- Non-compliant outputs: {len(non_compliant)}\n")
        f.write(
            f"- Validation errors before/after: {baseline_errors} -> {remediated_errors} "
            f"(delta {error_delta:+d})\n"
        )
        f.write(
            f"- Validation warnings before/after: {baseline_warnings} -> {remediated_warnings} "
            f"(delta {warning_delta:+d})\n"
        )
        f.write(
            f"- Font lane attempted/applied: {len(font_attempted)} / {len(font_applied)} "
            f"(improved {len(font_improved)})\n"
        )
        f.write(
            f"- TOC LLM assist attempted/applied: {len(toc_assist_attempted)} / {len(toc_assist_applied)}\n"
        )
        f.write(
            f"- LLM requests / tokens / cost: {total_llm_requests} / {total_llm_tokens} "
            f"(prompt {total_llm_prompt_tokens}, completion {total_llm_completion_tokens}) / "
            f"${total_llm_cost:.6f}\n"
        )
        if completed:
            f.write(
                f"- Average LLM cost per successful PDF: ${total_llm_cost / len(completed):.6f}\n"
            )
        f.write("\n## Top Link Coverage Gaps\n\n")
        if link_gaps:
            for row in link_gaps[:10]:
                f.write(
                    f"- {Path(row.source_path).name}: "
                    f"{row.links_tagged}/{row.link_annots_in_source} ({row.link_coverage:.3f})\n"
                )
        else:
            f.write("- None\n")
        f.write("\n## TOC LLM Assist\n\n")
        if toc_assist_attempted:
            for row in toc_assist_attempted[:10]:
                f.write(
                    f"- {Path(row.source_path).name}: "
                    f"applied={row.toc_llm_assist_applied}, "
                    f"groups={row.toc_llm_groups_applied}/{row.toc_llm_groups_considered}, "
                    f"toc_elements={row.elements_toc_captions + row.elements_toc_items}\n"
                )
        else:
            f.write("- None\n")
        f.write("\n## Top Heading Coverage Gaps\n\n")
        if heading_gaps:
            for row in heading_gaps[:10]:
                f.write(
                    f"- {Path(row.source_path).name}: "
                    f"{row.headings_tagged}/{row.elements_headings} ({row.heading_coverage:.3f})\n"
                )
        else:
            f.write("- None\n")
        f.write("\n## Top Remaining Rules\n\n")
        if rule_backlog:
            for entry in rule_backlog[:10]:
                top_docs = ", ".join(
                    f"{doc['filename']} ({doc['errors']})" for doc in entry["documents"][:3]
                )
                f.write(
                    f"- {entry['rule_id']}: {entry['total_errors']} errors across "
                    f"{entry['documents_affected']} document(s)"
                )
                if top_docs:
                    f.write(f" [{top_docs}]")
                f.write("\n")
        else:
            f.write("- None\n")
        f.write("\n## Top Manual Review Tasks\n\n")
        if review_backlog:
            for entry in review_backlog[:10]:
                top_docs = ", ".join(
                    f"{doc['filename']} ({doc['occurrences']})" for doc in entry["documents"][:3]
                )
                f.write(
                    f"- {entry['task_type']} [{entry['source']}] "
                    f"{'blocking' if entry['blocking'] else 'advisory'}: "
                    f"{entry['documents_affected']} document(s), {entry['occurrences']} occurrence(s)"
                )
                if top_docs:
                    f.write(f" [{top_docs}]")
                f.write("\n")
        else:
            f.write("- None\n")
        f.write("\n## Failed Files\n\n")
        if failed:
            for row in failed:
                f.write(f"- {row.source_path}: {row.error}\n")
        else:
            f.write("- None\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exclude-wac",
        action="store_true",
        help="Skip backend/test_wac.pdf from discovery.",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help=(
            "Additional PDF discovery root. May be passed multiple times. "
            "Use with --no-default-roots for a focused corpus."
        ),
    )
    parser.add_argument(
        "--no-default-roots",
        action="store_true",
        help="Do not scan the default Desktop/Documents/Downloads roots.",
    )
    args = parser.parse_args()

    scan_roots: list[Path] = []
    if not args.no_default_roots:
        scan_roots.extend(DEFAULT_SCAN_ROOTS)
    scan_roots.extend(Path(root).expanduser().resolve() for root in args.root)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_ROOT / f"corpus_{ts}"
    run_dir = out_dir / "tagged_outputs"
    run_dir.mkdir(parents=True, exist_ok=True)

    pdfs = discover_pdfs(
        exclude_wac=args.exclude_wac,
        scan_roots=scan_roots,
    )
    print(f"Discovered {len(pdfs)} PDFs")
    if not pdfs:
        return

    rows: list[DocMetrics] = []
    settings = get_settings()
    workflow_db = out_dir / "workflow_benchmark.sqlite3"
    workflow_engine, workflow_session_maker = await _init_workflow_db(workflow_db)
    workflow_job_manager = JobManager()

    for idx, pdf in enumerate(pdfs, start=1):
        print(f"[{idx}/{len(pdfs)}] {pdf}")
        doc_dir = run_dir / f"{idx:03d}"
        doc_dir.mkdir(parents=True, exist_ok=True)
        row = await benchmark_one_workflow(
            pdf,
            doc_dir,
            settings,
            workflow_session_maker,
            workflow_job_manager,
        )
        rows.append(row)
        status = "OK" if row.error == "" else "FAIL"
        print(
            f"  -> {status} | final={row.final_status} | compliant={row.compliant} "
            f"| fidelity={row.fidelity_passed} | "
            f"h={row.headings_tagged}/{row.elements_headings} "
            f"l={row.links_tagged}/{row.link_annots_in_source} "
            f"time={row.total_secs:.2f}s"
        )

    write_outputs(out_dir, rows, "workflow")
    await workflow_engine.dispose()
    print(f"\nWrote benchmark outputs to: {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
