"""HTML behavior from real tagged PDFs, including reviewer edits to the final tree."""

import base64
import subprocess
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO

import pikepdf
import pytest
from PIL import Image

from app.pipeline.tagger import tag_pdf
from app.services import html_export
from app.services.html_export import export_html
from app.services.structure_text import screen_reader_text
from tests.pdf_fixtures import described_text, helvetica, tagged_table, tagged_text_pages


@dataclass
class Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    @property
    def text(self):
        return "".join(child if isinstance(child, str) else child.text for child in self.children)

    def all(self, tag):
        return [child for child in self.descendants() if child.tag == tag]

    def descendants(self):
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.descendants()


class Document(HTMLParser):
    """Strict tag balance check: HTMLParser does not implicitly repair our output."""

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Element("root")
        self.stack = [self.root]
        self.comments = []
        self.feed(source)
        self.close()
        assert self.stack == [self.root], "Unclosed elements"
        self.body = self.root.all("body")[0]
        ids = [node.attrs["id"] for node in self.root.descendants() if "id" in node.attrs]
        assert len(ids) == len(set(ids)), "Duplicate IDs"
        for node in self.root.descendants():
            if node.tag in {"ol", "ul"}:
                assert all(child.tag == "li" for child in node.children if isinstance(child, Element))
            if node.tag == "p":
                assert not any(child.tag in {"p", "div", "table", "aside", "section"} for child in node.descendants())
            if node.tag == "table":
                assert all(child.tag in {"caption", "thead", "tbody", "tfoot", "tr"} for child in node.children if isinstance(child, Element))
            if node.tag == "a" and node.attrs["href"].startswith("#"):
                assert node.attrs["href"][1:] in ids
            for header in node.attrs.get("headers", "").split():
                assert any(th.attrs.get("id") == header for th in self.body.all("th"))

    def handle_starttag(self, tag, attrs):
        element = Element(tag, dict(attrs))
        self.stack[-1].children.append(element)
        if tag not in {"meta", "img", "br", "hr", "input", "link"}:
            self.stack.append(element)

    def handle_endtag(self, tag):
        assert self.stack[-1].tag == tag, f"Invalid close: {tag} after {self.stack[-1].tag}"
        self.stack.pop()

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def handle_comment(self, data):
        self.comments.append(data)


def norm(text):
    return " ".join(text.split())


def checked(path, *, formula_alt=""):
    source = export_html(path)
    document = Document(source)
    # Explicitly account for the two requested additions the scorer omits.
    titles = [h for h in document.body.all("h1") if h.attrs.get("class") == "document-title"]
    added = (titles[0].text + " ") if titles else ""
    heard = screen_reader_text(path)
    if formula_alt:
        assert formula_alt in document.body.text
        actual = document.body.text.replace(formula_alt, "", 1)
    else:
        actual = document.body.text
    assert norm(actual) == norm(added + heard)
    assert source.startswith("<!doctype html>")
    assert not document.root.all("script")
    assert not document.root.all("link")
    for image in document.body.all("img"):
        assert image.attrs["src"].startswith("data:image/png;base64,")
        assert "alt" in image.attrs
    return document


def structs(node):
    if isinstance(node, pikepdf.Array):
        for kid in node:
            yield from structs(kid)
    elif isinstance(node, pikepdf.Dictionary) and "/S" in node:
        yield node
        yield from structs(node.get("/K"))


async def tag_lines(tmp_path, lines, *, image=False, decorative=False):
    """Use the real tagger over synthetic text and image operators."""
    source, tagged = tmp_path / "source.pdf", tmp_path / "tagged.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    commands, elements = [], []
    for index, (text, options) in enumerate(lines):
        y = 740 - index * 24
        commands.append(
            f"BT /F1 10 Tf 40 {y} Td ".encode()
            + pikepdf.String(text.encode("cp1252")).unparse() + b" Tj ET"
        )
        elements.append({"type": "paragraph", "page": 0, "text": text,
                         "bbox": {"l": 39, "b": y - 3, "r": 550, "t": y + 11}, **options})
    if image:
        picture = pdf.make_stream(bytes([220, 20, 20] * 4))
        picture.Type, picture.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        picture.Width = picture.Height = 2
        picture.BitsPerComponent, picture.ColorSpace = 8, pikepdf.Name.DeviceRGB
        page.Resources.XObject = pikepdf.Dictionary(Im=picture)
        commands.append(b"q 120 0 0 80 40 80 cm /Im Do Q")
        elements.append({"type": "figure", "page": 0, "figure_index": 0,
                         "bbox": {"l": 40, "b": 80, "r": 160, "t": 160}})
    page.Contents = pdf.make_stream(b"\n".join(commands))
    pdf.save(source)
    pdf.close()
    await tag_pdf(
        source, tagged, {"title": "Reviewed <document>", "elements": elements},
        [{"figure_index": 0, "text": 'A red square & "detail"', "status": "approved", "decorative": decorative}] if image else [],
        language="en", original_filename=source.name,
    )
    return tagged


async def test_headings_lists_links_and_language_from_tagger(tmp_path):
    lines = [(f"Heading {level}", {"type": "heading", "level": level}) for level in range(1, 7)]
    lines += [("Bonjour le monde", {"language": "fr"}),
              ("1. First item", {"type": "list_item", "marker": "1.", "enumerated": True, "list_group_ref": "ordered"}),
              ("2. Second item", {"type": "list_item", "marker": "2.", "enumerated": True, "list_group_ref": "ordered"}),
              ("* Bullet item", {"type": "list_item", "marker": "*", "list_group_ref": "bullets"}),
              ("Read the guide for details", {})]
    path = await tag_lines(tmp_path, lines)
    # Reviewer edits act on the final tags, which are the only export source.
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        nodes = list(structs(pdf.Root.StructTreeRoot.K))
        paragraph = next(n for n in nodes if n.S == "/P")
        paragraph.Lang = pikepdf.String("fr")
        paragraph.ActualText = pikepdf.String("Bonjour <lecteur> & bienvenue")
        paragraph.S = pikepdf.Name.CustomParagraph
        pdf.Root.StructTreeRoot.RoleMap = pikepdf.Dictionary(CustomParagraph=pikepdf.Name.P)
        # The link's marked text still lives inside the paragraph.
        last = [n for n in nodes if n.S == "/P"][-1]
        annotation = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=[40, 490, 170, 510],
            A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String('https://example.test/?a=1&b="two"')),
        ))
        link = pdf.make_indirect(pikepdf.Dictionary(
            S=pikepdf.Name.Link, Pg=pdf.pages[0].obj,
            K=pikepdf.Array([last.K, pikepdf.Dictionary(Type=pikepdf.Name.OBJR, Obj=annotation)]),
        ))
        last.K = link
        pdf.save(path)
    document = checked(path)
    assert document.root.all("html")[0].attrs["lang"] == "en"
    assert document.root.all("title")[0].text == "Reviewed <document>"
    for level in range(1, 7):
        assert norm(document.body.all(f"h{level}")[0].text) == f"Heading {level}"
    assert len(document.body.all("h1")) == 1
    assert any(p.attrs.get("lang") == "fr" and "<lecteur>" in p.text for p in document.body.all("p"))
    assert len(document.body.all("ol")) == 1
    assert len(document.body.all("ul")) == 1
    assert len(document.body.all("li")) == 3
    assert document.body.all("a")[0].attrs["href"] == 'https://example.test/?a=1&b="two"'


async def test_table_scope_spans_headers_caption_and_duplicate_pdf_ids(tmp_path):
    path = await tag_lines(tmp_path, [("Table heading", {"type": "heading", "level": 1})])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        # Build table semantics with the tagger's own builder, as reviewer fixes
        # may add a caption or alter the table attribute array after tagging.
        from app.pipeline.tagger_structure import StructTreeBuilder

        builder = StructTreeBuilder(pdf)
        builder.struct_tree_root = pdf.Root.StructTreeRoot
        builder.doc_elem = pdf.Root.StructTreeRoot.K[0]
        table = {"num_rows": 3, "num_cols": 3, "cells": [
            {"row": 0, "col": 0, "text": "Years", "column_header": True, "col_span": 2},
            {"row": 0, "col": 2, "text": "Price", "column_header": True},
            {"row": 1, "col": 0, "text": "Region", "row_header": True, "row_span": 2},
            {"row": 1, "col": 1, "text": "10"}, {"row": 1, "col": 2, "text": "20"},
            {"row": 2, "col": 1, "text": "30"}, {"row": 2, "col": 2, "text": "40"},
        ]}
        cells = builder._ensure_fragmented_table(table)
        for cell in table["cells"]:
            cells[(cell["row"], cell["col"])].ActualText = pikepdf.String(cell["text"])
        # Attribute arrays and class attributes occur in externally reviewed PDFs.
        header = cells[(0, 0)]
        header.A = pikepdf.Array([header.A, pikepdf.Dictionary(O=pikepdf.Name.Layout)])
        table_node = builder.doc_elem.K[-1]
        caption = pdf.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.Caption, ActualText=pikepdf.String("Reviewed table")))
        table_node.K = pikepdf.Array([caption, *list(table_node.K)])
        duplicate = pdf.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.Note, ID=pikepdf.String("t1-r0-c0"), ActualText=pikepdf.String("A note")))
        builder.doc_elem.K.append(duplicate)
        pdf.save(path)
    document = checked(path)
    assert document.body.all("caption")[0].text == "Reviewed table"
    headers = document.body.all("th")
    assert {h.attrs["scope"] for h in headers} == {"col", "row"}
    assert any(h.attrs.get("rowspan") == "2" for h in headers)
    assert any(h.attrs.get("colspan") == "2" for h in headers)
    assert document.body.all("td")[0].attrs.get("headers")
    assert document.body.all("aside")[0].text == "A note"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("offset", [False, True])
async def test_figures_are_png_crops_with_alt_and_decorative_empty_alt(tmp_path, rotation, offset):
    path = await tag_lines(tmp_path, [("Figures", {"type": "heading", "level": 1})], image=True)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        figure = next(n for n in structs(pdf.Root.StructTreeRoot.K) if n.S == "/Figure")
        decorative = pdf.make_indirect(pikepdf.Dictionary(figure))
        decorative.Alt = pikepdf.String("")
        pdf.Root.StructTreeRoot.K[0].K.append(decorative)
        pdf.pages[0].Rotate = rotation
        if offset:
            pdf.pages[0].MediaBox = pikepdf.Array([20, 30, 632, 822])
            pdf.pages[0].CropBox = pikepdf.Array([30, 60, 500, 700])
        pdf.save(path)
    document = checked(path)
    images = document.body.all("img")
    assert [i.attrs["alt"] for i in images] == ['A red square & "detail"', ""]
    for image in images:
        with Image.open(BytesIO(base64.b64decode(image.attrs["src"].split(",")[1]))) as png:
            assert png.format == "PNG"
            assert max(png.size) <= 1600
            assert 245 <= max(png.size) <= 251  # 120 pt at 150 ppi, not a full-page render.
            pixel = png.convert("RGB").getpixel((png.width // 2, png.height // 2))
            assert pixel[0] > 180 and pixel[1] < 60


async def test_image_budget_and_missing_region_keep_alternatives(tmp_path, monkeypatch):
    path = await tag_lines(tmp_path, [("Figures", {"type": "heading", "level": 1})], image=True)
    monkeypatch.setattr(html_export, "MAX_TOTAL_IMAGE_BYTES", 1)
    document = checked(path)
    assert not document.body.all("img")
    assert document.body.all("span")[0].attrs["aria-label"] == 'A red square & "detail"'
    assert any("size budget" in comment for comment in document.comments)
    monkeypatch.setattr(html_export, "MAX_TOTAL_IMAGE_BYTES", 8 * 1024 * 1024)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        figure = next(n for n in structs(pdf.Root.StructTreeRoot.K) if n.S == "/Figure")
        del figure.K
        pdf.save(path)
    document = checked(path)
    assert any("region unavailable" in comment for comment in document.comments)
    assert document.body.all("span")[0].attrs["aria-label"] == 'A red square & "detail"'


@pytest.mark.parametrize("fixture", ["pages", "table", "form", "actual"])
def test_synthetic_fixtures_match_screen_reader_and_omit_artifacts(tmp_path, fixture):
    path = tmp_path / f"{fixture}.pdf"
    if fixture == "pages":
        tagged_text_pages(path, pages=3, running_head=True)
    elif fixture == "table":
        tagged_table(path, headers=True)
    elif fixture == "form":
        described_text(path, role="P", key="Alt", description="A description", form=True)
    else:
        tagged_text_pages(path, actual_text="Reviewer <fix> & correction", running_head=True)
    document = checked(path)
    assert "running head" not in document.body.text
    if fixture == "actual":
        assert "Reviewer <fix> & correction" in document.body.text
    if fixture == "form":
        assert "drawing drawing" in document.body.text


@pytest.mark.parametrize("content,expected", [
    (b"BT /F1 12 Tf 20 250 Td (The results were evalu-) Tj 0 -14 Td (ated again.) Tj ET", "The results were evaluated again."),
    (b"BT /F1 12 Tf 20 250 Td [(In) -600 (addition,) -600 (the) -600 (speakers)] TJ ET", "In addition, the speakers"),
    (b"BT /F1 12 Tf 20 250 Td (The ) Tj /Span <</ActualText (office)>> BDC (wrong) Tj EMC ( is open) Tj ET", "The office is open"),
])
def test_spacing_dehyphenation_and_marked_actual_text(tmp_path, content, expected):
    path = tagged_text_pages(tmp_path / "spacing.pdf")
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Contents = pdf.make_stream(b"/P <</MCID 0>> BDC " + content + b" EMC")
        pdf.save(path)
    assert expected in norm(checked(path).body.text)


async def test_formula_note_quote_code_internal_link_and_inherited_lang(tmp_path):
    path = await tag_lines(tmp_path, [("Semantics", {"type": "heading", "level": 1})])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        document = pdf.Root.StructTreeRoot.K[0]
        document.Lang = pikepdf.String("es")
        for role, text in [("Formula", "x < y"), ("Note", "A footnote"), ("BlockQuote", "A quotation"), ("Code", 'print("hello")')]:
            document.K.append(pdf.make_indirect(pikepdf.Dictionary(S=pikepdf.Name(f"/{role}"), ActualText=pikepdf.String(text))))
        document.K.append(pdf.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.Formula, Alt=pikepdf.String("a plus b"))))
        annotation = pdf.make_indirect(pikepdf.Dictionary(Dest=pikepdf.Array([pdf.pages[0].obj, pikepdf.Name.Fit])))
        document.K.append(pdf.make_indirect(pikepdf.Dictionary(
            S=pikepdf.Name.Link, ActualText=pikepdf.String("Back to start"),
            K=pikepdf.Dictionary(Type=pikepdf.Name.OBJR, Obj=annotation),
        )))
        pdf.save(path)
    document = checked(path, formula_alt="a plus b")
    assert any(node.attrs.get("lang") == "es" for node in document.body.all("div"))
    assert document.body.all("aside")[0].text == "A footnote"
    assert document.body.all("blockquote")[0].text == "A quotation"
    assert document.body.all("code")[0].text == 'print("hello")'
    assert document.body.all("a")[0].attrs["href"].startswith("#pdf-")
    assert "x < y" in document.body.text


@pytest.mark.parametrize("uri", ["javascript:alert(1)", "data:text/html,<script>bad</script>", "file:///etc/passwd", "java\nscript:bad"])
async def test_unsafe_link_actions_remain_plain_text(tmp_path, uri):
    path = await tag_lines(tmp_path, [("Links", {"type": "heading", "level": 1})])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        annotation = pdf.make_indirect(pikepdf.Dictionary(A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String(uri))))
        pdf.Root.StructTreeRoot.K[0].K.append(pdf.make_indirect(pikepdf.Dictionary(
            S=pikepdf.Name.Link, ActualText=pikepdf.String("Readable link"),
            K=pikepdf.Dictionary(Type=pikepdf.Name.OBJR, Obj=annotation),
        )))
        pdf.save(path)
    assert not checked(path).body.all("a")


def test_title_fallback_and_untagged_input(tmp_path):
    path = tagged_text_pages(tmp_path / "title.pdf")
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.docinfo.Title = pikepdf.String("A <title> & details")
        pdf.Root.Lang = pikepdf.String("fr-CA")
        pdf.save(path)
    document = checked(path)
    assert document.body.all("h1")[0].text == "A <title> & details"
    assert document.root.all("title")[0].text == "A <title> & details"
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        del pdf.Root.StructTreeRoot
        pdf.save(path)
    with pytest.raises(ValueError, match="requires a tagged PDF"):
        export_html(path)


def test_cli_writes_self_contained_utf8_html(tmp_path):
    path = tagged_text_pages(tmp_path / "cli.pdf", actual_text="Fran\u00e7ais & <texte>")
    output = tmp_path / "document.html"
    result = subprocess.run([sys.executable, "scripts/export_html.py", str(path), "-o", str(output)], capture_output=True, check=True)
    assert result.stdout == b""
    assert "Fran\u00e7ais & <texte>" in Document(output.read_text(encoding="utf-8")).body.text


def test_nested_lists_with_labels_and_inherited_language(tmp_path):
    path = tagged_text_pages(tmp_path / "nested-lists.pdf")
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        def element(role, text=None, kids=None, **extra):
            node = pikepdf.Dictionary(S=pikepdf.Name(f"/{role}"), **extra)
            if text is not None:
                node.ActualText = pikepdf.String(text)
            if kids is not None:
                node.K = pikepdf.Array(kids)
            return pdf.make_indirect(node)

        nested = element("L", kids=[element("LI", kids=[element("Lbl", "*"), element("LBody", "Nested item")])])
        outer = element("L", kids=[element("LI", kids=[element("Lbl", "(iv)"), element("LBody", kids=[element("P", "Outer item"), nested])])], Lang=pikepdf.String("la"))
        pdf.Root.StructTreeRoot.K = pikepdf.Array([element("H1", "Lists"), outer])
        pdf.save(path)
    document = checked(path)
    assert len(document.body.all("ol")) == 1
    assert len(document.body.all("ul")) == 1
    assert document.body.all("ol")[0].attrs["lang"] == "la"
    assert len(document.body.all("ol")[0].all("ul")) == 1


@pytest.mark.parametrize("separated", [True, False])
def test_inline_spans_keep_cross_mcid_word_boundaries(tmp_path, separated):
    path = tagged_text_pages(tmp_path / "inline.pdf")
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        paragraph = pdf.Root.StructTreeRoot.K[0]
        paragraph.K = pikepdf.Array([
            pdf.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.Span, K=0)),
            pdf.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.Span, K=1, Lang=pikepdf.String("fr"))),
        ])
        content = (b"BT /F1 12 Tf 40 650 Td /Span <</MCID 0>> BDC "
                   + (b"[(The) -600] TJ EMC " if separated else b"(of) Tj EMC ")
                   + b"/Span <</MCID 1>> BDC "
                   + (b"(office) Tj" if separated else b"(fice) Tj") + b" EMC ET")
        pdf.pages[0].Contents = pdf.make_stream(content)
        paragraph.Pg = pdf.pages[0].obj
        pdf.save(path)
    document = checked(path)
    assert norm(document.body.all("p")[0].text) == ("The office" if separated else "office")
    assert document.body.all("span")[1].attrs["lang"] == "fr"


def test_marked_actual_text_without_glyphs_and_artifact_nodes(tmp_path):
    path = tagged_text_pages(tmp_path / "marked-actual.pdf", running_head=True)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page.Contents = pdf.make_stream(
            b"/P <</MCID 0 /ActualText (Corrected symbol)>> BDC 20 20 10 10 re f EMC\n"
            b"/Artifact BMC BT /F1 12 Tf 40 700 Td (Hidden furniture) Tj ET EMC"
        )
        pdf.Root.StructTreeRoot.K.append(pdf.make_indirect(pikepdf.Dictionary(
            S=pikepdf.Name.Artifact, K=pikepdf.Dictionary(S=pikepdf.Name.P, ActualText=pikepdf.String("Hidden structure artifact")),
        )))
        pdf.save(path)
    document = checked(path)
    assert "Corrected symbol" in document.body.text
    assert "Hidden" not in document.body.text


async def test_image_resolution_and_cumulative_budget(tmp_path, monkeypatch):
    path = await tag_lines(tmp_path, [("Figures", {"type": "heading", "level": 1})], image=True)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        figure = next(n for n in structs(pdf.Root.StructTreeRoot.K) if n.S == "/Figure")
        figure.A = pikepdf.Dictionary(O=pikepdf.Name.Layout, BBox=pdf.pages[0].MediaBox)
        pdf.Root.StructTreeRoot.K[0].K.append(pdf.make_indirect(pikepdf.Dictionary(figure)))
        pdf.save(path)
    document = checked(path)
    uri = document.body.all("img")[0].attrs["src"]
    with Image.open(BytesIO(base64.b64decode(uri.split(",")[1]))) as image:
        assert max(image.size) == 1600
    monkeypatch.setattr(html_export, "MAX_TOTAL_IMAGE_BYTES", len(uri))
    document = checked(path)
    assert len(document.body.all("img")) == 1
    assert any("size budget" in comment for comment in document.comments)
    assert sum(len(i.attrs["src"]) for i in document.body.all("img")) <= len(uri)
    monkeypatch.setattr(html_export, "MAX_FIGURE_BYTES", 1)
    document = checked(path)
    assert not document.body.all("img")


def test_figure_region_in_a_form_xobject(tmp_path):
    path = described_text(tmp_path / "form-figure.pdf", form=True)
    document = checked(path)
    image = document.body.all("img")[0]
    assert image.attrs["alt"] == "A chart"
    with Image.open(BytesIO(base64.b64decode(image.attrs["src"].split(",")[1]))) as png:
        assert png.width < 600 and png.height < 200
        assert png.convert("L").getextrema()[0] < 100


async def test_decorative_artifacts_from_tagger_are_omitted(tmp_path):
    path = await tag_lines(tmp_path, [("Decorations", {"type": "heading", "level": 1})], image=True, decorative=True)
    document = checked(path)
    assert not document.body.all("img")
    assert not document.comments
    assert "red square" not in document.body.text


def test_cli_cannot_overwrite_input_pdf(tmp_path):
    path = tagged_text_pages(tmp_path / "source.pdf")
    before = path.read_bytes()
    result = subprocess.run([sys.executable, "scripts/export_html.py", str(path), "-o", str(path)], capture_output=True)
    assert result.returncode == 2
    assert b"output must differ" in result.stderr
    assert path.read_bytes() == before


async def test_figure_actual_text_replaces_alt_once_and_keeps_language(tmp_path):
    path = await tag_lines(tmp_path, [("Figures", {"type": "heading", "level": 1})], image=True)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        figure = next(n for n in structs(pdf.Root.StructTreeRoot.K) if n.S == "/Figure")
        figure.ActualText = pikepdf.String("Un carr\u00e9 rouge")
        figure.Lang = pikepdf.String("fr")
        pdf.save(path)
    document = checked(path)
    assert document.body.text.count("Un carr\u00e9 rouge") == 1
    wrapper = next(span for span in document.body.all("span") if "Un carr\u00e9 rouge" in span.text)
    assert wrapper.attrs["lang"] == "fr"
    assert wrapper.all("img")[0].attrs["alt"] == ""
