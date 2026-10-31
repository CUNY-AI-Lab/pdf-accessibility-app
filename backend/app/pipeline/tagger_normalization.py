"""Normalize document metadata and PDF/UA resources and save viewer-compatible PDFs."""

import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, TypeGuard

import pikepdf

from app.pipeline.document_title import FILE_NAME_TITLE_RE as FILE_NAME_TITLE_RE
from app.pipeline.document_title import OFFICE_TITLE_PREFIX_RE as OFFICE_TITLE_PREFIX_RE
from app.pipeline.document_title import PLACEHOLDER_TITLE_RE as PLACEHOLDER_TITLE_RE
from app.pipeline.document_title import (
    _is_placeholder_document_title as _is_placeholder_document_title,
)
from app.pipeline.document_title import select_document_title
from app.pipeline.fonts.resources import TAGGER_RESOURCES, _has_embedded_font, _iter_pdf_fonts
from app.pipeline.language import normalize_lang_tag as _normalize_lang_tag
from app.pipeline.pdf_repair import add_missing_icc_components
from app.pipeline.tagger_geometry import _as_positive_int, _obj_key, _resolve_dictionary
from app.services.pdf_objects import pdf_text

logger = logging.getLogger(__name__)


def _resolve_document_language(structure_json: dict[str, Any], explicit_language: str | None) -> str:
    """Resolve the best available document-level language tag for tagging."""
    structure_language = _normalize_lang_tag(structure_json.get("language"))
    if structure_language:
        return structure_language
    explicit = _normalize_lang_tag(explicit_language)
    return explicit or "en"


def _normalize_heading_hierarchy(elements: list[dict]) -> None:
    """Close skipped heading levels while keeping the nesting: each heading
    ends the open sections at its level or deeper and goes one below the
    section still open, so the outline starts at H1, an H4 right after an H2
    becomes an H3, and headings at one level stay siblings (H1, H2, H5, H5
    become H1, H2, H3, H3, not a staircase)."""
    headings = [el for el in elements if el.get("type") == "heading"]
    if not headings:
        return

    # The open sections, outermost first: (level given, level assigned).
    open_sections: list[tuple[int, int]] = []
    for heading in headings:
        level = _as_positive_int(heading.get("level", 1), default=1)
        while open_sections and open_sections[-1][0] >= level:
            open_sections.pop()
        assigned = min(6, open_sections[-1][1] + 1 if open_sections else 1)
        open_sections.append((level, assigned))
        heading["level"] = assigned


# ──────────────────────────────────────────────────────────────────────────────
# Document metadata
# ──────────────────────────────────────────────────────────────────────────────

def _reset_xmp_metadata(pdf: pikepdf.Pdf):
    """Drop source XMP metadata to avoid carrying malformed metadata packets."""
    try:
        if "/Metadata" in pdf.Root:
            del pdf.Root["/Metadata"]
    except Exception as e:
        logger.warning(f"Could not reset existing metadata stream: {e}")


def _set_document_title(
    pdf: pikepdf.Pdf, structure_json: dict, original_filename: str,
    *, pdf_path: Path | None = None,
):
    """Write one evidence-based title to Info and XMP before enabling display."""
    decision = select_document_title(pdf_path, structure_json, original_filename, source_pdf=pdf)
    title = decision["title"]
    # Read XMP's title before dropping potentially malformed source metadata.
    _reset_xmp_metadata(pdf)

    try:
        pdf.docinfo["/Title"] = pikepdf.String(title)
    except Exception as e:
        logger.warning(f"Could not set Info dictionary title: {e}")

    try:
        with pdf.open_metadata() as meta:
            meta["{http://purl.org/dc/elements/1.1/}title"] = title
    except Exception as e:
        logger.warning(f"Could not set XMP title: {e}")

    if "/ViewerPreferences" not in pdf.Root:
        pdf.Root["/ViewerPreferences"] = pikepdf.Dictionary()
    pdf.Root["/ViewerPreferences"]["/DisplayDocTitle"] = True

    return title


def _set_pdfua_identification(pdf: pikepdf.Pdf):
    """Set PDF/UA identification metadata in XMP."""
    try:
        with pdf.open_metadata() as meta:
            meta.register_xml_namespace("http://www.aiim.org/pdfua/ns/id/", "pdfuaid")
            meta["{http://www.aiim.org/pdfua/ns/id/}part"] = "1"
    except Exception as e:
        logger.warning(f"Could not set PDF/UA identification metadata: {e}")


def _normalize_optional_content_configs(pdf: pikepdf.Pdf) -> int:
    """Normalize optional content configs to satisfy PDF/UA syntax requirements."""
    ocprops = pdf.Root.get("/OCProperties")
    if not isinstance(ocprops, pikepdf.Dictionary):
        return 0

    changes = 0

    def _normalize_config(config, default_name: str) -> None:
        nonlocal changes
        if not isinstance(config, pikepdf.Dictionary):
            return

        try:
            name = pdf_text(config.get("/Name")).strip()
        except Exception:
            name = ""
        if not name:
            config["/Name"] = pikepdf.String(default_name)
            changes += 1
        if "/AS" in config:
            del config["/AS"]
            changes += 1

    _normalize_config(ocprops.get("/D"), "Default")

    configs = ocprops.get("/Configs")
    if isinstance(configs, pikepdf.Array):
        for idx, config in enumerate(configs):
            _normalize_config(config, f"Optional Content Config {idx + 1}")

    return changes


def _remove_dynamic_xfa(pdf: pikepdf.Pdf) -> bool:
    """Strip dynamic XFA packets from AcroForm dictionaries."""
    acroform = pdf.Root.get("/AcroForm")
    if not isinstance(acroform, pikepdf.Dictionary):
        return False
    if "/XFA" not in acroform:
        return False
    del acroform["/XFA"]
    return True


def _normalize_embedded_file_specs(pdf: pikepdf.Pdf) -> int:
    """Ensure embedded file specs carry non-empty F/UF filename keys."""
    names = pdf.Root.get("/Names")
    if not isinstance(names, pikepdf.Dictionary):
        return 0

    embedded_files = names.get("/EmbeddedFiles")
    if not isinstance(embedded_files, pikepdf.Dictionary):
        return 0

    changes = 0

    def _string_or_empty(value) -> str:
        if value is None:
            return ""
        try:
            text = str(value).strip()
        except Exception:
            return ""
        return "" if text == "None" else text

    def _walk_name_tree(node) -> list[tuple[str, pikepdf.Object]]:
        results: list[tuple[str, pikepdf.Object]] = []
        if not isinstance(node, pikepdf.Dictionary):
            return results
        kids = node.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                results.extend(_walk_name_tree(kid))
        names_array = node.get("/Names")
        if isinstance(names_array, pikepdf.Array):
            for idx in range(0, len(names_array) - 1, 2):
                results.append((str(names_array[idx]), names_array[idx + 1]))
        return results

    for entry_name, file_spec in _walk_name_tree(embedded_files):
        if not isinstance(file_spec, pikepdf.Dictionary):
            continue
        ef = file_spec.get("/EF")
        if not isinstance(ef, pikepdf.Dictionary) or len(ef) == 0:
            continue

        candidate = ""
        for value in (
            file_spec.get("/UF"),
            file_spec.get("/F"),
            entry_name,
            file_spec.get("/Desc"),
            "attachment",
        ):
            candidate = _string_or_empty(value)
            if candidate:
                break
        if not candidate:
            candidate = "attachment"

        if not _string_or_empty(file_spec.get("/F")):
            file_spec["/F"] = pikepdf.String(candidate)
            changes += 1
        if not _string_or_empty(file_spec.get("/UF")):
            file_spec["/UF"] = pikepdf.String(candidate)
            changes += 1

    return changes


def _normalize_type1_font_charsets(pdf: pikepdf.Pdf) -> int:
    """Remove stale CharSet entries from embedded Type1 font descriptors.

    CharSet is optional. When present but incomplete it triggers PDF/UA font
    failures, especially on subset fonts carried through OCR or producer
    pipelines. Removing an invalid CharSet is safer than keeping incorrect
    glyph metadata.
    """
    seen_fonts: set[tuple[int, int]] = set()
    changes = 0

    def _process_font(font_dict) -> None:
        nonlocal changes
        font_key = _obj_key(font_dict)
        if font_key and font_key in seen_fonts:
            return
        if font_key:
            seen_fonts.add(font_key)

        subtype = font_dict.get("/Subtype")
        if subtype not in (pikepdf.Name("/Type1"), pikepdf.Name("/MMType1")):
            return
        descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
        if not _has_embedded_font(descriptor):
            return
        if isinstance(descriptor, pikepdf.Dictionary) and pikepdf.Name("/CharSet") in descriptor:
            del descriptor[pikepdf.Name("/CharSet")]
            changes += 1

    for font_dict in _iter_pdf_fonts(pdf, policy=TAGGER_RESOURCES, object_key=_obj_key):
        _process_font(font_dict)

    return changes


def _normalize_media_clip_data_dicts(pdf: pikepdf.Pdf) -> int:
    """Backfill syntax-critical CT/Alt entries on media clip data dictionaries."""
    changes = 0
    seen_indirect: set[tuple[int, int]] = set()

    def _string_or_empty(value) -> str:
        if value is None:
            return ""
        try:
            text = str(value).strip()
        except Exception:
            return ""
        if text == "None":
            return ""
        if text.startswith("/"):
            text = text[1:]
        return re.sub(
            r"#([0-9A-Fa-f]{2})",
            lambda match: chr(int(match.group(1), 16)),
            text,
        )

    def _is_mapping(obj: object) -> TypeGuard[pikepdf.Dictionary | pikepdf.Stream]:
        return isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream))

    def _derive_media_clip_label(media_clip) -> str:
        label = _string_or_empty(media_clip.get("/N"))
        if label:
            return label
        data = media_clip.get("/D")
        if _is_mapping(data):
            for key in ("/UF", "/F", "/Desc"):
                label = _string_or_empty(data.get(key))
                if label:
                    return label
        return "Embedded media"

    def _derive_media_clip_type(media_clip) -> str | None:
        data = media_clip.get("/D")
        if not _is_mapping(data):
            return "application/octet-stream"
        try:
            if data.get("/Subtype") == pikepdf.Name("/Form"):
                return None
        except Exception:
            pass

        subtype = _string_or_empty(data.get("/Subtype"))
        if "/" in subtype:
            return subtype

        for key in ("/UF", "/F", "/Desc"):
            guess = _string_or_empty(data.get(key))
            if not guess:
                continue
            guessed_type, _ = mimetypes.guess_type(guess)
            if guessed_type:
                return guessed_type

        ef = data.get("/EF")
        if _is_mapping(ef):
            for key in ("/UF", "/F"):
                embedded_stream = ef.get(key)
                if not _is_mapping(embedded_stream):
                    continue
                stream_subtype = _string_or_empty(embedded_stream.get("/Subtype"))
                if "/" in stream_subtype:
                    return stream_subtype

        return "application/octet-stream"

    active_direct_refs: list[tuple[int, object]] = []
    stack: list[tuple[object | None, tuple[int, object] | None]] = [(pdf.Root, None)]

    def _is_active_direct_ref(candidate: object, candidate_id: int) -> bool:
        for active_id, active_obj in active_direct_refs:
            if candidate_id == active_id:
                return True
            try:
                if candidate == active_obj:
                    return True
            except Exception:
                continue
        return False

    while stack:
        obj, exit_mode = stack.pop()
        if exit_mode is not None:
            direct_id, _direct_ref = exit_mode
            active_direct_refs = [
                (active_id, active_obj)
                for active_id, active_obj in active_direct_refs
                if active_id != direct_id
            ]
            continue
        if obj is None:
            continue

        indirect_key = _obj_key(obj)
        if indirect_key:
            if indirect_key in seen_indirect:
                continue
            seen_indirect.add(indirect_key)
        elif isinstance(obj, (pikepdf.Array, pikepdf.Dictionary, pikepdf.Stream)):
            direct_id = id(obj)
            if _is_active_direct_ref(obj, direct_id):
                continue
            active_direct_refs.append((direct_id, obj))
            stack.append((None, (direct_id, obj)))

        if isinstance(obj, pikepdf.Array):
            for item in reversed(list(obj)):
                stack.append((item, None))
            continue
        if not _is_mapping(obj):
            continue

        try:
            is_media_clip_data = obj.get("/S") == pikepdf.Name("/MCD")
        except Exception:
            is_media_clip_data = False

        if is_media_clip_data:
            ct_value = _derive_media_clip_type(obj)
            if ct_value and not _string_or_empty(obj.get("/CT")):
                obj["/CT"] = pikepdf.String(ct_value)
                changes += 1
            alt = obj.get("/Alt")
            if not isinstance(alt, pikepdf.Array) or len(alt) < 2:
                obj["/Alt"] = pikepdf.Array([
                    pikepdf.String(""),
                    pikepdf.String(_derive_media_clip_label(obj)),
                ])
                changes += 1

        for _, value in reversed(list(obj.items())):
            stack.append((value, None))

    return changes


def _save_pdf_for_viewer_compatibility(pdf: pikepdf.Pdf, output_path: Path) -> None:
    """Save linearized output when possible; fall back to a normal save."""
    add_missing_icc_components(pdf)
    tmp_path = output_path.with_name(f"{output_path.name}.tmp")
    tmp_path.unlink(missing_ok=True)
    try:
        pdf.save(str(tmp_path), linearize=True)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        logger.warning("Could not save linearized PDF %s: %s", output_path.name, exc)
        pdf.save(str(output_path))
    else:
        tmp_path.replace(output_path)
