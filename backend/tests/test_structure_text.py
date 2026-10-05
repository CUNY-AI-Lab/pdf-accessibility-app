"""What a screen reader hears from a tagged PDF, and how Figures are read."""

from pathlib import Path

import pikepdf
import pytest

from app.pipeline.pdf_repair import add_missing_icc_components
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import helvetica


def _tagged_pdf(path: Path, figure_alt: str | None) -> None:
    """A paragraph, then a Figure whose content is drawn text."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 300))
    font = helvetica(pdf)
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.Contents = pdf.make_stream(
        b"/P <</MCID 0>> BDC BT /F1 12 Tf 20 250 Td (Paragraph text) Tj ET EMC\n"
        b"/Figure <</MCID 1>> BDC BT /F1 12 Tf 20 150 Td (Text in figure) Tj ET EMC\n"
    )
    page.StructParents = 0
    figure = pikepdf.Dictionary(
        Type=pikepdf.Name.StructElem, S=pikepdf.Name.Figure, Pg=page.obj, K=1
    )
    if figure_alt is not None:
        figure.Alt = pikepdf.String(figure_alt)
    paragraph = pikepdf.Dictionary(Type=pikepdf.Name.StructElem, S=pikepdf.Name.P, Pg=page.obj, K=0)
    document = pikepdf.Dictionary(
        Type=pikepdf.Name.StructElem,
        S=pikepdf.Name.Document,
        K=pikepdf.Array([pdf.make_indirect(paragraph), pdf.make_indirect(figure)]),
    )
    pdf.Root.StructTreeRoot = pdf.make_indirect(
        pikepdf.Dictionary(Type=pikepdf.Name.StructTreeRoot, K=pdf.make_indirect(document))
    )
    pdf.save(path)


@pytest.mark.parametrize(
    ("figure_alt", "figure_text", "heard"),
    [
        (None, False, "Paragraph text"),
        (None, True, "Paragraph text Text in figure"),
        ("A photograph", True, "Paragraph text"),
    ],
)
def test_figure_content_is_read_only_when_asked_and_the_figure_has_no_alt(
    tmp_path, figure_alt, figure_text, heard
):
    path = tmp_path / "tagged.pdf"
    _tagged_pdf(path, figure_alt)
    assert " ".join(screen_reader_text(path, figure_text=figure_text).split()) == heard


def _table_pdf(path: Path) -> None:
    """A header row of two cells, then one data cell spanning both columns."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 300))
    font = helvetica(pdf)
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.Contents = pdf.make_stream(
        b"/TH <</MCID 0>> BDC BT /F1 12 Tf 20 250 Td (Year) Tj ET EMC\n"
        b"/TH <</MCID 1>> BDC BT /F1 12 Tf 120 250 Td (Price & tax) Tj ET EMC\n"
        b"/TD <</MCID 2>> BDC BT /F1 12 Tf 20 230 Td (No sales) Tj ET EMC\n"
    )

    def element(role, kids, **extra):
        return pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.StructElem,
                S=pikepdf.Name(f"/{role}"),
                Pg=page.obj,
                K=kids,
                **extra,
            )
        )

    spanning = pikepdf.Dictionary(O=pikepdf.Name.Table, ColSpan=2)
    table = element(
        "Table",
        pikepdf.Array(
            [
                element(
                    "THead",
                    pikepdf.Array(
                        [element("TR", pikepdf.Array([element("TH", 0), element("TH", 1)]))]
                    ),
                ),
                element(
                    "TBody",
                    pikepdf.Array([element("TR", pikepdf.Array([element("TD", 2, A=spanning)]))]),
                ),
            ]
        ),
    )
    pdf.Root.StructTreeRoot = pdf.make_indirect(
        pikepdf.Dictionary(Type=pikepdf.Name.StructTreeRoot, K=table)
    )
    pdf.save(path)


def test_table_structure_is_kept_as_html_in_markdown(tmp_path):
    path = tmp_path / "table.pdf"
    _table_pdf(path)
    assert " ".join(screen_reader_text(path).split()) == "Year Price & tax No sales"
    assert "".join(screen_reader_text(path, markdown=True).split("\n")) == (
        "<table><thead><tr><th>Year</th><th>Price &amp; tax</th></tr></thead>"
        '<tbody><tr><td colspan="2">No sales</td></tr></tbody></table>'
    )


def test_a_page_with_an_icc_profile_missing_its_component_count_is_read(tmp_path):
    path = tmp_path / "icc.pdf"
    _tagged_pdf(path, figure_alt=None)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        header = bytearray(128)
        header[16:20] = b"RGB "
        header[36:40] = b"acsp"
        profile = pdf.make_stream(bytes(header))
        pdf.pages[0].Resources.ColorSpace = pikepdf.Dictionary(
            CS0=pikepdf.Array([pikepdf.Name.ICCBased, profile])
        )
        pdf.save(path)

    assert " ".join(screen_reader_text(path).split()) == "Paragraph text"
    with pikepdf.open(path) as pdf:
        assert add_missing_icc_components(pdf) == 1
        assert int(pdf.pages[0].Resources.ColorSpace.CS0[1].N) == 3


def _paragraph_pdf(path: Path, content: bytes, kids: list[int] | None = None) -> None:
    """One P element over MCID 0 (or ``kids``), drawn by the given content."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 300))
    font = helvetica(pdf)
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.Contents = pdf.make_stream(content)
    paragraph = pikepdf.Dictionary(
        Type=pikepdf.Name.StructElem,
        S=pikepdf.Name.P,
        Pg=page.obj,
        K=pikepdf.Array(kids) if kids else 0,
    )
    pdf.Root.StructTreeRoot = pdf.make_indirect(
        pikepdf.Dictionary(Type=pikepdf.Name.StructTreeRoot, K=pdf.make_indirect(paragraph))
    )
    pdf.save(path)


@pytest.mark.parametrize(
    ("content", "heard"),
    [
        # ActualText on the MCID's own sequence replaces all its glyphs.
        (
            b"/P <</MCID 0 /ActualText (Whole paragraph text)>> BDC "
            b"BT /F1 12 Tf 20 250 Td (First line only) Tj ET EMC",
            "Whole paragraph text",
        ),
        # ActualText on a nested span replaces only the glyphs inside it.
        (
            b"/P <</MCID 0>> BDC BT /F1 12 Tf 20 250 Td (The ) Tj "
            b"/Span <</ActualText (office)>> BDC (o\\336ce) Tj EMC "
            b"( is open) Tj ET EMC",
            "The office is open",
        ),
    ],
)
def test_marked_content_actual_text_is_read_in_place_of_its_glyphs(tmp_path, content, heard):
    path = tmp_path / "actual_text.pdf"
    _paragraph_pdf(path, content)
    assert " ".join(screen_reader_text(path).split()) == heard


def test_words_spaced_only_by_position_are_read_as_words(tmp_path):
    """Text placed word by word with no space characters, as OCR text
    layers and many typeset PDFs draw it, reads with its word spaces."""
    content = (
        b"/P <</MCID 0>> BDC BT /F1 12 Tf 20 250 Td "
        b"[(In) -600 (addition,) -600 (the) -600 (speakers)] TJ ET EMC"
    )
    path = tmp_path / "spaced_by_position.pdf"
    _paragraph_pdf(path, content)

    assert screen_reader_text(path) == "In addition, the speakers"


def test_a_word_hyphenated_at_a_line_end_is_read_whole(tmp_path):
    content = (
        b"/P <</MCID 0>> BDC BT /F1 12 Tf 20 250 Td (The results were evalu-) Tj "
        b"0 -14 Td (ated again.) Tj ET EMC"
    )
    path = tmp_path / "hyphenated.pdf"
    _paragraph_pdf(path, content)

    assert " ".join(screen_reader_text(path).split()) == "The results were evaluated again."


def test_words_split_between_two_marked_contents_keep_their_space(tmp_path):
    content = (
        b"/P <</MCID 0>> BDC BT /F1 12 Tf 20 250 Td [(The) -600] TJ ET EMC "
        b"/P <</MCID 1>> BDC BT /F1 12 Tf 52 250 Td (office) Tj ET EMC"
    )
    path = tmp_path / "two_marked.pdf"
    _paragraph_pdf(path, content, kids=[0, 1])

    assert " ".join(screen_reader_text(path).split()) == "The office"
