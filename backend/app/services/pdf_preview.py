import base64
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

import pikepdf
from PIL import Image, ImageDraw

from app.config import get_settings
from app.services.pdf_operator_context import extract_operator_visual_context
from app.services.runtime_paths import enriched_subprocess_env, resolve_binary

RENDER_DPI = 144
MAX_IMAGE_WIDTH = 1400
SEMANTIC_RENDER_DPI = 110
SEMANTIC_MAX_IMAGE_WIDTH = 1100
SEMANTIC_JPEG_QUALITY = 72
TARGET_MIN_BOX_PX = 18
TARGET_BOX_PADDING_PX = 6
TARGET_OUTLINE_COLOR = (220, 38, 38, 255)
TARGET_FILL_COLOR = (251, 191, 36, 72)
TARGET_DIM_COLOR = (0, 0, 0, 56)
TARGET_CENTER_COLOR = (220, 38, 38, 255)


def pdftoppm_path() -> str:
    binary = resolve_binary("pdftoppm", explicit=get_settings().pdftoppm_path)
    if not binary:
        raise RuntimeError("pdftoppm is required for PDF page previews but was not found")
    return binary


def render_page_png_bytes(
    pdf_path: Path,
    page_number: int,
    *,
    dpi: int = RENDER_DPI,
    max_width: int = MAX_IMAGE_WIDTH,
    timeout: int = 30,
) -> bytes:
    if page_number < 1:
        raise ValueError("page_number must be 1 or greater")
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    with tempfile.TemporaryDirectory(prefix="pdf-preview-") as tmp_dir:
        output_prefix = Path(tmp_dir) / f"page_{page_number}"
        cmd = [
            pdftoppm_path(),
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-r",
            str(dpi),
            "-png",
            "-singlefile",
            str(pdf_path),
            str(output_prefix),
        ]
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                timeout=timeout,
                env=enriched_subprocess_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Page preview timed out after {timeout}s"
            ) from exc
        image_path = output_prefix.with_suffix(".png")
        with Image.open(image_path) as image:
            rendered = image.convert("RGB")
            if rendered.width > max_width:
                scale = max_width / max(rendered.width, 1)
                rendered = rendered.resize(
                    (max_width, max(1, int(rendered.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            output_bytes = tempfile.SpooledTemporaryFile()
            rendered.save(output_bytes, format="PNG")
            output_bytes.seek(0)
            return output_bytes.read()


def render_page_jpeg_bytes(
    pdf_path: Path,
    page_number: int,
    *,
    dpi: int = SEMANTIC_RENDER_DPI,
    max_width: int = SEMANTIC_MAX_IMAGE_WIDTH,
    quality: int = SEMANTIC_JPEG_QUALITY,
    timeout: int = 30,
) -> bytes:
    png_bytes = render_page_png_bytes(
        pdf_path,
        page_number,
        dpi=dpi,
        max_width=max_width,
        timeout=timeout,
    )
    with Image.open(BytesIO(png_bytes)) as image:
        rendered = image.convert("RGB")
        output_bytes = tempfile.SpooledTemporaryFile()
        rendered.save(
            output_bytes,
            format="JPEG",
            quality=max(1, min(int(quality), 95)),
            optimize=True,
        )
        output_bytes.seek(0)
        return output_bytes.read()


def render_page_jpeg_data_url(
    pdf_path: Path,
    page_number: int,
    *,
    dpi: int = SEMANTIC_RENDER_DPI,
    max_width: int = SEMANTIC_MAX_IMAGE_WIDTH,
    quality: int = SEMANTIC_JPEG_QUALITY,
    timeout: int = 30,
) -> str:
    encoded = base64.b64encode(
        render_page_jpeg_bytes(
            pdf_path,
            page_number,
            dpi=dpi,
            max_width=max_width,
            quality=quality,
            timeout=timeout,
        )
    ).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _page_dimensions_points(pdf_path: Path, page_number: int) -> tuple[float, float]:
    with pikepdf.Pdf.open(pdf_path) as pdf:
        if page_number < 1 or page_number > len(pdf.pages):
            raise ValueError("page_number is out of range for this PDF")
        mediabox = pdf.pages[page_number - 1].mediabox
        return float(mediabox[2]) - float(mediabox[0]), float(mediabox[3]) - float(mediabox[1])


def _clamp_box(box: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    left = max(0, min(left, width))
    right = max(0, min(right, width))
    top = max(0, min(top, height))
    bottom = max(0, min(bottom, height))
    return left, top, right, bottom


def _expand_focus_box(
    box: tuple[int, int, int, int],
    *,
    width: int,
    height: int,
    min_size: int = TARGET_MIN_BOX_PX,
    padding: int = TARGET_BOX_PADDING_PX,
) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    current_width = max(right - left, 1)
    current_height = max(bottom - top, 1)

    if current_width < min_size:
        delta = min_size - current_width
        left -= delta // 2
        right += delta - delta // 2
    if current_height < min_size:
        delta = min_size - current_height
        top -= delta // 2
        bottom += delta - delta // 2

    left -= padding
    right += padding
    top -= padding
    bottom += padding
    return _clamp_box((left, top, right, bottom), width, height)


def _draw_target_focus(
    image: Image.Image,
    *,
    exact_box: tuple[int, int, int, int],
    focus_box: tuple[int, int, int, int],
) -> Image.Image:
    rendered = image.convert("RGBA")
    overlay = Image.new("RGBA", rendered.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = rendered.size
    left, top, right, bottom = focus_box

    if top > 0:
        draw.rectangle((0, 0, width, top), fill=TARGET_DIM_COLOR)
    if bottom < height:
        draw.rectangle((0, bottom, width, height), fill=TARGET_DIM_COLOR)
    if left > 0:
        draw.rectangle((0, top, left, bottom), fill=TARGET_DIM_COLOR)
    if right < width:
        draw.rectangle((right, top, width, bottom), fill=TARGET_DIM_COLOR)

    draw.rectangle(focus_box, fill=TARGET_FILL_COLOR, outline=TARGET_OUTLINE_COLOR, width=3)

    exact_left, exact_top, exact_right, exact_bottom = exact_box
    draw.rectangle(exact_box, outline=TARGET_OUTLINE_COLOR, width=2)

    center_x = (exact_left + exact_right) / 2.0
    center_y = (exact_top + exact_bottom) / 2.0
    marker_half = 6
    draw.line(
        (
            center_x - marker_half,
            center_y,
            center_x + marker_half,
            center_y,
        ),
        fill=TARGET_CENTER_COLOR,
        width=2,
    )
    draw.line(
        (
            center_x,
            center_y - marker_half,
            center_x,
            center_y + marker_half,
        ),
        fill=TARGET_CENTER_COLOR,
        width=2,
    )

    return Image.alpha_composite(rendered, overlay).convert("RGB")


def render_target_preview_png_bytes(
    pdf_path: Path,
    context_path: str,
    *,
    dpi: int = RENDER_DPI,
    max_width: int = MAX_IMAGE_WIDTH,
    crop_margin_points: float = 24.0,
) -> bytes:
    visual = extract_operator_visual_context(pdf_path=pdf_path, context_path=context_path)
    page_number = int(visual.get("page_number") or 1)
    page_bytes = render_page_png_bytes(pdf_path, page_number, dpi=dpi, max_width=max_width)

    bbox = visual.get("bbox")
    page_width = float(visual.get("page_width") or 0.0)
    page_height = float(visual.get("page_height") or 0.0)
    if not isinstance(bbox, dict) or page_width <= 0 or page_height <= 0:
        return page_bytes

    scale_x = dpi / 72.0
    scale_y = dpi / 72.0
    if page_width * scale_x > max_width:
        resize_scale = max_width / max(page_width * scale_x, 1.0)
        scale_x *= resize_scale
        scale_y *= resize_scale

    margin_x = crop_margin_points * scale_x
    margin_y = crop_margin_points * scale_y

    with Image.open(BytesIO(page_bytes)) as image:
        rendered = image.convert("RGB")
        image_width, image_height = rendered.size
        exact_box = _clamp_box(
            (
                int(round(bbox["l"] * scale_x)),
                int(round((page_height - bbox["t"]) * scale_y)),
                int(round(bbox["r"] * scale_x)),
                int(round((page_height - bbox["b"]) * scale_y)),
            ),
            image_width,
            image_height,
        )
        focus_box = _expand_focus_box(
            exact_box,
            width=image_width,
            height=image_height,
        )

        x0 = max(0, int(round(focus_box[0] - margin_x)))
        x1 = min(image_width, int(round(focus_box[2] + margin_x)))
        y0 = max(0, int(round(focus_box[1] - margin_y)))
        y1 = min(image_height, int(round(focus_box[3] + margin_y)))

        if x1 <= x0 or y1 <= y0:
            crop = _draw_target_focus(
                rendered,
                exact_box=exact_box,
                focus_box=focus_box,
            )
        else:
            exact_crop_box = (
                exact_box[0] - x0,
                exact_box[1] - y0,
                exact_box[2] - x0,
                exact_box[3] - y0,
            )
            focus_crop_box = (
                focus_box[0] - x0,
                focus_box[1] - y0,
                focus_box[2] - x0,
                focus_box[3] - y0,
            )
            crop = _draw_target_focus(
                rendered.crop((x0, y0, x1, y1)),
                exact_box=exact_crop_box,
                focus_box=focus_crop_box,
            )

        output = BytesIO()
        crop.save(output, format="PNG")
        return output.getvalue()


def render_target_preview_png_data_url(pdf_path: Path, context_path: str) -> str:
    encoded = base64.b64encode(render_target_preview_png_bytes(pdf_path, context_path)).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render_bbox_preview_png_bytes(
    pdf_path: Path,
    page_number: int,
    bbox: dict[str, float],
    *,
    dpi: int = RENDER_DPI,
    max_width: int = MAX_IMAGE_WIDTH,
    crop_margin_points: float = 24.0,
    highlight: bool = True,
) -> bytes:
    if not isinstance(bbox, dict):
        raise ValueError("bbox must be a mapping")

    page_width, page_height = _page_dimensions_points(pdf_path, page_number)
    page_bytes = render_page_png_bytes(pdf_path, page_number, dpi=dpi, max_width=max_width)

    scale_x = dpi / 72.0
    scale_y = dpi / 72.0
    if page_width * scale_x > max_width:
        resize_scale = max_width / max(page_width * scale_x, 1.0)
        scale_x *= resize_scale
        scale_y *= resize_scale

    margin_x = crop_margin_points * scale_x
    margin_y = crop_margin_points * scale_y

    with Image.open(BytesIO(page_bytes)) as image:
        rendered = image.convert("RGB")
        image_width, image_height = rendered.size
        exact_box = _clamp_box(
            (
                int(round(float(bbox["l"]) * scale_x)),
                int(round((page_height - float(bbox["t"])) * scale_y)),
                int(round(float(bbox["r"]) * scale_x)),
                int(round((page_height - float(bbox["b"])) * scale_y)),
            ),
            image_width,
            image_height,
        )
        focus_box = _expand_focus_box(
            exact_box,
            width=image_width,
            height=image_height,
        )

        x0 = max(0, int(round(focus_box[0] - margin_x)))
        x1 = min(image_width, int(round(focus_box[2] + margin_x)))
        y0 = max(0, int(round(focus_box[1] - margin_y)))
        y1 = min(image_height, int(round(focus_box[3] + margin_y)))

        if x1 <= x0 or y1 <= y0:
            crop = rendered
            if highlight:
                crop = _draw_target_focus(
                    rendered,
                    exact_box=exact_box,
                    focus_box=focus_box,
                )
        else:
            exact_crop_box = (
                exact_box[0] - x0,
                exact_box[1] - y0,
                exact_box[2] - x0,
                exact_box[3] - y0,
            )
            focus_crop_box = (
                focus_box[0] - x0,
                focus_box[1] - y0,
                focus_box[2] - x0,
                focus_box[3] - y0,
            )
            crop = rendered.crop((x0, y0, x1, y1))
            if highlight:
                crop = _draw_target_focus(
                    crop,
                    exact_box=exact_crop_box,
                    focus_box=focus_crop_box,
                )

        output = BytesIO()
        crop.save(output, format="PNG")
        return output.getvalue()


def render_bbox_preview_png_data_url(
    pdf_path: Path,
    page_number: int,
    bbox: dict[str, float],
    *,
    highlight: bool = True,
) -> str:
    encoded = base64.b64encode(
        render_bbox_preview_png_bytes(pdf_path, page_number, bbox, highlight=highlight)
    ).decode("ascii")
    return f"data:image/png;base64,{encoded}"


# Glyph identification previews. Vision models misread small details, and
# cropping and zooming to them recovers accuracy (Zhang et al., "MLLMs Know
# Where to Look", ICLR 2025). A thin red circle directs attention without
# covering what it marks (Shtedritski et al., "What does CLIP know about a red
# circle?", ICCV 2023). One em at GLYPH_EM_PX puts capitals near the 25-35 px
# Willus Dotkom found best for Tesseract. As in pdf-glyph-mapping, the glyph
# is also shown alone.
GLYPH_EM_PX = 48
GLYPH_MAX_DPI = 1200
GLYPH_MAX_REGION_PX = 1400
GLYPH_INK_THRESHOLD = 24
GLYPH_MAX_EXTENT_EM = 8.0


def _render_png_image(
    pdf_path: Path,
    page_number: int,
    dpi: float,
    region: tuple[int, int, int, int] | None = None,
    timeout: int = 30,
) -> Image.Image:
    with tempfile.TemporaryDirectory(prefix="glyph-preview-") as tmp_dir:
        output_prefix = Path(tmp_dir) / "page"
        crop = []
        if region is not None:
            x, y, width, height = region
            crop = ["-x", str(x), "-y", str(y), "-W", str(width), "-H", str(height)]
        subprocess.run(
            [
                pdftoppm_path(),
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-r",
                f"{dpi:.2f}",
                *crop,
                "-png",
                "-singlefile",
                str(pdf_path),
                str(output_prefix),
            ],
            check=True,
            capture_output=True,
            timeout=timeout,
            env=enriched_subprocess_env(),
        )
        with Image.open(output_prefix.with_suffix(".png")) as image:
            return image.convert("RGB")


def _hide_target_glyph(pdf_path: Path, context_path: str, output_path: Path) -> float:
    """Save a copy that draws the target operator in render mode 3 (invisible).

    Returns the operator's em in points: font size times the scale of the
    text and current transformation matrices.
    """
    import math

    from app.pipeline.tagger import IDENTITY, _mat_multiply, _safe_float
    from app.services.font_actualtext import (
        _resolve_object,
        _resolve_target_stream_from_context,
        _resolve_text_showing_instruction,
    )

    with pikepdf.open(str(pdf_path)) as pdf:
        resolved = _resolve_target_stream_from_context(pdf, context_path)
        target_owner, target_stream = resolved["target_owner"], resolved["target_stream"]
        instructions = list(pikepdf.parse_content_stream(target_stream or target_owner))
        index = int(
            _resolve_text_showing_instruction(instructions, int(resolved["operator_index"]))[
                "text_operator_index"
            ]
        )
        mode, size, ctm, text_matrix = 0, 12.0, IDENTITY, IDENTITY
        stack: list[tuple[int, float, tuple]] = []
        for instruction in instructions[:index]:
            op, operands = str(instruction.operator), list(instruction.operands)
            if op == "q":
                stack.append((mode, size, ctm))
            elif op == "Q" and stack:
                mode, size, ctm = stack.pop()
            elif op == "cm" and len(operands) >= 6:
                ctm = _mat_multiply(tuple(_safe_float(x) for x in operands[:6]), ctm)
            elif op == "Tr" and operands:
                mode = int(_safe_float(operands[0]))
            elif op == "Tf" and len(operands) >= 2:
                size = abs(_safe_float(operands[1]))
            elif op == "BT":
                text_matrix = IDENTITY
            elif op == "Tm" and len(operands) >= 6:
                text_matrix = tuple(_safe_float(x) for x in operands[:6])
        a, b, c, d, _, _ = _mat_multiply(text_matrix, ctm)
        em = size * math.sqrt(abs(a * d - b * c))
        rewritten = [
            *instructions[:index],
            pikepdf.ContentStreamInstruction([3], pikepdf.Operator("Tr")),
            instructions[index],
            pikepdf.ContentStreamInstruction([mode], pikepdf.Operator("Tr")),
            *instructions[index + 1 :],
        ]
        data = pikepdf.unparse_content_stream(rewritten)
        if target_stream is None and isinstance(target_owner, pikepdf.Page):
            target_owner["/Contents"] = pdf.make_stream(data)
        else:
            _resolve_object(target_stream or target_owner).write(data)
        pdf.save(str(output_path))
    return em


def _ink_mask(shown: Image.Image, hidden: Image.Image) -> Image.Image:
    from PIL import ImageChops

    difference = ImageChops.difference(shown, hidden).convert("L")
    return difference.point(lambda value: 255 if value > GLYPH_INK_THRESHOLD else 0)


def _glyph_preview_images(
    pdf_path: Path, context_path: str
) -> tuple[Image.Image, Image.Image, tuple[int, int, int, int], float] | None:
    """A zoomed crop with the target glyph circled, the glyph drawn alone, the
    ink box in the crop, and the em in crop pixels.

    The glyph's ink is what differs between the page as drawn and the page
    with the target operator invisible, so its box is exact for any font,
    transform or clip. None when the operator draws no ink of its own size.
    """
    from app.services.pdf_context import parse_verapdf_context_path

    page_number = parse_verapdf_context_path(context_path).get("page_number")
    if not isinstance(page_number, int) or page_number < 1:
        return None
    with tempfile.TemporaryDirectory(prefix="glyph-hidden-") as tmp_dir:
        hidden_pdf = Path(tmp_dir) / "hidden.pdf"
        em = _hide_target_glyph(pdf_path, context_path, hidden_pdf)
        shown = _render_png_image(pdf_path, page_number, RENDER_DPI)
        hidden = _render_png_image(hidden_pdf, page_number, RENDER_DPI)
        if shown.size != hidden.size:
            return None
        box = _ink_mask(shown, hidden).getbbox()
        if box is None:
            return None
        to_points = 72.0 / RENDER_DPI
        left, top, right, bottom = (value * to_points for value in box)
        if em <= 0:
            em = bottom - top
        if max(right - left, bottom - top) > GLYPH_MAX_EXTENT_EM * em:
            return None
        page_width, page_height = (value * to_points for value in shown.size)
        x0, x1 = max(0.0, left - 3 * em), min(page_width, right + 3 * em)
        y0, y1 = max(0.0, top - 1.25 * em), min(page_height, bottom + 1.25 * em)
        dpi = min(max(72.0 * GLYPH_EM_PX / em, RENDER_DPI), GLYPH_MAX_DPI)
        dpi = min(dpi, 72.0 * GLYPH_MAX_REGION_PX / max(x1 - x0, 1.0))
        scale = dpi / 72.0
        region = (
            round(x0 * scale),
            round(y0 * scale),
            max(1, round((x1 - x0) * scale)),
            max(1, round((y1 - y0) * scale)),
        )
        shown_region = _render_png_image(pdf_path, page_number, dpi, region)
        hidden_region = _render_png_image(hidden_pdf, page_number, dpi, region)
    if shown_region.size != hidden_region.size:
        return None
    mask = _ink_mask(shown_region, hidden_region)
    ink = mask.getbbox()
    if ink is None:
        return None

    # The circle passes outside the ink's corners, with a gap.
    em_px = em * scale
    gap = max(3.0, 0.12 * em_px)
    center_x, center_y = (ink[0] + ink[2]) / 2.0, (ink[1] + ink[3]) / 2.0
    radius_x = (ink[2] - ink[0]) / 2.0 * 2**0.5 + gap
    radius_y = (ink[3] - ink[1]) / 2.0 * 2**0.5 + gap
    circled = shown_region.copy()
    ImageDraw.Draw(circled).ellipse(
        (center_x - radius_x, center_y - radius_y, center_x + radius_x, center_y + radius_y),
        outline=TARGET_OUTLINE_COLOR[:3],
        width=max(2, round(0.04 * em_px)),
    )

    pad = max(4, round(0.25 * em_px))
    alone = Image.new("RGB", shown_region.size, (255, 255, 255))
    alone.paste(shown_region, mask=mask)
    alone = alone.crop((ink[0] - pad, ink[1] - pad, ink[2] + pad, ink[3] + pad))

    return circled, alone, ink, em_px


def render_glyph_previews_png_bytes(
    pdf_path: Path, context_path: str
) -> tuple[bytes, bytes] | None:
    """PNG bytes of the circled crop and the glyph alone (see _glyph_preview_images)."""
    previews = _glyph_preview_images(pdf_path, context_path)
    if previews is None:
        return None

    def _png(image: Image.Image) -> bytes:
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    return _png(previews[0]), _png(previews[1])


def render_target_previews_png_data_urls(
    pdf_path: Path, context_path: str
) -> list[tuple[str, str]]:
    """(description, data URL) for each preview image of a font review target."""
    try:
        glyph = render_glyph_previews_png_bytes(pdf_path, context_path)
    except Exception:
        glyph = None
    if glyph is None:
        return [("target region", render_target_preview_png_data_url(pdf_path, context_path))]
    circled, alone = (base64.b64encode(data).decode("ascii") for data in glyph)
    return [
        ("target glyph circled in red, zoomed", f"data:image/png;base64,{circled}"),
        ("target glyph drawn alone", f"data:image/png;base64,{alone}"),
    ]
