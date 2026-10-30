"""Embed local font programs or rewrite PDFs with Ghostscript."""

import asyncio
import logging
from pathlib import Path

from app.config import get_settings
from app.pipeline.fonts.names import _strip_subset_prefix
from app.pipeline.fonts.programs import (
    _ghostscript_type1_descriptor,
    _local_embed_support_kind,
    _local_font_program,
)
from app.pipeline.fonts.resources import (
    _has_embedded_font,
    _iter_pdf_fonts,
    _obj_key,
    _resolve_dictionary,
)
from app.pipeline.subprocess_utils import (
    SubprocessTimeout,
    communicate_with_timeout,
    subprocess_process_group_kwargs,
)
from app.services.runtime_paths import enriched_subprocess_env, resolve_binary

logger = logging.getLogger("app.pipeline.orchestrator")


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
    seen_fonts: set[tuple[int, int]] = set()

    def _ensure_type1_descriptor(font_dict, descriptor, font_name: str):
        if isinstance(descriptor, pikepdf.Dictionary):
            return descriptor
        if _local_embed_support_kind(font_dict) != "type1_standard14":
            return descriptor

        descriptor_data = _ghostscript_type1_descriptor(font_name)
        if not descriptor_data:
            return descriptor

        descriptor = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/FontDescriptor"),
                "/FontName": pikepdf.Name(f"/{_strip_subset_prefix(font_name)}"),
                "/Flags": int(descriptor_data["Flags"]),
                "/ItalicAngle": int(descriptor_data["ItalicAngle"]),
                "/Ascent": int(descriptor_data["Ascent"]),
                "/Descent": int(descriptor_data["Descent"]),
                "/CapHeight": int(descriptor_data["CapHeight"]),
                "/StemV": int(descriptor_data["StemV"]),
                "/FontBBox": pikepdf.Array(descriptor_data["FontBBox"]),
            }
        )
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
            if (
                isinstance(descriptor, pikepdf.Dictionary)
                and pikepdf.Name("/MissingWidth") not in descriptor
            ):
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
            descriptor[pikepdf.Name("/MissingWidth")] = int(
                descriptor_data.get("MissingWidth", 0) or 0
            )

    def _embed_descriptor_font(
        font_dict, descriptor, font_name: str, *, descendant_subtype=None
    ) -> bool:
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
            descriptor[pikepdf.Name("/FontName")] = pikepdf.Name(
                f"/{_strip_subset_prefix(font_name)}"
            )
        stats["fonts_embedded"] += 1
        stats["fonts_touched"] += 1
        logger.info(f"Embedded local font program for {font_name} using {matched_name}")
        return True

    def _process_font(font_dict) -> None:
        font_key = _obj_key(font_dict)
        if font_key and font_key in seen_fonts:
            return
        if font_key:
            seen_fonts.add(font_key)

        subtype = font_dict.get("/Subtype")
        if subtype == pikepdf.Name("/TrueType"):
            descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
            font_name = str(
                font_dict.get("/BaseFont")
                or (
                    descriptor.get("/FontName")
                    if isinstance(descriptor, pikepdf.Dictionary)
                    else ""
                )
            )
            _embed_descriptor_font(font_dict, descriptor, font_name)
            return

        if subtype in (pikepdf.Name("/Type1"), pikepdf.Name("/MMType1")):
            descriptor = _resolve_dictionary(font_dict.get("/FontDescriptor"))
            font_name = str(
                font_dict.get("/BaseFont")
                or (
                    descriptor.get("/FontName")
                    if isinstance(descriptor, pikepdf.Dictionary)
                    else ""
                )
            )
            changed = _embed_descriptor_font(font_dict, descriptor, font_name)
            if not changed and _local_embed_support_kind(font_dict) != "type1_standard14":
                stats["fonts_unsupported"] += 1
            return

        if subtype != pikepdf.Name("/Type0"):
            stats["fonts_unsupported"] += 1
            return

        descendants = font_dict.get("/DescendantFonts")
        if not isinstance(descendants, pikepdf.Array) or not descendants:
            stats["fonts_unsupported"] += 1
            return
        cid_font = _resolve_dictionary(descendants[0])
        if not isinstance(cid_font, pikepdf.Dictionary):
            stats["fonts_unsupported"] += 1
            return
        if cid_font.get("/Subtype") != pikepdf.Name("/CIDFontType2"):
            stats["fonts_unsupported"] += 1
            return

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

    try:
        with pikepdf.open(str(input_path)) as pdf:
            for font_dict in _iter_pdf_fonts(pdf):
                _process_font(font_dict)

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
    distiller_params = f"<< /NeverEmbed [ ] /AlwaysEmbed [ {always_embed} ] >> setdistillerparams"
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
        **subprocess_process_group_kwargs(),
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
