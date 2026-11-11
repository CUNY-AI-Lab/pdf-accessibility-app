"""Small hand-built PDFs for tagging and reading tests."""

from pathlib import Path

import pikepdf
import pypdfium2


def helvetica(pdf: pikepdf.Pdf) -> pikepdf.Object:
    """Standard Helvetica with an explicit encoding. Without /Encoding,
    pdfminer decodes the font as raw CIDs once ocrmypdf has been imported in
    the same process, as it is when the plugin tests run first."""
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            Encoding=pikepdf.Name.WinAnsiEncoding,
        )
    )


def rendered(path: Path) -> bytes:
    """The first page's pixels as PDFium draws them."""
    document = pypdfium2.PdfDocument(path)
    try:
        return document[0].render(scale=1).to_pil().convert("RGB").tobytes()
    finally:
        document.close()


def tagged_text_pages(
    path: Path, *, pages: int = 1, hidden_page: int | None = None,
    untagged_page: int | None = None,
    running_head: bool = False, hidden_words: int = 0, tagged_words: int = 20,
    invisible: bool = False, actual_text: str | None = None,
) -> Path:
    """Real page text, with optional artifact body, furniture, or OCR rendering mode."""
    pdf = pikepdf.new()
    font = helvetica(pdf)
    elements = []
    for number in range(1, pages + 1):
        page = pdf.add_blank_page(page_size=(612, 792))
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        commands = []

        def text(mark: str, words: str, y: int) -> None:
            tokens = words.split()
            lines = [" ".join(tokens[start:start + 10]) for start in range(0, len(tokens), 10)]
            commands.append(
                f"{mark} BT /F1 10 Tf 14 TL {'3 Tr' if invisible else ''} 54 {y} Td "
                + " ".join(f"({line}) Tj T*" for line in lines)
                + " ET EMC"
            )

        if running_head:
            text("/Artifact BMC", "A repeated running head with many words", 760)
            text("/Artifact BMC", str(number), 25)
        body = " ".join(["word"] * tagged_words)
        text("/Artifact BMC" if number == hidden_page else "/P <</MCID 0>> BDC", body, 650)
        if hidden_words:
            text("/Artifact BMC", " ".join(["word"] * hidden_words), 630 - 14 * ((tagged_words + 9) // 10))
        if number not in {hidden_page, untagged_page}:
            paragraph = pikepdf.Dictionary(
                Type=pikepdf.Name.StructElem, S=pikepdf.Name.P, Pg=page.obj, K=0,
            )
            if actual_text is not None:
                paragraph.ActualText = pikepdf.String(actual_text)
            elements.append(pdf.make_indirect(paragraph))
        page.Contents = pdf.make_stream("\n".join(commands).encode("ascii"))
    pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.StructTreeRoot, K=pikepdf.Array(elements),
    ))
    pdf.save(path)
    return path


def tagged_table(
    path: Path, *, headers: bool, summary: bool = False, rows: int = 3, columns: int = 3,
) -> Path:
    """A tagged table, with row groups and /Pg inherited by its cells."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))

    def element(role, kids):
        return pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.StructElem, S=pikepdf.Name(f"/{role}"), K=kids,
        ))

    tagged_rows, commands = [], []
    for row in range(rows):
        cells = []
        for column in range(columns):
            mcid = row * columns + column
            role = "TH" if headers and row == 0 else "TD"
            cells.append(element(role, mcid))
            commands.append(
                f"/{role} <</MCID {mcid}>> BDC BT /F1 12 Tf "
                f"{54 + column * 100} {650 - row * 20} Td (Cell{mcid}) Tj ET EMC"
            )
        tagged_rows.append(element("TR", pikepdf.Array(cells)))
    table = element("Table", element("TBody", pikepdf.Array(tagged_rows)))
    table.Pg = page.obj
    if summary:
        table.Summary = pikepdf.String("A summary is not a header cell")
    page.Contents = pdf.make_stream("\n".join(commands).encode("ascii"))
    pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.StructTreeRoot, K=table,
    ))
    pdf.save(path)
    return path


def described_text(
    path: Path, *, role: str = "Figure", key: str = "Alt", description: str = "A chart",
    form: bool = False, hidden_body: bool = False,
) -> Path:
    """Described text with the same MCID as unrelated body text in a Form variant."""
    tagged_text_pages(path, hidden_page=1 if hidden_body else None, tagged_words=50)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        commands = (
            "/Span <</MCID 0>> BDC " if form else "/Span <</MCID 1>> BDC "
        ) + "BT /F1 10 Tf 14 TL 54 400 Td " + " ".join(
            "(drawing drawing drawing drawing drawing) Tj T*" for _ in range(4)
        ) + " ET EMC"
        if form:
            stream = pdf.make_stream(commands.encode("ascii"))
            stream.Type = pikepdf.Name.XObject
            stream.Subtype = pikepdf.Name.Form
            stream.BBox = pikepdf.Array([0, 0, 612, 792])
            stream.Resources = page.Resources
            page.Resources.XObject = pikepdf.Dictionary(Fx=stream)
            commands = "/Fx Do"
            reference = pikepdf.Dictionary(Type=pikepdf.Name.MCR, MCID=0, Stm=stream)
        else:
            reference = 1
        contents = page.Contents.read_bytes() + b"\n" + commands.encode("ascii")
        page.Contents = pdf.make_stream(contents)
        span = pdf.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.Span, K=reference))
        node = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.StructElem, S=pikepdf.Name(f"/{role}"),
            Pg=page.obj, K=span,
        ))
        node[f"/{key}"] = pikepdf.String(description)
        pdf.Root.StructTreeRoot.K.append(node)
        pdf.save(path)
    return path
