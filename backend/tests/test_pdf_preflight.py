from types import SimpleNamespace

import pikepdf
import pytest

from app.services.pdf_preflight import (
    PdfUploadPreflightError,
    inspect_pdf_upload,
    preflight_pdf_upload,
)


def _settings(**overrides):
    defaults = {
        "max_upload_pages": 300,
        "max_upload_page_render_pixels": 75_000_000,
        "upload_preflight_render_dpi": 300,
        "max_upload_image_pixels": 75_000_000,
        "max_upload_total_image_pixels": 1_000_000_000,
        "max_upload_image_heavy_pages": 75,
        "upload_image_heavy_page_min_pixels": 4_000_000,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _write_image_pdf(path, *, pages: int = 1, image_width: int = 1000, image_height: int = 1000):
    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        page = pdf.add_blank_page(page_size=(612, 792))
        image = pdf.make_stream(b"\x00")
        image["/Type"] = pikepdf.Name("/XObject")
        image["/Subtype"] = pikepdf.Name("/Image")
        image["/Width"] = image_width
        image["/Height"] = image_height
        image["/ColorSpace"] = pikepdf.Name("/DeviceGray")
        image["/BitsPerComponent"] = 8
        page.obj["/Resources"] = pikepdf.Dictionary({
            "/XObject": pikepdf.Dictionary({"/Im0": image})
        })
    pdf.save(path)


def test_preflight_reports_embedded_image_workload(tmp_path):
    path = tmp_path / "scan.pdf"
    _write_image_pdf(path, pages=2, image_width=2000, image_height=3000)

    report = inspect_pdf_upload(path, settings=_settings())

    assert report.page_count == 2
    assert report.max_image_pixels == 6_000_000
    assert report.total_image_pixels == 12_000_000
    assert report.image_heavy_pages == 2


def test_preflight_rejects_single_huge_embedded_image(tmp_path):
    path = tmp_path / "huge-page.pdf"
    _write_image_pdf(path, image_width=10_000, image_height=10_000)

    with pytest.raises(PdfUploadPreflightError) as exc_info:
        preflight_pdf_upload(path, settings=_settings(max_upload_image_pixels=50_000_000))

    assert exc_info.value.status_code == 413
    assert "largest page embeds 100 MP" in exc_info.value.detail


def test_preflight_rejects_total_embedded_image_workload(tmp_path):
    path = tmp_path / "many-scans.pdf"
    _write_image_pdf(path, pages=3, image_width=2000, image_height=3000)

    with pytest.raises(PdfUploadPreflightError) as exc_info:
        preflight_pdf_upload(path, settings=_settings(max_upload_total_image_pixels=10_000_000))

    assert "embedded image data totals 18 MP" in exc_info.value.detail


def test_preflight_rejects_oversized_page_render_area(tmp_path):
    path = tmp_path / "poster.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(3000, 3000))
    pdf.save(path)

    with pytest.raises(PdfUploadPreflightError) as exc_info:
        preflight_pdf_upload(path, settings=_settings(max_upload_page_render_pixels=50_000_000))

    assert "largest page would render" in exc_info.value.detail
