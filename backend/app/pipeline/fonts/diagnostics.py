"""PDF font features, per-font diagnostics, and automatic Unicode repair gates."""

import logging
from pathlib import Path

from app.pipeline.fonts.encoding import (
    _is_valid_unicode_text,
    _parse_tounicode_map_details,
    _raw_text_bytes,
    _simple_font_auto_unicode_policy,
    _simple_font_unicode_map,
)
from app.pipeline.fonts.names import _base_font_name, _looks_like_ocr_overlay_font
from app.pipeline.fonts.programs import _local_embed_support_kind, _local_font_program
from app.pipeline.fonts.resources import (
    DIAGNOSTIC_RESOURCES,
    _font_stream_bytes,
    _has_embedded_font,
    _is_form_xobject,
    _iter_pdf_fonts,
    _obj_key,
    _resolve_dictionary,
)
from app.pipeline.fonts.rules import FONT_UNICODE_RULE_MARKERS
from app.services.pdf_objects import pdf_named_resource

logger = logging.getLogger("app.pipeline.orchestrator")


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

        seen_fonts: set[tuple[int, int]] = set()

        def _process_font(font_dict) -> None:
            font_key = _obj_key(font_dict)
            if font_key and font_key in seen_fonts:
                return
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
                            cid_font.get("/BaseFont") or font_dict.get("/BaseFont") or ""
                        )
            else:
                descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
                if isinstance(descriptor, pikepdf.Dictionary):
                    base_font_name = str(
                        font_dict.get("/BaseFont") or descriptor.get("/FontName") or ""
                    )

            if not _has_embedded_font(descriptor):
                features["unembedded_fonts"] = int(features["unembedded_fonts"]) + 1
            if _looks_like_ocr_overlay_font(base_font_name):
                features["ocr_suspect_fonts"] = int(features["ocr_suspect_fonts"]) + 1

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
            for font_dict in _iter_pdf_fonts(pdf):
                _process_font(font_dict)
    except Exception as exc:
        logger.warning(f"Failed to inspect PDF features for remediation gating: {exc}")
    return features


def _unicode_repair_gate_from_diagnostics(
    font_diagnostics: dict[str, object] | None,
    *,
    violations=None,
) -> dict[str, object]:
    """Decide whether automatic ToUnicode repair is deterministic enough to run."""
    blocked_examples: list[str] = []
    profile: dict[str, object] = {
        "allow_automatic": False,
        "safe_type0_candidates": 0,
        "safe_simple_candidates": 0,
        "blocked_simple_fonts": 0,
        "safe_candidate_count": 0,
        "blocked_candidate_count": 0,
        "blocked_examples": blocked_examples,
        "reason": "",
    }
    has_invalid_unicode_rules = any(
        v.severity == "error" and "7.21.7-2" in str(v.rule_id) for v in (violations or [])
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

        if subtype not in ("/Type1", "/MMType1", "/TrueType", "/Type3"):
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
        if (
            policy in {"explicit", "standard14", "embedded_cff", "embedded_type1", "symbol"}
            and repair_evidence
        ):
            profile["safe_simple_candidates"] = int(profile["safe_simple_candidates"]) + 1
            continue

        profile["blocked_simple_fonts"] = int(profile["blocked_simple_fonts"]) + 1
        if len(blocked_examples) < 5:
            name = str(item.get("base_font") or "(unnamed)")
            if name not in blocked_examples:
                blocked_examples.append(name)

    safe_candidates = int(profile["safe_type0_candidates"]) + int(profile["safe_simple_candidates"])
    blocked_candidates = int(profile["blocked_simple_fonts"])
    profile["safe_candidate_count"] = safe_candidates
    profile["blocked_candidate_count"] = blocked_candidates
    profile["allow_automatic"] = safe_candidates > 0
    if safe_candidates > 0:
        profile["reason"] = "deterministic font candidates available"
    elif blocked_candidates > 0:
        suffix = f" ({', '.join(blocked_examples)})" if blocked_examples else ""
        profile["reason"] = (
            "unicode issues appear tied to simple fonts without explicit encoding" + suffix
        )
    else:
        profile["reason"] = "no deterministic ToUnicode candidates found"
    return profile


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
        seen_fonts: set[tuple[int, int]] = set()
        seen_content_streams: set[tuple[int, int]] = set()
        used_simple_codes: dict[tuple[int, int], set[int]] = {}
        profiles_by_key: dict[str, dict[str, object]] = {}
        simple_font_candidates: dict[tuple[int, int], dict[str, object]] = {}

        def _collect_used_simple_font_codes(
            content_obj, resources, *, target_font_keys: set[tuple[int, int]]
        ) -> None:
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
                for _, font_obj in fonts.items():
                    font_dict = _resolve_dictionary(font_obj)
                    font_key = (
                        _obj_key(font_dict) if isinstance(font_dict, pikepdf.Dictionary) else None
                    )
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
                    current_font = _resolve_dictionary(pdf_named_resource(fonts, operands[0]))
                    continue

                if op in ("Tj", "TJ", "'", '"') and isinstance(current_font, pikepdf.Dictionary):
                    if current_font.get("/Subtype") not in (
                        pikepdf.Name("/Type1"),
                        pikepdf.Name("/MMType1"),
                        pikepdf.Name("/TrueType"),
                        pikepdf.Name("/Type3"),
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
                    xobject = _resolve_dictionary(pdf_named_resource(xobjects, operands[0]))
                    if not _is_form_xobject(xobject):
                        continue
                    child_resources = _resolve_dictionary(xobject.get("/Resources")) or resources
                    _collect_used_simple_font_codes(
                        xobject, child_resources, target_font_keys=target_font_keys
                    )

        def _touch_profile(font_dict, *, descendant_subtype=None, descriptor=None) -> None:
            font_key = _obj_key(font_dict)
            profile_key = f"{font_key[0]} {font_key[1]}" if font_key else f"inline:{id(font_dict)}"
            profile = profiles_by_key.get(profile_key)
            if profile is None:
                base_font = _base_font_name(font_dict)
                subtype = str(font_dict.get("/Subtype") or "")
                has_tounicode = pikepdf.Name("/ToUnicode") in font_dict
                encoding = font_dict.get("/Encoding")
                encoding_name = (
                    str(encoding)
                    if encoding is not None and not isinstance(encoding, pikepdf.Dictionary)
                    else ("dict" if isinstance(encoding, pikepdf.Dictionary) else "")
                )
                font_bytes = _font_stream_bytes(descriptor)
                auto_unicode_policy = ""
                if subtype in ("/Type1", "/MMType1", "/TrueType", "/Type3"):
                    auto_unicode_policy = _simple_font_auto_unicode_policy(
                        font_dict, font_bytes=font_bytes
                    )
                embedded = _has_embedded_font(descriptor)
                local_embed_support = _local_embed_support_kind(
                    font_dict, descendant_subtype=descendant_subtype
                )
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
                    "descendant_subtype": str(descendant_subtype or "")
                    if descendant_subtype
                    else "",
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
            if font_key and profile["subtype"] in ("/Type1", "/MMType1", "/TrueType", "/Type3"):
                simple_font_candidates[font_key] = {
                    "profile": profile,
                    "font_dict": font_dict,
                    "font_bytes": _font_stream_bytes(descriptor),
                    "existing_map": existing_map,
                }

        def _process_font(font_dict) -> None:
            font_key = _obj_key(font_dict)
            if font_key and font_key in seen_fonts:
                return
            if font_key:
                seen_fonts.add(font_key)

            subtype = font_dict.get("/Subtype")
            if subtype == pikepdf.Name("/Type0"):
                descendants = font_dict.get("/DescendantFonts")
                cid_font = None
                if isinstance(descendants, pikepdf.Array) and descendants:
                    cid_font = _resolve_dictionary(descendants[0])
                descriptor = (
                    _resolve_dictionary(cid_font.get("/FontDescriptor"))
                    if isinstance(cid_font, pikepdf.Dictionary)
                    else None
                )
                descendant_subtype = (
                    cid_font.get("/Subtype") if isinstance(cid_font, pikepdf.Dictionary) else None
                )
                _touch_profile(
                    font_dict,
                    descendant_subtype=descendant_subtype,
                    descriptor=descriptor,
                )
            else:
                descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
                _touch_profile(font_dict, descriptor=descriptor)

        with pikepdf.open(str(pdf_path)) as pdf:
            acroform = _resolve_dictionary(pdf.Root.get("/AcroForm"))
            for font_dict in _iter_pdf_fonts(pdf, policy=DIAGNOSTIC_RESOURCES):
                _process_font(font_dict)

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
                            if code not in existing_map
                            or not _is_valid_unicode_text(existing_map[code])
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
            if str(profile["subtype"]) in {"/Type1", "/MMType1", "/TrueType", "/Type3"}
        )
        summary["type0_fonts"] = sum(
            1 for profile in profiles if str(profile["subtype"]) == "/Type0"
        )
        summary["embedded_fonts"] = sum(1 for profile in profiles if bool(profile["embedded"]))
        summary["unembedded_fonts"] = sum(
            1 for profile in profiles if not bool(profile["embedded"])
        )
        summary["fonts_with_tounicode"] = sum(
            1 for profile in profiles if bool(profile["has_tounicode"])
        )
        summary["fonts_missing_tounicode"] = sum(
            1 for profile in profiles if not bool(profile["has_tounicode"])
        )
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
        diagnostics["profiles"] = profiles[: max(0, profile_limit)]
    except Exception as exc:
        diagnostics["error"] = str(exc)

    return diagnostics
