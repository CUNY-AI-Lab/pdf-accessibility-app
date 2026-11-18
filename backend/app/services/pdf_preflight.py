import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pikepdf

OCR_SCAN_METADATA_MARKERS = ("ocrmypdf", "paper capture", "tesseract")


@dataclass(frozen=True)
class PdfUploadPreflightReport:
    page_count: int
    max_page_render_pixels: int
    max_image_pixels: int
    total_image_pixels: int
    image_heavy_pages: int
    creator: str = ""
    producer: str = ""
    has_structure_tree: bool = False
    has_acroform: bool = False
    # Image pixels a renderer must also decode: soft masks and images inside
    # annotation appearances. Limits use these; classification does not.
    max_workload_image_pixels: int = 0
    total_workload_image_pixels: int = 0

    @property
    def is_ocr_scan_like(self) -> bool:
        metadata = f"{self.creator} {self.producer}".casefold()
        return any(marker in metadata for marker in OCR_SCAN_METADATA_MARKERS)


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


def _metadata_text(info: Any, key: str) -> str:
    try:
        value = info.get(key, "")
    except Exception:
        return ""
    return str(value or "").strip()


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
        # /UserUnit makes each unit that many points (PDF 1.6), as previews render it.
        unit = max(1.0, float(page.obj.get("/UserUnit", 1)))
        width_points = max(0.0, float(mediabox[2]) - float(mediabox[0])) * unit
        height_points = max(0.0, float(mediabox[3]) - float(mediabox[1])) * unit
    except Exception:
        return 0

    width_pixels = width_points / 72.0 * dpi
    height_pixels = height_points / 72.0 * dpi
    return int(width_pixels * height_pixels)


def page_image_pixels_of(page: Any) -> int:
    """The pixels of every image a page draws, forms included."""
    resources = page.get("/Resources", {}) if hasattr(page, "get") else page.obj.get("/Resources")
    return sum(_iter_resource_image_pixels(resources, visited=set()))


def _iter_workload_image_pixels(resources: Any, *, visited: set[tuple[int, int] | int]):
    """Like the drawn images, plus each image's soft mask, which is decoded too."""
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
            mask = xobject.get("/SMask")
            if hasattr(mask, "get") and _object_key(mask) not in visited:
                visited.add(_object_key(mask))
                yield _image_pixels(mask)
        elif subtype == pikepdf.Name("/Form"):
            yield from _iter_workload_image_pixels(xobject.get("/Resources"), visited=visited)


def _annotation_appearances(page: Any):
    annotations = page.get("/Annots") if hasattr(page, "get") else None
    if not isinstance(annotations, pikepdf.Array):
        return
    for annotation in annotations:
        appearance = annotation.get("/AP") if hasattr(annotation, "get") else None
        if not hasattr(appearance, "get"):
            continue
        for kind in ("/N", "/R", "/D"):
            entry = appearance.get(kind)
            if isinstance(entry, pikepdf.Stream):
                yield entry
            elif isinstance(entry, pikepdf.Dictionary):
                # Appearance states (checkbox on/off): each is a form.
                yield from (state for state in entry.values() if isinstance(state, pikepdf.Stream))


def page_workload_image_pixels_of(page: Any) -> int:
    """Every image pixel rendering the page decodes: drawn, masked, and annotated."""
    visited: set[tuple[int, int] | int] = set()
    resources = page.get("/Resources", {}) if hasattr(page, "get") else page.obj.get("/Resources")
    total = sum(_iter_workload_image_pixels(resources, visited=visited))
    for form in _annotation_appearances(page):
        total += sum(_iter_workload_image_pixels(form.get("/Resources"), visited=visited))
    return total


def _flate_only(stream: pikepdf.Stream) -> bool:
    filters = stream.get("/Filter")
    if isinstance(filters, pikepdf.Array):
        return len(filters) == 1 and filters[0] == pikepdf.Name("/FlateDecode")
    return filters == pikepdf.Name("/FlateDecode")


def _decoded_size(stream: pikepdf.Stream, cap: int) -> int:
    """How many bytes a Flate stream decodes to, counting no further than cap + 1."""
    decoder = zlib.decompressobj()
    pending = stream.read_raw_bytes()
    size = 0
    try:
        while pending and size <= cap:
            size += len(decoder.decompress(pending, cap + 1 - size))
            pending = decoder.unconsumed_tail
    except zlib.error:
        # A damaged stream is not a bomb; the tools that read it repair or skip it.
        pass
    return size


@dataclass(frozen=True)
class StreamWorkload:
    objects: int
    largest_decoded_bytes: int
    total_decoded_bytes: int


def inspect_stream_workload(
    path: Path, *, stream_cap: int, total_cap: int
) -> StreamWorkload:
    """Object count and decoded size of the non-image Flate streams.

    Images are bounded by their pixel limits. Content streams, fonts and
    forms are not, and a few kilobytes of Flate can expand to gigabytes, so
    each is decoded incrementally and counting stops at the caps.
    """
    try:
        with pikepdf.open(str(path)) as pdf:
            objects = len(pdf.objects)
            largest = 0
            total = 0
            for obj in pdf.objects:
                if total > total_cap:
                    break
                if not isinstance(obj, pikepdf.Stream) or not _flate_only(obj):
                    continue
                if obj.get("/Subtype") == pikepdf.Name("/Image"):
                    continue
                size = _decoded_size(obj, stream_cap)
                largest = max(largest, size)
                total += size
    except pikepdf.PdfError as exc:
        raise PdfUploadPreflightError("File is not a readable PDF", status_code=400) from exc
    return StreamWorkload(objects, largest, total)


def inspect_pdf_upload(path: Path, *, settings: Any) -> PdfUploadPreflightReport:
    dpi = _setting_int(settings, "upload_preflight_render_dpi", 300)

    try:
        with pikepdf.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            creator = _metadata_text(pdf.docinfo, "/Creator")
            producer = _metadata_text(pdf.docinfo, "/Producer")
            has_structure_tree = "/StructTreeRoot" in pdf.Root
            has_acroform = "/AcroForm" in pdf.Root
            max_page_render_pixels = 0
            max_image_pixels = 0
            total_image_pixels = 0
            max_workload_image_pixels = 0
            total_workload_image_pixels = 0
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
                page_image_pixels = page_image_pixels_of(page)
                total_image_pixels += page_image_pixels
                max_image_pixels = max(max_image_pixels, page_image_pixels)
                if page_image_pixels >= image_heavy_page_min_pixels:
                    image_heavy_pages += 1
                workload_pixels = page_workload_image_pixels_of(page)
                total_workload_image_pixels += workload_pixels
                max_workload_image_pixels = max(max_workload_image_pixels, workload_pixels)
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
        creator=creator,
        producer=producer,
        has_structure_tree=has_structure_tree,
        has_acroform=has_acroform,
        max_workload_image_pixels=max_workload_image_pixels,
        total_workload_image_pixels=total_workload_image_pixels,
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
    max_ocr_scan_total_image_pixels = _setting_int(
        settings,
        "max_upload_ocr_scan_total_image_pixels",
        150_000_000,
    )
    max_ocr_scan_image_heavy_pages = _setting_int(
        settings,
        "max_upload_ocr_scan_image_heavy_pages",
        25,
    )

    max_objects = _setting_int(settings, "max_upload_objects", 1_000_000)
    stream_cap = _setting_int(settings, "max_upload_decoded_stream_bytes", 256 * 1024 * 1024)
    total_cap = _setting_int(settings, "max_upload_decoded_total_bytes", 2 * 1024 * 1024 * 1024)
    streams = inspect_stream_workload(path, stream_cap=stream_cap, total_cap=total_cap)
    max_workload = max(report.max_image_pixels, report.max_workload_image_pixels)
    total_workload = max(report.total_image_pixels, report.total_workload_image_pixels)

    reasons: list[str] = []
    if streams.objects > max_objects:
        reasons.append(f"{streams.objects:,} objects exceeds the {max_objects:,}-object limit")
    if streams.largest_decoded_bytes > stream_cap:
        reasons.append(
            f"a compressed stream expands past the {stream_cap // (1024 * 1024)} MB limit"
        )
    elif streams.total_decoded_bytes > total_cap:
        reasons.append(
            f"its compressed streams expand past the {total_cap // (1024 * 1024)} MB limit"
        )
    if report.page_count > max_pages:
        reasons.append(f"{report.page_count} pages exceeds the {max_pages}-page limit")
    if report.max_page_render_pixels > max_page_render_pixels:
        reasons.append(
            f"the largest page would render at {_megapixels(report.max_page_render_pixels)}, "
            f"above the {_megapixels(max_page_render_pixels)} limit"
        )
    if max_workload > max_image_pixels:
        reasons.append(
            f"the largest page embeds {_megapixels(max_workload)} of image data, "
            f"above the {_megapixels(max_image_pixels)} limit"
        )
    if total_workload > max_total_image_pixels:
        reasons.append(
            f"embedded image data totals {_megapixels(total_workload)}, "
            f"above the {_megapixels(max_total_image_pixels)} limit"
        )
    if report.image_heavy_pages > max_image_heavy_pages:
        reasons.append(
            f"{report.image_heavy_pages} image-heavy pages exceeds the "
            f"{max_image_heavy_pages}-page self-service limit"
        )
    if (
        report.is_ocr_scan_like
        and report.total_image_pixels > max_ocr_scan_total_image_pixels
    ):
        reasons.append(
            "this OCR/Paper Capture PDF embeds "
            f"{_megapixels(report.total_image_pixels)} of scan image data, "
            f"above the {_megapixels(max_ocr_scan_total_image_pixels)} "
            "self-service limit"
        )
    if (
        report.is_ocr_scan_like
        and report.image_heavy_pages > max_ocr_scan_image_heavy_pages
    ):
        reasons.append(
            f"this OCR/Paper Capture PDF has {report.image_heavy_pages} "
            "image-heavy pages, above the "
            f"{max_ocr_scan_image_heavy_pages}-page self-service limit"
        )

    if reasons:
        raise PdfUploadPreflightError(
            "This PDF is too large or image-heavy for self-service processing: "
            + "; ".join(reasons)
            + ". Split it into smaller sections, downsample scan images, "
            "or contact the CUNY AI Lab for assisted remediation.",
            report=report,
        )

    return report
