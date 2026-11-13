"""Embed local font programs or rewrite PDFs with Ghostscript."""

import asyncio
import logging
from pathlib import Path

from app.config import get_settings
from app.pipeline.fonts.encoding import (
    _encoding_differences_names,
    _standard_encoding_over_winansi,
)
from app.pipeline.fonts.hidden import SimpleFontUses, glyphless_program, simple_font_uses
from app.pipeline.fonts.names import _strip_subset_prefix
from app.pipeline.fonts.programs import (
    _ghostscript_type1_descriptor,
    _local_embed_support_kind,
    _local_font_program,
    _truetype_descriptor,
)
from app.pipeline.fonts.resources import (
    _has_embedded_font,
    _iter_pdf_fonts,
    _obj_key,
    _resolve_dictionary,
)
from app.pipeline.fonts.widths import (
    _align_truetype_advances,
    _derive_truetype_widths,
    _truetype_code_glyphs,
)
from app.pipeline.subprocess_utils import (
    SubprocessTimeout,
    communicate_with_timeout,
    subprocess_process_group_kwargs,
)
from app.services.runtime_paths import enriched_subprocess_env, resolve_binary
from app.services.thread_work import to_thread

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
        "fonts_glyphless": 0,
    }
    seen_fonts: set[tuple[int, int]] = set()
    font_uses: list[SimpleFontUses | None] = []

    def _uses() -> SimpleFontUses | None:
        """Where each simple font is shown, read once and only when needed."""
        if not font_uses:
            try:
                font_uses.append(simple_font_uses(pdf))
            except Exception:
                # Reading only; an unreadable document keeps every code shown.
                logger.warning("Could not read simple-font uses", exc_info=True)
                font_uses.append(None)
        return font_uses[0]

    def _shown_codes(font_dict) -> set[int] | None:
        uses = _uses()
        return uses.shown_codes(font_dict) if uses is not None else None

    def _restate(encoding, *, implicit_standard: bool, font_dict):
        restated = _standard_encoding_over_winansi(encoding, implicit_standard=implicit_standard)
        if restated is None:
            shown = _shown_codes(font_dict)
            if shown is not None:
                restated = _standard_encoding_over_winansi(
                    encoding, implicit_standard=implicit_standard, shown_codes=shown
                )
        return restated

    def _program_draws(font_dict, font_name: str, codes: set[int]) -> bool:
        """A local program exists and has a glyph for each code (Type 1: assumed)."""
        from io import BytesIO

        from fontTools.ttLib import TTFont

        font_bytes, _, fontfile_key, _ = _local_font_program(font_dict, font_name)
        if not font_bytes:
            return False
        if fontfile_key != "/FontFile2":
            return True
        with TTFont(BytesIO(font_bytes), lazy=True) as program:
            glyphs = _truetype_code_glyphs(font_dict, program)
        return all(glyphs.get(code, ".notdef") != ".notdef" for code in codes)

    def _embed_glyphless(font_dict, descriptor, font_name: str) -> bool:
        """Give a TrueType font whose text shows nothing an empty-glyph program.

        Its text is covered by the page scan, in rendering mode 3, or spaces
        (``hidden.py``), and no local program draws every code it shows. Its
        encoding must already be what PDF/UA requires of TrueType:
        WinAnsiEncoding or MacRomanEncoding, with AGL names in Differences.
        """
        from fontTools.agl import LEGACY_AGL2UV

        if not isinstance(descriptor, pikepdf.Dictionary) or _obj_key(font_dict) is None:
            return False
        uses = _uses()
        codes = uses.unseen_codes(font_dict) if uses is not None else None
        if not codes:
            return False
        encoding = font_dict.get("/Encoding")
        base = encoding.get("/BaseEncoding") if isinstance(encoding, pikepdf.Dictionary) else encoding
        if str(base) not in ("/WinAnsiEncoding", "/MacRomanEncoding"):
            return False
        if isinstance(encoding, pikepdf.Dictionary) and any(
            name not in LEGACY_AGL2UV
            for name in _encoding_differences_names(encoding.get("/Differences")).values()
        ):
            return False
        if _program_draws(font_dict, font_name, codes):
            return False
        program = glyphless_program(font_dict, codes)
        if program is None:
            return False
        descriptor = pikepdf.Dictionary(descriptor)
        descriptor[pikepdf.Name("/FontFile2")] = pdf.make_stream(program, Length1=len(program))
        font_dict[pikepdf.Name("/FontDescriptor")] = pdf.make_indirect(descriptor)
        stats["fonts_embedded"] += 1
        stats["fonts_glyphless"] += 1
        stats["fonts_touched"] += 1
        logger.info(f"Embedded an empty-glyph program for {font_name}: its text shows nothing")
        return True

    def _ensure_type1_descriptor(font_dict, descriptor, font_name: str, font_bytes, fontfile_key):
        if isinstance(descriptor, pikepdf.Dictionary):
            return descriptor
        if _local_embed_support_kind(font_dict) != "type1_standard14":
            return descriptor

        descriptor_data = (
            _truetype_descriptor(font_bytes) if fontfile_key == "/FontFile2"
            else _ghostscript_type1_descriptor(font_name)
        )
        if not descriptor_data:
            return descriptor

        descriptor = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/FontDescriptor"),
                "/FontName": pikepdf.Name(f"/{_strip_subset_prefix(font_name)}"),
                "/Flags": int(descriptor_data["Flags"]),
                "/ItalicAngle": float(descriptor_data["ItalicAngle"]),
                "/Ascent": int(descriptor_data["Ascent"]),
                "/Descent": int(descriptor_data["Descent"]),
                "/CapHeight": int(descriptor_data["CapHeight"]),
                "/StemV": int(descriptor_data["StemV"]),
                "/FontBBox": pikepdf.Array(descriptor_data["FontBBox"]),
            }
        )
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
        font_dict, descriptor, font_name: str, *, descendant_subtype=None, descriptor_owner=None
    ) -> bool:
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

        descriptor = _ensure_type1_descriptor(
            font_dict, descriptor, font_name, font_bytes, fontfile_key,
        )
        if not isinstance(descriptor, pikepdf.Dictionary):
            stats["fonts_unsupported"] += 1
            return False
        symbolic = int(descriptor.get("/Flags") or 0) & 4
        if fontfile_key == "/FontFile2" and font_dict.get("/Subtype") in (
            pikepdf.Name("/Type1"), pikepdf.Name("/MMType1"),
        ):
            # A TrueType program requires a TrueType dictionary. A Type 1
            # font's implicit base is StandardEncoding; state the same glyph
            # names over WinAnsiEncoding, as PDF/UA requires of TrueType.
            encoding = font_dict.get("/Encoding")
            restated = (
                None
                if symbolic
                else _restate(encoding, implicit_standard=True, font_dict=font_dict)
            )
            if restated is not None:
                font_dict["/Encoding"] = restated
            elif encoding is None:
                font_dict["/Encoding"] = pikepdf.Name("/StandardEncoding")
            elif isinstance(encoding, pikepdf.Dictionary) and "/BaseEncoding" not in encoding:
                encoding = pikepdf.Dictionary(encoding)
                encoding["/BaseEncoding"] = pikepdf.Name("/StandardEncoding")
                font_dict["/Encoding"] = encoding
            font_dict["/Subtype"] = pikepdf.Name("/TrueType")
        elif (
            fontfile_key == "/FontFile2"
            and font_dict.get("/Subtype") == pikepdf.Name("/TrueType")
            and not symbolic
        ):
            restated = _restate(
                font_dict.get("/Encoding"), implicit_standard=False, font_dict=font_dict
            )
            if restated is not None:
                font_dict["/Encoding"] = restated
        if fontfile_key == "/FontFile2" and font_dict.get("/Subtype") == pikepdf.Name("/TrueType"):
            _derive_truetype_widths(font_dict, font_bytes)
            font_bytes, _, _ = _align_truetype_advances(font_dict, font_bytes)
            length_info = {**length_info, "Length1": len(font_bytes)}
        stream = pdf.make_stream(font_bytes)
        for key, value in length_info.items():
            stream[pikepdf.Name(f"/{key}")] = int(value)
        # Each dictionary can have different widths/encoding even when its
        # original descriptor is shared. Keep the aligned program local to
        # this use; otherwise later fonts inherit the first use's advances.
        descriptor = pikepdf.Dictionary(descriptor)
        descriptor[pikepdf.Name(fontfile_key)] = stream
        owner = font_dict if descriptor_owner is None else descriptor_owner
        owner[pikepdf.Name("/FontDescriptor")] = pdf.make_indirect(descriptor)
        if fontfile_key == "/FontFile" and font_dict.get("/Subtype") == pikepdf.Name("/TrueType"):
            # URW base-35 substitutes are Type 1 programs. Retain the source's
            # character encoding, ToUnicode, and explicit PDF widths verbatim.
            font_dict[pikepdf.Name("/Subtype")] = pikepdf.Name("/Type1")
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
            if not _has_embedded_font(descriptor) and _embed_glyphless(
                font_dict, descriptor, font_name
            ):
                return
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
            descriptor_owner=cid_font,
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
                f"CIDToGIDMap added={stats['cidtogid_added']}, "
                f"empty-glyph programs for unseen text={stats['fonts_glyphless']})"
            ),
            stats,
        )
    except Exception as exc:
        return False, f"Local font embedding failed: {exc}", stats


async def _embed_system_fonts(
    input_path: Path,
    output_path: Path,
) -> tuple[bool, str, dict[str, int]]:
    return await to_thread(_embed_system_fonts_sync, input_path, output_path)


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
