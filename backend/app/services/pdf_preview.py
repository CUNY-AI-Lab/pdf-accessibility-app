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
