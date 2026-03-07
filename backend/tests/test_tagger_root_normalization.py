import pikepdf

from app.pipeline.tagger import (
    _normalize_embedded_file_specs,
    _normalize_media_clip_data_dicts,
    _normalize_optional_content_configs,
    _normalize_type1_font_charsets,
    _remove_dynamic_xfa,
)


def test_normalize_optional_content_configs_sets_name_and_removes_as():
    pdf = pikepdf.new()
    pdf.Root["/OCProperties"] = pikepdf.Dictionary({
        "/D": pikepdf.Dictionary({
            "/AS": pikepdf.Array([pikepdf.Dictionary({"/Event": pikepdf.Name("/View")})]),
        }),
        "/Configs": pikepdf.Array([
            pikepdf.Dictionary({
                "/AS": pikepdf.Array([]),
            }),
            pikepdf.Dictionary({
                "/Name": pikepdf.String("Already Named"),
            }),
        ]),
    })

    changes = _normalize_optional_content_configs(pdf)

    default_config = pdf.Root["/OCProperties"]["/D"]
    first_config = pdf.Root["/OCProperties"]["/Configs"][0]
    second_config = pdf.Root["/OCProperties"]["/Configs"][1]

    assert changes == 4
    assert str(default_config["/Name"]) == "Default"
    assert "/AS" not in default_config
    assert str(first_config["/Name"]) == "Optional Content Config 1"
    assert "/AS" not in first_config
    assert str(second_config["/Name"]) == "Already Named"


def test_remove_dynamic_xfa_strips_xfa_packet():
    pdf = pikepdf.new()
    pdf.Root["/AcroForm"] = pikepdf.Dictionary({
        "/Fields": pikepdf.Array([]),
        "/XFA": pikepdf.Array([pikepdf.String("template"), pikepdf.String("<xdp/>")]),
    })

    removed = _remove_dynamic_xfa(pdf)

    assert removed is True
    assert "/XFA" not in pdf.Root["/AcroForm"]


def test_normalize_embedded_file_specs_backfills_f_and_uf():
    pdf = pikepdf.new()
    embedded_stream = pdf.make_stream(b"attachment bytes")
    file_spec = pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Filespec"),
        "/EF": pikepdf.Dictionary({
            "/F": embedded_stream,
        }),
        "/Desc": pikepdf.String("Course handout"),
    })
    pdf.Root["/Names"] = pikepdf.Dictionary({
        "/EmbeddedFiles": pikepdf.Dictionary({
            "/Names": pikepdf.Array([
                pikepdf.String("handout.pdf"),
                file_spec,
            ]),
        }),
    })

    changes = _normalize_embedded_file_specs(pdf)
    stored_file_spec = pdf.Root["/Names"]["/EmbeddedFiles"]["/Names"][1]

    assert changes == 2
    assert str(stored_file_spec["/F"]) == "handout.pdf"
    assert str(stored_file_spec["/UF"]) == "handout.pdf"


def test_normalize_media_clip_data_dicts_backfills_ct_and_alt():
    pdf = pikepdf.new()
    media_clip = pikepdf.Dictionary({
        "/S": pikepdf.Name("/MCD"),
        "/D": pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Filespec"),
            "/F": pikepdf.String("lecture.mp4"),
        }),
    })
    pdf.Root["/OpenAction"] = pikepdf.Dictionary({
        "/S": pikepdf.Name("/Rendition"),
        "/R": pikepdf.Dictionary({
            "/C": media_clip,
        }),
    })

    changes = _normalize_media_clip_data_dicts(pdf)

    stored_clip = pdf.Root["/OpenAction"]["/R"]["/C"]
    assert changes == 2
    assert str(stored_clip["/CT"]) == "video/mp4"
    assert isinstance(stored_clip["/Alt"], pikepdf.Array)
    assert len(stored_clip["/Alt"]) == 2
    assert str(stored_clip["/Alt"][1]) == "lecture.mp4"


def test_normalize_type1_font_charsets_strips_nested_form_font_charset():
    pdf = pikepdf.new()
    page = pdf.add_blank_page()

    font_stream = pdf.make_stream(b"%!PS-AdobeFont-1.0")
    font_stream["/Length1"] = 10
    font_stream["/Length2"] = 0
    font_stream["/Length3"] = 0

    descriptor = pikepdf.Dictionary({
        "/Type": pikepdf.Name("/FontDescriptor"),
        "/FontName": pikepdf.Name("/SubsetFont"),
        "/Flags": 32,
        "/ItalicAngle": 0,
        "/Ascent": 700,
        "/Descent": -200,
        "/CapHeight": 680,
        "/StemV": 80,
        "/FontBBox": pikepdf.Array([-100, -200, 1000, 900]),
        "/CharSet": pikepdf.String("/A/B/C"),
        "/FontFile": font_stream,
    })
    nested_font = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Font"),
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/SubsetFont"),
        "/FirstChar": 0,
        "/LastChar": 2,
        "/Widths": pikepdf.Array([500, 500, 500]),
        "/FontDescriptor": descriptor,
    }))
    form = pdf.make_stream(
        b"q BT /F1 12 Tf 0 0 Td (ABC) Tj ET Q",
        Type=pikepdf.Name("/XObject"),
        Subtype=pikepdf.Name("/Form"),
        BBox=pikepdf.Array([0, 0, 100, 100]),
        Resources=pikepdf.Dictionary({
            "/Font": pikepdf.Dictionary({
                "/F1": nested_font,
            }),
        }),
    )
    page.obj["/Resources"] = pikepdf.Dictionary({
        "/XObject": pikepdf.Dictionary({
            "/Fm0": form,
        }),
    })

    changes = _normalize_type1_font_charsets(pdf)

    assert changes == 1
    assert "/CharSet" not in descriptor
