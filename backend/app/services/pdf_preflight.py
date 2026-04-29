from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pikepdf


@dataclass(frozen=True)
class PdfUploadPreflightReport:
    page_count: int
    max_page_render_pixels: int
    max_image_pixels: int
    total_image_pixels: int
    image_heavy_pages: int


class PdfUploadPreflightError(ValueError):
    def __init__(
        self,
        detail: str,
        *,
        status_code: int = 413,
        report: PdfUploadPreflightReport | None = None,
    ):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.report = report


def _setting_int(settings: Any, name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def _megapixels(pixels: int) -> str:
    return f"{pixels / 1_000_000:.0f} MP"


def _object_key(obj: Any) -> tuple[int, int] | int:
    try:
        objgen = tuple(obj.objgen)
        return objgen if objgen != (0, 0) else id(obj)
    except Exception:
        return id(obj)


def _image_pixels(image: Any) -> int:
    try:
        width = int(image.get("/Width", 0) or 0)
        height = int(image.get("/Height", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, width) * max(0, height)


def _iter_resource_image_pixels(resources: Any, *, visited: set[tuple[int, int] | int]):
    if not hasattr(resources, "get"):
        return

    xobjects = resources.get("/XObject")
    if not hasattr(xobjects, "values"):
        return

    for xobject in xobjects.values():
        if not hasattr(xobject, "get"):
            continue

        key = _object_key(xobject)
        if key in visited:
            continue
        visited.add(key)

        subtype = xobject.get("/Subtype")
        if subtype == pikepdf.Name("/Image"):
            yield _image_pixels(xobject)
        elif subtype == pikepdf.Name("/Form"):
            yield from _iter_resource_image_pixels(
                xobject.get("/Resources"),
                visited=visited,
            )


def _page_render_pixels(page: pikepdf.Page, *, dpi: int) -> int:
    try:
        mediabox = page.mediabox
        width_points = max(0.0, float(mediabox[2]) - float(mediabox[0]))
        height_points = max(0.0, float(mediabox[3]) - float(mediabox[1]))
    except Exception:
        return 0

    width_pixels = width_points / 72.0 * dpi
    height_pixels = height_points / 72.0 * dpi
    return int(width_pixels * height_pixels)


def inspect_pdf_upload(path: Path, *, settings: Any) -> PdfUploadPreflightReport:
    dpi = _setting_int(settings, "upload_preflight_render_dpi", 300)

    try:
        with pikepdf.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            max_page_render_pixels = 0
            max_image_pixels = 0
            total_image_pixels = 0
            image_heavy_pages = 0
            image_heavy_page_min_pixels = _setting_int(
                settings,
                "upload_image_heavy_page_min_pixels",
                4_000_000,
            )

            for page in pdf.pages:
                max_page_render_pixels = max(
                    max_page_render_pixels,
                    _page_render_pixels(page, dpi=dpi),
                )
                resources = (
                    page.get("/Resources", {})
                    if hasattr(page, "get")
                    else page.obj.get("/Resources")
                )
                page_image_pixels = sum(
                    _iter_resource_image_pixels(
                        resources,
                        visited=set(),
                    )
                )
                total_image_pixels += page_image_pixels
                max_image_pixels = max(max_image_pixels, page_image_pixels)
                if page_image_pixels >= image_heavy_page_min_pixels:
                    image_heavy_pages += 1
    except pikepdf.PdfError as exc:
        raise PdfUploadPreflightError(
            "File is not a readable PDF",
            status_code=400,
        ) from exc

    return PdfUploadPreflightReport(
        page_count=page_count,
        max_page_render_pixels=max_page_render_pixels,
        max_image_pixels=max_image_pixels,
        total_image_pixels=total_image_pixels,
        image_heavy_pages=image_heavy_pages,
    )


def preflight_pdf_upload(path: Path, *, settings: Any) -> PdfUploadPreflightReport:
    report = inspect_pdf_upload(path, settings=settings)

    max_pages = _setting_int(settings, "max_upload_pages", 300)
    max_page_render_pixels = _setting_int(settings, "max_upload_page_render_pixels", 75_000_000)
    max_image_pixels = _setting_int(settings, "max_upload_image_pixels", 75_000_000)
    max_total_image_pixels = _setting_int(
        settings,
        "max_upload_total_image_pixels",
        1_000_000_000,
    )
    max_image_heavy_pages = _setting_int(settings, "max_upload_image_heavy_pages", 75)

    reasons: list[str] = []
    if report.page_count > max_pages:
        reasons.append(f"{report.page_count} pages exceeds the {max_pages}-page limit")
    if report.max_page_render_pixels > max_page_render_pixels:
        reasons.append(
            f"the largest page would render at {_megapixels(report.max_page_render_pixels)}, "
            f"above the {_megapixels(max_page_render_pixels)} limit"
        )
    if report.max_image_pixels > max_image_pixels:
        reasons.append(
            f"the largest page embeds {_megapixels(report.max_image_pixels)} of image data, "
            f"above the {_megapixels(max_image_pixels)} limit"
        )
    if report.total_image_pixels > max_total_image_pixels:
        reasons.append(
            f"embedded image data totals {_megapixels(report.total_image_pixels)}, "
            f"above the {_megapixels(max_total_image_pixels)} limit"
        )
    if report.image_heavy_pages > max_image_heavy_pages:
        reasons.append(
            f"{report.image_heavy_pages} image-heavy pages exceeds the "
            f"{max_image_heavy_pages}-page self-service limit"
        )

    if reasons:
        raise PdfUploadPreflightError(
            "This PDF is too large or image-heavy for self-service processing: "
            + "; ".join(reasons)
            + ". Split it into smaller sections, downsample scans to about 300 DPI, "
            "or contact the CUNY AI Lab for assisted remediation.",
            report=report,
        )

    return report
