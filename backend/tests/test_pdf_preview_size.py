import subprocess
from io import BytesIO

import pikepdf
from PIL import Image

from app.services import pdf_preview


def _page(path, width_points, height_points, rotate=0):
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(width_points, height_points))
        if rotate:
            page.obj.Rotate = rotate
        page.Contents = pdf.make_stream(b"0 0 1 rg 10 10 50 50 re f")
        pdf.save(path)
    return path


def _render(path, monkeypatch):
    requested = []
    real_run = subprocess.run

    def run(cmd, **kwargs):
        requested.append(float(cmd[cmd.index("-r") + 1]))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(pdf_preview.subprocess, "run", run)
    png = pdf_preview.render_page_png_bytes(path, 1)
    with Image.open(BytesIO(png)) as image:
        return requested[0], image.size


def test_a_poster_page_renders_at_its_preview_width(tmp_path, monkeypatch):
    # 100 x 50 inches: 144 dpi would be a 14,400-pixel-wide render.
    dpi, (width, _) = _render(_page(tmp_path / "poster.pdf", 7200, 3600), monkeypatch)
    assert dpi <= 14.0
    assert pdf_preview.MAX_IMAGE_WIDTH - 1 <= width <= pdf_preview.MAX_IMAGE_WIDTH


def test_a_rotated_poster_uses_its_displayed_width(tmp_path, monkeypatch):
    # Rotated a quarter turn, the 7,200-point side is the height on screen.
    dpi, (width, height) = _render(_page(tmp_path / "rotated.pdf", 7200, 3600, rotate=90), monkeypatch)
    assert dpi <= 28.0
    assert height > width


def test_a_letter_page_keeps_the_preview_resolution(tmp_path, monkeypatch):
    dpi, (width, _) = _render(_page(tmp_path / "letter.pdf", 612, 792), monkeypatch)
    assert dpi == pdf_preview.RENDER_DPI
    assert width == 1224
