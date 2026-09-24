"""What a screen reader hears from a tagged PDF, and how Figures are read."""

from pathlib import Path

import pikepdf
import pytest

from app.services.structure_text import screen_reader_text


def _tagged_pdf(path: Path, figure_alt: str | None) -> None:
    """A paragraph, then a Figure whose content is drawn text."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(300, 300))
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            Encoding=pikepdf.Name.WinAnsiEncoding,
        )
    )
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
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            Encoding=pikepdf.Name.WinAnsiEncoding,
        )
    )
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
