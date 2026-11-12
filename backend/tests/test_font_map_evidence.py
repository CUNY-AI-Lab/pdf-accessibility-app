"""Evidence the font-map step shows the model: glyph names and glyph previews."""

from io import BytesIO

import pikepdf
import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from PIL import Image

from app.pipeline.fonts.encoding import _glyph_name_for_code
from app.services import pdf_preview, remediation_intelligence
from app.services.font_unicode_override import inspect_context_font_target
from tests.test_remediation_intelligence import _job, _task
from tests.test_tex_font_unicode import ARRAY_ENCODING, _type1_program


def _symbolic_truetype(*, keep_names: bool) -> bytes:
    builder = FontBuilder(1000, isTTF=True)
    order = [".notdef", "heart"]
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({0xF041: "heart"})
    builder.setupGlyf({name: TTGlyphPen(None).glyph() for name in order})
    builder.setupHorizontalMetrics({name: (500, 0) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupPost(keepGlyphNames=keep_names)
    buffer = BytesIO()
    builder.save(buffer)
    # FontBuilder writes a Unicode (3,1) cmap; a symbol font has (3,0).
    with TTFont(BytesIO(buffer.getvalue())) as font:
        for table in font["cmap"].tables:
            if table.platformID == 3:
                table.platEncID = 0
        out = BytesIO()
        font.save(out)
        return out.getvalue()


def test_glyph_names_come_from_differences_base_encoding_or_program():
    font = pikepdf.Dictionary(
        Subtype=pikepdf.Name.Type1,
        Encoding=pikepdf.Dictionary(
            BaseEncoding=pikepdf.Name.WinAnsiEncoding,
            Differences=[31, pikepdf.Name("/epsilonlunatesymbol")],
        ),
    )
    assert _glyph_name_for_code(font, 31, None) == ("epsilonlunatesymbol", "Differences")
    assert _glyph_name_for_code(font, 65, None) == ("A", "WinAnsiEncoding")
    assert _glyph_name_for_code(font, 3, None) is None

    builtin = pikepdf.Dictionary(Subtype=pikepdf.Name.Type1)
    program, _ = _type1_program(ARRAY_ENCODING)
    assert _glyph_name_for_code(builtin, 2, program) == (
        "summation",
        "font program built-in encoding",
    )


@pytest.mark.parametrize(("keep_names", "expected"), [(True, "heart"), (False, None)])
def test_symbolic_truetype_glyph_name_needs_names_the_program_stores(keep_names, expected):
    font = pikepdf.Dictionary(Subtype=pikepdf.Name.TrueType)
    named = _glyph_name_for_code(font, 0x41, _symbolic_truetype(keep_names=keep_names))
    assert (named[0] if named else None) == expected


def _write_text_pdf(path, shows, *, font=None, mode=0) -> list[str]:
    """One Tj per (text, size, x, y). Returns each Tj's veraPDF context path."""
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(200, 100))
        font = font or pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=pdf.make_indirect(font)))
        parts = [f"BT {mode} Tr".encode()]
        for text, size, x, y in shows:
            literal = text.replace("(", "\\(").replace(")", "\\)")
            parts.append(f"/F1 {size} Tf 1 0 0 1 {x} {y} Tm ({literal}) Tj".encode("latin-1"))
        parts.append(b"ET")
        page.Contents = pdf.make_stream(b"\n".join(parts))
        pdf.save(path)
    with pikepdf.open(path) as pdf:
        shown = [
            index
            for index, instruction in enumerate(pikepdf.parse_content_stream(pdf.pages[0]))
            if str(instruction.operator) == "Tj"
        ]
    return [
        "root/document[0]/pages[0](1 0 obj PDPage)/contentStream[0](2 0 obj PDContentStream)"
        f"/operators[{index}]/usedGlyphs[0](F F 1 0 0 0 true)"
        for index in shown
    ]


def test_target_inspection_carries_the_glyph_name_and_what_it_resolves_to(tmp_path):
    path = tmp_path / "target.pdf"
    font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica,
        Encoding=pikepdf.Dictionary(Differences=[33, pikepdf.Name.alpha, pikepdf.Name.g37]),
    )
    known, opaque = _write_text_pdf(path, [("!", 10, 20, 50), ('"', 10, 40, 50)], font=font)

    target = inspect_context_font_target(pdf_path=path, context_path=opaque)
    assert (target["glyph_name"], target["glyph_name_source"]) == ("g37", "Differences")
    assert "glyph_name_unicode" not in target
    resolved = inspect_context_font_target(pdf_path=path, context_path=known)
    assert (resolved["glyph_name"], resolved["glyph_name_unicode"]) == ("alpha", "α")


def _superscript_pdf(path) -> str:
    # "log", a small "2+", "(x)", and the small target "e" after "2+".
    shows = [("log", 10, 20, 50), ("2+", 6, 36, 55), ("(x)", 10, 47, 50), ("e", 6, 42.5, 55)]
    return _write_text_pdf(path, shows)[-1]


def test_glyph_preview_zooms_to_the_glyph_and_circles_it_without_covering_it(tmp_path):
    path = tmp_path / "superscript.pdf"
    previews = pdf_preview._glyph_preview_images(path, _superscript_pdf(path))

    assert previews is not None
    circled, alone, ink, em_px = previews
    assert em_px == pytest.approx(pdf_preview.GLYPH_EM_PX, abs=1)
    # Only the small "e": its ink is under an em wide, so no neighbor is in it.
    assert ink[2] - ink[0] < 0.8 * em_px
    red = [
        (x, y)
        for x in range(circled.width)
        for y in range(circled.height)
        if (pixel := circled.getpixel((x, y)))[0] > 150 and pixel[1] < 100 and pixel[2] < 100
    ]
    assert red
    assert not any(ink[0] <= x < ink[2] and ink[1] <= y < ink[3] for x, y in red)
    assert alone.convert("L").getextrema()[0] < 128


def test_preview_falls_back_to_the_region_crop_without_one_glyph_of_ink(tmp_path, monkeypatch):
    invisible = tmp_path / "invisible.pdf"
    (hidden,) = _write_text_pdf(invisible, [("e", 10, 40, 50)], mode=3)
    assert pdf_preview._glyph_preview_images(invisible, hidden) is None
    long_run = tmp_path / "long.pdf"
    (run,) = _write_text_pdf(long_run, [("ABCDEFGHIJKLMNOPQRST", 6, 10, 50)])
    assert pdf_preview._glyph_preview_images(long_run, run) is None

    monkeypatch.setattr(pdf_preview, "render_target_preview_png_data_url", lambda *_: "region")
    assert pdf_preview.render_target_previews_png_data_urls(invisible, hidden) == [
        ("target region", "region")
    ]
    superscript = tmp_path / "superscript.pdf"
    glyph = pdf_preview.render_target_previews_png_data_urls(
        superscript, _superscript_pdf(superscript)
    )
    assert [description for description, _ in glyph] == [
        "target glyph circled in red, zoomed",
        "target glyph drawn alone",
    ]
    for _, url in glyph:
        assert url.startswith("data:image/png;base64,")


def test_glyph_previews_are_png_images(tmp_path):
    path = tmp_path / "superscript.pdf"
    previews = pdf_preview.render_glyph_previews_png_bytes(path, _superscript_pdf(path))
    assert previews is not None
    for data in previews:
        with Image.open(BytesIO(data)) as image:
            assert image.format == "PNG"


def test_font_prompt_carries_glyph_names_and_every_preview_image(monkeypatch, tmp_path):
    monkeypatch.setattr(
        remediation_intelligence,
        "render_page_jpeg_data_url",
        lambda pdf_path, page_number: f"data:image/jpeg;base64,page-{page_number}",
    )
    monkeypatch.setattr(
        remediation_intelligence,
        "render_target_previews_png_data_urls",
        lambda pdf_path, context_path: [("circled", "data:c"), ("alone", "data:a")],
    )
    monkeypatch.setattr(
        remediation_intelligence,
        "inspect_context_font_target",
        lambda pdf_path, context_path: {
            "font_code": 31,
            "font_code_hex": "1F",
            "font_base_name": "HardingText-RegularItalic5",
            "target_operator": "Tj",
            "glyph_name": "epsilonlunatesymbol",
            "glyph_name_source": "Differences",
        },
    )
    target = {"page": 7, "font": "HardingText", "operator_index": 1906, "context_path": "ctx"}

    prompt_text, content = remediation_intelligence._font_task_payload(
        _job(tmp_path),
        _task(
            "font_text_fidelity",
            metadata={"pages_to_check": [7], "font_review_targets": [target]},
        ),
    )

    assert '"glyph_name": "epsilonlunatesymbol"' in prompt_text
    assert '"images": [\n        "circled",\n        "alone"\n      ]' in prompt_text
    assert [part["image_url"]["url"] for part in content[1:]] == [
        "data:image/jpeg;base64,page-7",
        "data:c",
        "data:a",
    ]
