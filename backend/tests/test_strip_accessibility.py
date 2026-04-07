import pikepdf

from scripts.strip_accessibility import strip_accessibility


def _build_accessible_like_pdf(path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    font = pdf.make_indirect(
        pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Font"),
            "/Subtype": pikepdf.Name("/Type1"),
            "/BaseFont": pikepdf.Name("/Helvetica"),
        })
    )

    page["/Resources"] = pikepdf.Dictionary({
        "/Font": pikepdf.Dictionary({
            "/F1": font,
        })
    })
    page["/Contents"] = pdf.make_stream(
        pikepdf.unparse_content_stream(
            [
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
                pikepdf.ContentStreamInstruction([pikepdf.Name("/F1"), 12], pikepdf.Operator("Tf")),
                pikepdf.ContentStreamInstruction([10, 20], pikepdf.Operator("Td")),
                pikepdf.ContentStreamInstruction(
                    [
                        pikepdf.Name("/Span"),
                        pikepdf.Dictionary({
                            "/ActualText": pikepdf.String("Visible text"),
                            "/MCID": 0,
                        }),
                    ],
                    pikepdf.Operator("BDC"),
                ),
                pikepdf.ContentStreamInstruction([pikepdf.String("Visible text")], pikepdf.Operator("Tj")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC")),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
            ]
        )
    )

    widget = pdf.make_indirect(
        pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Annot"),
            "/Subtype": pikepdf.Name("/Widget"),
            "/FT": pikepdf.Name("/Tx"),
            "/Rect": pikepdf.Array([10, 10, 100, 30]),
            "/T": pikepdf.String("name"),
            "/TU": pikepdf.String("Accessible name"),
            "/StructParent": 0,
            "/Contents": pikepdf.String("Widget description"),
        })
    )
    page["/Annots"] = pikepdf.Array([widget])
    page["/Tabs"] = pikepdf.Name("/S")
    page["/StructParents"] = 0

    pdf.Root["/MarkInfo"] = pikepdf.Dictionary({
        "/Marked": True,
        "/Suspects": False,
    })
    pdf.Root["/Lang"] = pikepdf.String("en-US")
    metadata = pdf.make_stream(
        b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description rdf:about="" />'
        b"</rdf:RDF>"
        b"</x:xmpmeta>"
        b'<?xpacket end="w"?>'
    )
    metadata["/Type"] = pikepdf.Name("/Metadata")
    metadata["/Subtype"] = pikepdf.Name("/XML")
    pdf.Root["/Metadata"] = metadata
    pdf.Root["/StructTreeRoot"] = pdf.make_indirect(
        pikepdf.Dictionary({
            "/Type": pikepdf.Name("/StructTreeRoot"),
            "/ParentTree": pdf.make_indirect(pikepdf.Dictionary()),
        })
    )
    pdf.Root["/Outlines"] = pdf.make_indirect(
        pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Outlines"),
            "/Count": 0,
        })
    )
    pdf.Root["/PageMode"] = pikepdf.Name("/UseOutlines")
    pdf.Root["/AcroForm"] = pikepdf.Dictionary({
        "/Fields": pikepdf.Array([widget]),
    })
    pdf.docinfo["/Title"] = pikepdf.String("Gold Title")
    pdf.save(path)


def test_strip_accessibility_removes_core_semantics(tmp_path) -> None:
    input_pdf = tmp_path / "gold.pdf"
    output_pdf = tmp_path / "stripped.pdf"
    _build_accessible_like_pdf(input_pdf)

    report = strip_accessibility(input_path=input_pdf, output_path=output_pdf)

    assert report.bookmarks_removed is True
    assert report.root_fields_removed >= 5
    assert report.dictionary_fields_removed >= 2
    assert report.annotation_contents_removed == 1
    assert report.marked_content_properties_removed >= 2
    assert report.page_streams_rewritten == 1

    with pikepdf.open(output_pdf) as pdf:
        assert "/StructTreeRoot" not in pdf.Root
        assert "/MarkInfo" not in pdf.Root
        assert "/Lang" not in pdf.Root
        assert "/Metadata" not in pdf.Root
        assert "/Outlines" not in pdf.Root
        assert str(pdf.Root.get("/PageMode", "")) != "/UseOutlines"
        assert "/Title" not in pdf.docinfo

        page = pdf.pages[0]
        assert "/Tabs" not in page
        assert "/StructParents" not in page

        widget = page.Annots[0]
        assert "/TU" not in widget
        assert "/StructParent" not in widget
        assert "/Contents" not in widget

        instructions = list(pikepdf.parse_content_stream(page))
        bdc_props = [
            list(instr.operands)[1]
            for instr in instructions
            if str(getattr(instr, "operator", "")) == "BDC" and len(list(instr.operands)) == 2
        ]
        assert all("/ActualText" not in props for props in bdc_props)
        assert all("/MCID" not in props for props in bdc_props)
        assert any(str(getattr(instr, "operator", "")) == "Tj" for instr in instructions)


def test_strip_accessibility_handles_stream_like_dictionaries_in_realistic_pages(tmp_path) -> None:
    input_pdf = tmp_path / "streamy.pdf"
    output_pdf = tmp_path / "streamy_stripped.pdf"
    _build_accessible_like_pdf(input_pdf)

    with pikepdf.open(input_pdf, allow_overwriting_input=True) as pdf:
        form_xobject = pdf.make_stream(b"q Q")
        form_xobject["/Type"] = pikepdf.Name("/XObject")
        form_xobject["/Subtype"] = pikepdf.Name("/Form")
        form_xobject["/Resources"] = pikepdf.Dictionary()
        form_xobject["/StructParent"] = 99
        pdf.pages[0]["/Resources"]["/XObject"] = pikepdf.Dictionary({
            "/Fm1": form_xobject,
        })
        pdf.save(input_pdf)

    report = strip_accessibility(input_path=input_pdf, output_path=output_pdf)

    assert report.dictionary_fields_removed >= 2
    assert output_pdf.exists()
