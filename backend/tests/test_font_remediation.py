from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest

from tests.fixtures import TEST_SAMPLE_PDF

from app.config import Settings
from app.pipeline import orchestrator
from app.pipeline.orchestrator import (
    FONT_LANE_EMBED,
    FONT_LANE_OCR_REDO,
    FONT_LANE_REPAIR_DICTS,
    FONT_LANE_REPAIR_TOUNICODE,
    _embed_lane_should_skip_local,
    _font_remediation_lanes,
    _ghostscript_embed_command,
    _inspect_pdf_features,
    _inspect_font_diagnostics,
    _repair_pdf_font_dicts_sync,
    _sync_pdf_cid_cff_widths_sync,
    _attempt_auto_llm_font_map,
    _cid_cff_width_key,
    _local_embed_support_kind,
    _local_font_program,
    _merge_tounicode_maps,
    _normalize_font_name,
    _parse_tounicode_map_details,
    _sanitize_text_showing_zero_bytes,
    _simple_font_auto_unicode_policy,
    _simple_font_unicode_map,
    _simple_font_zero_byte_repair_candidate,
    _unicode_repair_gate_from_diagnostics,
)


def _settings(**overrides) -> Settings:
    values = {
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "gemini-test",
    }
    values.update(overrides)
    return Settings(**values)


def _violation(rule_id: str, severity: str = "error") -> SimpleNamespace:
    return SimpleNamespace(rule_id=rule_id, severity=severity)


def _resolve_object(value):
    try:
        return value.get_object()
    except Exception:
        return value


def _xobject_font_only_pdf(output_path: Path) -> None:
    with pikepdf.open(str(TEST_SAMPLE_PDF)) as sample_pdf:
        sample_page = sample_pdf.pages[0]
        sample_resources = _resolve_object(sample_page.obj.get("/Resources"))
        sample_fonts = _resolve_object(sample_resources.get("/Font"))
        font_name = next(iter(sample_fonts.keys()))
        font_dict = sample_fonts.get(font_name)

        pdf = pikepdf.new()
        page = pdf.add_blank_page()

        imported_font = pdf.copy_foreign(font_dict)
        form = pdf.make_stream(
            pikepdf.unparse_content_stream(
                [
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
                    pikepdf.ContentStreamInstruction([font_name, 12], pikepdf.Operator("Tf")),
                    pikepdf.ContentStreamInstruction([10, 20], pikepdf.Operator("Td")),
                    pikepdf.ContentStreamInstruction([pikepdf.String("Nested only")], pikepdf.Operator("Tj")),
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
                ]
            )
        )
        form["/Type"] = pikepdf.Name("/XObject")
        form["/Subtype"] = pikepdf.Name("/Form")
        form["/BBox"] = pikepdf.Array([0, 0, 200, 50])
        form["/Resources"] = pikepdf.Dictionary({
            "/Font": pikepdf.Dictionary({font_name: imported_font}),
        })

        page.obj["/Resources"] = pikepdf.Dictionary({
            "/XObject": pikepdf.Dictionary({"/Fx0": form}),
        })
        page["/Contents"] = pdf.make_stream(
            pikepdf.unparse_content_stream(
                [
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")),
                    pikepdf.ContentStreamInstruction([pikepdf.Name("/Fx0")], pikepdf.Operator("Do")),
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")),
                ]
            )
        )
        pdf.save(str(output_path))


def _pdf_with_stale_acroform(output_path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    link = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([10, 10, 40, 20]),
        "/A": pikepdf.Dictionary({
            "/S": pikepdf.Name("/URI"),
            "/URI": pikepdf.String("https://example.com"),
        }),
    }))
    page["/Annots"] = pikepdf.Array([link])
    pdf.Root["/AcroForm"] = pikepdf.Dictionary({
        "/Fields": pikepdf.Array([]),
        "/DA": pikepdf.String("/Helv 10 Tf 0 g"),
        "/DR": pikepdf.Dictionary(),
    })
    pdf.save(str(output_path))


def _pdf_with_real_widget(output_path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    widget = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Widget"),
        "/Rect": pikepdf.Array([10, 10, 40, 20]),
        "/FT": pikepdf.Name("/Tx"),
        "/T": pikepdf.String("field1"),
    }))
    page["/Annots"] = pikepdf.Array([widget])
    pdf.Root["/AcroForm"] = pikepdf.Dictionary({
        "/Fields": pikepdf.Array([widget]),
        "/DA": pikepdf.String("/Helv 10 Tf 0 g"),
        "/DR": pikepdf.Dictionary(),
    })
    pdf.save(str(output_path))


def _pdf_with_cid_type0_widths(output_path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    font_file3 = pdf.make_stream(b"dummy")
    font_file3["/Subtype"] = pikepdf.Name("/CIDFontType0C")

    descriptor = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/FontDescriptor"),
        "/FontName": pikepdf.Name("/ABCDEE+TestCID"),
        "/FontFile3": font_file3,
    }))
    cid_font = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Font"),
        "/Subtype": pikepdf.Name("/CIDFontType0"),
        "/BaseFont": pikepdf.Name("/ABCDEE+TestCID"),
        "/CIDSystemInfo": pikepdf.Dictionary({
            "/Registry": pikepdf.String("Adobe"),
            "/Ordering": pikepdf.String("Identity"),
            "/Supplement": 0,
        }),
        "/FontDescriptor": descriptor,
        "/W": pikepdf.Array([170, pikepdf.Array([10, 0, 20])]),
    }))
    type0_font = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Font"),
        "/Subtype": pikepdf.Name("/Type0"),
        "/BaseFont": pikepdf.Name("/ABCDEE+TestCID-Identity-H"),
        "/Encoding": pikepdf.Name("/Identity-H"),
        "/DescendantFonts": pikepdf.Array([cid_font]),
    }))

    page["/Resources"] = pikepdf.Dictionary({
        "/Font": pikepdf.Dictionary({"/F0": type0_font}),
    })
    pdf.save(str(output_path))


def test_font_lanes_prioritize_tounicode_when_fonts_are_embedded():
    lanes, skipped = _font_remediation_lanes(
        [_violation("ISO 14289-1:2014-7.21.7-1")],
        classification="digital",
        pdf_features={"pages": 10, "link_annots": 0, "has_forms": False, "unembedded_fonts": 0},
        settings=_settings(),
    )

    assert lanes[:2] == [FONT_LANE_REPAIR_TOUNICODE, FONT_LANE_EMBED]
    assert skipped == ["OCR lanes skipped: digital document"]


def test_font_lanes_embed_before_tounicode_when_fonts_are_unembedded():
    lanes, skipped = _font_remediation_lanes(
        [
            _violation("ISO 14289-1:2014-7.21.3.2-1"),
            _violation("ISO 14289-1:2014-7.21.7-1"),
        ],
        classification="digital",
        pdf_features={"pages": 10, "link_annots": 0, "has_forms": False, "unembedded_fonts": 2},
        settings=_settings(),
    )

    assert lanes[:3] == [
        FONT_LANE_REPAIR_DICTS,
        FONT_LANE_EMBED,
        FONT_LANE_REPAIR_TOUNICODE,
    ]
    assert skipped == ["OCR lanes skipped: digital document"]


def test_ocr_lane_is_not_blocked_by_links_alone():
    lanes, skipped = _font_remediation_lanes(
        [_violation("ISO 14289-1:2014-7.21.8-1")],
        classification="scanned",
        pdf_features={"pages": 5, "link_annots": 3, "has_forms": False, "unembedded_fonts": 0},
        settings=_settings(),
    )

    assert FONT_LANE_OCR_REDO in lanes
    assert skipped == []


def test_ocr_lane_allowed_on_digital_pdf_with_ocr_suspect_fonts():
    lanes, skipped = _font_remediation_lanes(
        [_violation("ISO 14289-1:2014-7.21.8-1")],
        classification="digital",
        pdf_features={
            "pages": 101,
            "link_annots": 0,
            "has_forms": False,
            "unembedded_fonts": 0,
            "ocr_suspect_fonts": 1,
        },
        settings=_settings(),
    )

    assert FONT_LANE_OCR_REDO in lanes
    assert skipped == []


def test_ocr_lane_still_respects_suspect_page_limit():
    lanes, skipped = _font_remediation_lanes(
        [_violation("ISO 14289-1:2014-7.21.8-1")],
        classification="digital",
        pdf_features={
            "pages": 250,
            "link_annots": 0,
            "has_forms": False,
            "unembedded_fonts": 0,
            "ocr_suspect_fonts": 1,
        },
        settings=_settings(),
    )

    assert FONT_LANE_OCR_REDO not in lanes
    assert skipped == ["OCR lanes skipped: page count 250 > limit 200"]


def test_inspect_pdf_features_ignores_stale_acroform_without_widgets(tmp_path):
    pdf_path = tmp_path / "stale_acroform.pdf"
    _pdf_with_stale_acroform(pdf_path)

    features = _inspect_pdf_features(pdf_path)

    assert features["has_forms"] is False
    assert features["link_annots"] == 1


def test_inspect_pdf_features_marks_real_widgets_as_forms(tmp_path):
    pdf_path = tmp_path / "widget_form.pdf"
    _pdf_with_real_widget(pdf_path)

    features = _inspect_pdf_features(pdf_path)

    assert features["has_forms"] is True


def test_sync_pdf_cid_cff_widths_syncs_cff_widths(monkeypatch, tmp_path):
    input_pdf = tmp_path / "cid_widths.pdf"
    output_pdf = tmp_path / "cid_widths_fixed.pdf"
    _pdf_with_cid_type0_widths(input_pdf)

    monkeypatch.setattr(
        orchestrator,
        "_collect_cid_cff_widths",
        lambda _font_bytes: {170: 10, 171: -4, 172: 20},
    )

    ok, message, stats = _sync_pdf_cid_cff_widths_sync(input_pdf, output_pdf)

    assert ok is True
    assert "widths synced=1" in message
    assert stats["widths_synced"] == 1

    with pikepdf.open(output_pdf) as pdf:
        cid_font = pdf.pages[0]["/Resources"]["/Font"]["/F0"]["/DescendantFonts"][0]
        widths = list(cid_font["/W"][1])
        assert widths == [10, -4, 20]


def test_cid_cff_width_key_prefers_cid_glyph_names():
    assert _cid_cff_width_key("cid00016", 4) == 16
    assert _cid_cff_width_key(".notdef", 7) == 0
    assert _cid_cff_width_key("A", 12) == 12


def test_unicode_lane_is_skipped_when_gate_has_no_safe_candidates():
    lanes, skipped = _font_remediation_lanes(
        [_violation("ISO 14289-1:2014-7.21.7-1")],
        classification="digital",
        pdf_features={"pages": 10, "link_annots": 0, "has_forms": False, "unembedded_fonts": 0},
        settings=_settings(),
        unicode_gate={
            "safe_candidate_count": 0,
            "blocked_candidate_count": 2,
            "reason": "unicode issues appear tied to simple fonts without explicit encoding",
        },
    )

    assert FONT_LANE_REPAIR_TOUNICODE not in lanes
    assert FONT_LANE_EMBED in lanes
    assert skipped == [
        "ToUnicode repair skipped: unicode issues appear tied to simple fonts without explicit encoding",
        "OCR lanes skipped: digital document",
    ]


def test_generated_tounicode_mappings_override_existing_entries():
    merged, overwritten = _merge_tounicode_maps(
        {1: "A", 2: "B"},
        {2: "C", 3: "D"},
    )

    assert merged == {1: "A", 2: "C", 3: "D"}
    assert overwritten == 1


def test_font_name_normalization_strips_subset_prefix_and_punctuation():
    assert _normalize_font_name("/ABCDEE+Arial-BoldMT") == "arialboldmt"
    assert _normalize_font_name("Trebuchet MS Bold") == "trebuchetmsbold"


def test_simple_type1_fonts_fall_back_to_standard_encoding():
    font_dict = pikepdf.Dictionary({
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/Times-Roman"),
    })

    mapping = _simple_font_unicode_map(font_dict, None)

    assert mapping[65] == "A"


def test_simple_font_policy_blocks_implicit_nonstandard_fonts():
    font_dict = pikepdf.Dictionary({
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/ABCDEE+TeX_CM_Maths_Symbols"),
    })

    assert _simple_font_auto_unicode_policy(font_dict) == "blocked"


def test_simple_font_policy_allows_embedded_cff_when_builtin_encoding_decodes(monkeypatch):
    font_dict = pikepdf.Dictionary({
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/ABCDEE+TeX_CM_Maths_Symbols"),
    })

    monkeypatch.setattr(
        orchestrator,
        "_cff_builtin_encoding_map",
        lambda font_bytes: {14: "◦"} if font_bytes == b"cff" else {},
    )

    assert _simple_font_auto_unicode_policy(font_dict, font_bytes=b"cff") == "embedded_cff"


def test_simple_type1_fonts_use_embedded_cff_encoding_when_available(monkeypatch):
    font_dict = pikepdf.Dictionary({
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/ABCDEE+TeX_CM_Maths_Symbols"),
    })

    monkeypatch.setattr(
        orchestrator,
        "_cff_builtin_encoding_map",
        lambda font_bytes: {14: "◦", 20: "≤"} if font_bytes == b"cff" else {},
    )

    mapping = _simple_font_unicode_map(font_dict, b"cff")

    assert mapping == {14: "◦", 20: "≤"}


def test_local_embed_support_includes_standard_type1_fonts():
    font_dict = pikepdf.Dictionary({
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/Times-Roman"),
    })

    assert _local_embed_support_kind(font_dict) == "type1_standard14"


def test_local_font_program_uses_type1_fontfile_for_standard14(monkeypatch):
    font_dict = pikepdf.Dictionary({
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/Times-Roman"),
    })

    monkeypatch.setattr(
        orchestrator,
        "_ghostscript_type1_font_program",
        lambda font_name: (b"%!PS-AdobeFont-1.0", "NimbusRoman-Regular", {"Length1": 10, "Length2": 20, "Length3": 0}),
    )

    font_bytes, matched_name, fontfile_key, lengths = _local_font_program(font_dict, "Times-Roman")

    assert font_bytes == b"%!PS-AdobeFont-1.0"
    assert matched_name == "NimbusRoman-Regular"
    assert fontfile_key == "/FontFile"
    assert lengths == {"Length1": 10, "Length2": 20, "Length3": 0}


def test_ghostscript_type1_descriptor_accepts_float_like_afm_metrics(tmp_path, monkeypatch):
    class _FakeAFM:
        def __init__(self, *_args, **_kwargs):
            self._attrs = {
                "FontBBox": ("-168.0", "-218.0", "1000.0", "898.0"),
                "ItalicAngle": "0.0",
                "Ascender": "718.0",
                "Descender": "-207.0",
                "CapHeight": "676.0",
                "Weight": "Regular",
            }
            self._chars = {
                "space": (32, "250.0", "space"),
                "A": (65, "722.0", "A"),
            }

    from fontTools import afmLib

    metrics_dir = tmp_path / "afm"
    metrics_dir.mkdir()
    (metrics_dir / "Times-Roman.afm").write_text("")
    monkeypatch.setattr(orchestrator, "_ghostscript_font_metrics_dir", lambda: metrics_dir)
    monkeypatch.setitem(
        orchestrator.GHOSTSCRIPT_TYPE1_DESCRIPTOR_SPECS,
        "timesroman",
        {"afm": "Times-Roman.afm", "flags": 34},
    )
    monkeypatch.setattr(afmLib, "AFM", _FakeAFM)
    orchestrator._ghostscript_type1_descriptor.cache_clear()

    descriptor = orchestrator._ghostscript_type1_descriptor("Times-Roman")

    assert descriptor is not None
    assert descriptor["ItalicAngle"] == 0
    assert descriptor["Ascent"] == 718
    assert descriptor["Descent"] == -207
    assert descriptor["CapHeight"] == 676
    assert descriptor["FontBBox"] == [-168, -218, 1000, 898]
    assert descriptor["Widths"][32] == 250
    assert descriptor["Widths"][65] == 722


def test_font_diagnostics_can_skip_used_code_analysis(tmp_path, monkeypatch):
    input_pdf = tmp_path / "font_resources_only.pdf"

    pdf = pikepdf.new()
    page = pdf.add_blank_page()
    page.obj["/Resources"] = pikepdf.Dictionary({
        "/Font": pikepdf.Dictionary({
            "/F1": pikepdf.Dictionary({
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/Type1"),
                "/BaseFont": pikepdf.Name("/Times-Roman"),
            }),
        }),
    })
    pdf.save(str(input_pdf))

    parse_calls = {"count": 0}
    original_parse = pikepdf.parse_content_stream

    def _count_parse(*args, **kwargs):
        parse_calls["count"] += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(pikepdf, "parse_content_stream", _count_parse)

    diagnostics = _inspect_font_diagnostics(
        input_pdf,
        include_used_code_analysis=False,
    )

    assert diagnostics["error"] is None
    assert diagnostics["summary"]["fonts_total"] == 1
    assert parse_calls["count"] == 0


def test_parse_tounicode_map_details_caps_pathological_bfrange_span():
    class _FakeStream:
        def read_bytes(self):
            return b"""
            1 beginbfrange
            <0000> <FFFFFFFF> <0041>
            endbfrange
            """

    mapping, invalid_entries = _parse_tounicode_map_details(_FakeStream())

    assert mapping == {}
    assert invalid_entries == 1


def test_unicode_gate_uses_font_diagnostics_for_safe_simple_candidates():
    gate = _unicode_repair_gate_from_diagnostics(
        {
            "profiles": [
                {
                    "subtype": "/Type1",
                    "base_font": "Univers",
                    "has_tounicode": True,
                    "auto_unicode_policy": "embedded_cff",
                    "invalid_tounicode_entries": 12,
                    "missing_used_code_count": 19,
                    "repairable_missing_used_codes": 19,
                }
            ]
        },
        violations=[_violation("ISO 14289-1:2014-7.21.7-1")],
    )

    assert gate["allow_automatic"] is True
    assert gate["safe_simple_candidates"] == 1


def test_unicode_gate_uses_type0_candidates_for_embedded_cid_fonts():
    gate = _unicode_repair_gate_from_diagnostics(
        {
            "profiles": [
                {
                    "subtype": "/Type0",
                    "descendant_subtype": "/CIDFontType2",
                    "embedded": True,
                    "has_tounicode": True,
                }
            ]
        },
        violations=[_violation("ISO 14289-1:2014-7.21.7-1")],
    )

    assert gate["allow_automatic"] is True
    assert gate["safe_type0_candidates"] == 1


def test_inspect_font_diagnostics_counts_fonts_inside_form_xobjects(tmp_path):
    input_pdf = tmp_path / "xobject_fonts.pdf"
    _xobject_font_only_pdf(input_pdf)

    diagnostics = _inspect_font_diagnostics(
        input_pdf,
        include_used_code_analysis=False,
        profile_limit=10,
    )

    assert diagnostics["error"] is None
    assert diagnostics["summary"]["fonts_total"] == 1
    assert diagnostics["summary"]["simple_fonts"] == 1
    assert diagnostics["profiles"][0]["base_font"] != "(unnamed)"


def test_embed_system_fonts_creates_descriptor_for_standard_type1(tmp_path, monkeypatch):
    input_pdf = tmp_path / "input.pdf"
    output_pdf = tmp_path / "output.pdf"

    pdf = pikepdf.new()
    page = pdf.add_blank_page()
    page_obj = page.obj
    font_dict = pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Font"),
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/Times-Roman"),
        "/Encoding": pikepdf.Name("/WinAnsiEncoding"),
    })
    page_obj["/Resources"] = pikepdf.Dictionary({
        "/Font": pikepdf.Dictionary({
            "/F1": font_dict,
        })
    })
    pdf.save(str(input_pdf))

    monkeypatch.setattr(
        orchestrator,
        "_ghostscript_type1_descriptor",
        lambda font_name: {
            "Flags": 34,
            "ItalicAngle": 0,
            "Ascent": 700,
            "Descent": -200,
            "CapHeight": 680,
            "StemV": 80,
            "FontBBox": [-168, -281, 1000, 900],
            "FirstChar": 0,
            "LastChar": 3,
            "Widths": [250, 333, 444, 555],
            "MissingWidth": 250,
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_local_font_program",
        lambda font_dict, font_name, descendant_subtype=None: (
            b"%!PS-AdobeFont-1.0",
            "NimbusRoman-Regular",
            "/FontFile",
            {"Length1": 10, "Length2": 20, "Length3": 0},
        ),
    )

    ok, message, stats = orchestrator._embed_system_fonts_sync(input_pdf, output_pdf)

    assert ok is True
    assert "Local font embedding completed" in message
    assert stats["fonts_embedded"] == 1

    with pikepdf.open(str(output_pdf)) as repaired_pdf:
        resources = repaired_pdf.pages[0].obj["/Resources"]
        repaired_font = resources["/Font"]["/F1"]
        descriptor = repaired_font["/FontDescriptor"]
        assert descriptor["/FontName"] == pikepdf.Name("/Times-Roman")
        assert pikepdf.Name("/FontFile") in descriptor
        assert int(repaired_font["/FirstChar"]) == 0
        assert int(repaired_font["/LastChar"]) == 3
        assert [int(value) for value in repaired_font["/Widths"]] == [250, 333, 444, 555]
        assert int(descriptor["/MissingWidth"]) == 250
        font_stream = descriptor["/FontFile"]
        assert bytes(font_stream.read_bytes()) == b"%!PS-AdobeFont-1.0"
        assert int(font_stream["/Length1"]) == 10
        assert int(font_stream["/Length2"]) == 20
        assert int(font_stream["/Length3"]) == 0


def test_embed_system_fonts_preserves_existing_type1_widths(tmp_path, monkeypatch):
    input_pdf = tmp_path / "input.pdf"
    output_pdf = tmp_path / "output.pdf"

    pdf = pikepdf.new()
    page = pdf.add_blank_page()
    page_obj = page.obj
    descriptor = pikepdf.Dictionary({
        "/Type": pikepdf.Name("/FontDescriptor"),
        "/FontName": pikepdf.Name("/Helvetica"),
        "/Flags": 32,
        "/ItalicAngle": 0,
        "/Ascent": 718,
        "/Descent": -207,
        "/CapHeight": 718,
        "/StemV": 80,
        "/FontBBox": pikepdf.Array([-166, -225, 1000, 931]),
    })
    font_dict = pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Font"),
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/Helvetica"),
        "/Encoding": pikepdf.Dictionary({
            "/Type": pikepdf.Name("/Encoding"),
            "/BaseEncoding": pikepdf.Name("/WinAnsiEncoding"),
            "/Differences": pikepdf.Array([1, pikepdf.Name("/A"), pikepdf.Name("/B"), pikepdf.Name("/C")]),
        }),
        "/FirstChar": 0,
        "/LastChar": 3,
        "/Widths": pikepdf.Array([278, 667, 722, 611]),
        "/FontDescriptor": descriptor,
    })
    page_obj["/Resources"] = pikepdf.Dictionary({
        "/Font": pikepdf.Dictionary({
            "/F1": font_dict,
        })
    })
    pdf.save(str(input_pdf))

    monkeypatch.setattr(
        orchestrator,
        "_ghostscript_type1_descriptor",
        lambda font_name: {
            "Flags": 32,
            "ItalicAngle": 0,
            "Ascent": 718,
            "Descent": -207,
            "CapHeight": 718,
            "StemV": 80,
            "FontBBox": [-166, -225, 1000, 931],
            "FirstChar": 0,
            "LastChar": 255,
            "Widths": [278] + [0] * 255,
            "MissingWidth": 278,
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_local_font_program",
        lambda font_dict, font_name, descendant_subtype=None: (
            b"%!PS-AdobeFont-1.0",
            "NimbusSans-Regular",
            "/FontFile",
            {"Length1": 10, "Length2": 20, "Length3": 0},
        ),
    )

    ok, _, _ = orchestrator._embed_system_fonts_sync(input_pdf, output_pdf)

    assert ok is True

    with pikepdf.open(str(output_pdf)) as repaired_pdf:
        repaired_font = repaired_pdf.pages[0].obj["/Resources"]["/Font"]["/F1"]
        assert [int(value) for value in repaired_font["/Widths"]] == [278, 667, 722, 611]
        descriptor = repaired_font["/FontDescriptor"]
        assert int(descriptor["/MissingWidth"]) == 278
        assert pikepdf.Name("/FontFile") in descriptor


def test_simple_font_zero_byte_repair_candidate_only_allows_code_zero_residue():
    font_dict = pikepdf.Dictionary({
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/Times-Roman"),
        "/Encoding": pikepdf.Name("/WinAnsiEncoding"),
    })

    generated_map = _simple_font_unicode_map(font_dict, None)

    assert _simple_font_zero_byte_repair_candidate(
        font_dict,
        used_codes={0, 65},
        existing_map={},
        generated_map=generated_map,
    ) is True
    assert _simple_font_zero_byte_repair_candidate(
        font_dict,
        used_codes={0, 129, 65},
        existing_map={},
        generated_map=generated_map,
    ) is False


def test_simple_font_zero_byte_candidate_uses_embedded_cff_map(monkeypatch):
    font_dict = pikepdf.Dictionary({
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/ABCDEE+TeX_CM_Maths_Symbols"),
    })

    monkeypatch.setattr(
        orchestrator,
        "_cff_builtin_encoding_map",
        lambda font_bytes: {14: "◦", 20: "≤", 135: "+", 136: "="} if font_bytes == b"cff" else {},
    )

    generated_map = _simple_font_unicode_map(font_dict, b"cff")

    assert _simple_font_zero_byte_repair_candidate(
        font_dict,
        used_codes={0, 14, 20, 135, 136},
        existing_map={},
        generated_map=generated_map,
    ) is True


def test_zero_byte_text_operand_sanitizer_strips_only_string_bytes():
    operands = [pikepdf.Array([pikepdf.String(b"ab\x00c"), -120, pikepdf.String(b"\x00d")])]

    new_operands, removed = _sanitize_text_showing_zero_bytes("TJ", operands)

    assert removed == 2
    assert bytes(new_operands[0][0]) == b"abc"
    assert new_operands[0][1] == -120
    assert bytes(new_operands[0][2]) == b"d"


def test_ghostscript_embed_command_forces_standard_font_embedding():
    cmd = _ghostscript_embed_command(
        "/opt/homebrew/bin/gs",
        Path("/tmp/input.pdf"),
        Path("/tmp/output.pdf"),
    )

    assert "-dEmbedAllFonts=true" in cmd
    assert "-dEmbedSubstituteFonts=true" in cmd
    assert "-c" in cmd
    distiller_params = cmd[cmd.index("-c") + 1]
    assert "/NeverEmbed [ ]" in distiller_params
    assert "/AlwaysEmbed [" in distiller_params
    assert "/Times-Roman" in distiller_params
    assert "/Helvetica" in distiller_params
    assert "/ZapfDingbats" in distiller_params
    assert cmd[-2:] == ("-f", "/tmp/input.pdf")


@pytest.mark.asyncio
async def test_attempt_auto_llm_font_map_applies_only_when_validation_improves(tmp_path, monkeypatch):
    source_pdf = tmp_path / "source.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n% test\n")

    class _FakeLlmClient:
        def __init__(self, *args, **kwargs):
            self.model = "google/gemini-3-flash-preview"

        async def close(self):
            return None

    monkeypatch.setattr(orchestrator, "LlmClient", _FakeLlmClient)
    async def _generate_review_suggestion(**kwargs):
        return {
            "task_type": "font_text_fidelity",
            "confidence": "high",
            "suggested_action": "font_map_candidate",
            "actualtext_candidates": [],
            "model": "google/gemini-3-flash-preview",
        }

    monkeypatch.setattr(
        orchestrator,
        "generate_review_suggestion",
        _generate_review_suggestion,
    )
    monkeypatch.setattr(
        orchestrator,
        "select_auto_font_map_override",
        lambda **kwargs: {
            "page_number": 2,
            "operator_index": 132,
            "unicode_text": "►",
            "font": "ExampleSymbolFont",
            "font_base_name": "ExampleSymbolFont",
            "font_code_hex": "01",
            "target_count": 3,
        },
    )

    def _copy_apply(*, input_pdf, output_pdf, context_path, unicode_text):
        output_pdf.write_bytes(Path(input_pdf).read_bytes())

    monkeypatch.setattr(orchestrator, "apply_unicode_override_to_context", _copy_apply)

    current_validation = SimpleNamespace(
        compliant=False,
        violations=[SimpleNamespace(rule_id="ISO 14289-1:2014-7.21.7-1", severity="error", count=3)],
        raw_report={},
    )
    improved_validation = SimpleNamespace(
        compliant=True,
        violations=[],
        raw_report={},
    )

    async def _validate_pdf(**kwargs):
        return improved_validation

    monkeypatch.setattr(orchestrator, "validate_pdf", _validate_pdf)

    job = SimpleNamespace(
        id="job-1",
        original_filename="sample.pdf",
        input_path=str(source_pdf),
        output_path=str(source_pdf),
        structure_json="{}",
    )
    review_tasks = [
        {
            "task_type": "font_text_fidelity",
            "title": "Verify font text fidelity",
            "detail": "Manual review needed.",
            "severity": "high",
            "blocking": True,
            "source": "validation",
            "metadata": {
                "font_review_targets": [
                    {"page": 2, "operator_index": 132, "context_path": "ctx-1"},
                ],
            },
        }
    ]

    audit, candidate_validation, candidate_output, metadata_overrides = await _attempt_auto_llm_font_map(
        job=job,
        settings=_settings(auto_apply_llm_font_map=True),
        output_pdf=source_pdf,
        current_validation=current_validation,
        review_tasks=review_tasks,
    )

    assert audit["applied"] is True
    assert audit["unicode_text"] == "►"
    assert candidate_validation is improved_validation
    assert candidate_output is not None and candidate_output.exists()
    assert metadata_overrides[("font_text_fidelity", "validation")]["llm_auto_font_map"]["applied"] is True


def test_embed_lane_skips_local_when_no_supported_candidates():
    assert _embed_lane_should_skip_local({
        "summary": {
            "unembedded_fonts": 1,
            "local_embed_candidate_count": 0,
        }
    }) is True
    assert _embed_lane_should_skip_local({
        "summary": {
            "unembedded_fonts": 1,
            "local_embed_candidate_count": 1,
        }
    }) is False
    assert _embed_lane_should_skip_local({
        "summary": {
            "unembedded_fonts": 0,
            "local_embed_candidate_count": 0,
        }
    }) is False


@pytest.mark.asyncio
async def test_font_dict_lane_reuses_existing_tags(tmp_path, monkeypatch):
    tagged_pdf = tmp_path / "tagged.pdf"
    tagged_pdf.write_bytes(b"%PDF-1.7\n")
    reused_tagging = SimpleNamespace(marker="reused")

    async def fake_repair(input_path, output_path):
        output_path.write_bytes(b"%PDF-1.7\n")
        return True, "repaired", {"fonts_touched": 1}

    async def fake_validate(pdf_path, verapdf_path, flavour, **kwargs):
        return SimpleNamespace(
            compliant=True,
            violations=[],
            raw_report={},
            validated_path=Path(pdf_path),
        )

    async def fail_tag_pdf(*args, **kwargs):
        raise AssertionError("tag_pdf should not run for non-structural font repairs")

    monkeypatch.setattr(orchestrator, "create_job_dir", lambda job_id: tmp_path)
    monkeypatch.setattr(orchestrator, "_repair_pdf_font_dicts", fake_repair)
    monkeypatch.setattr(orchestrator, "validate_pdf", fake_validate)
    monkeypatch.setattr(orchestrator, "tag_pdf", fail_tag_pdf)

    result = await orchestrator._attempt_font_lane(
        job_id="job-1",
        job=SimpleNamespace(original_filename="sample.pdf"),
        settings=_settings(),
        working_pdf=tmp_path / "working.pdf",
        tagged_pdf=tagged_pdf,
        structure_json={},
        reviewed_alts=[],
        lane=FONT_LANE_REPAIR_DICTS,
        current_tagging_result=reused_tagging,
    )

    assert result["success"] is True
    assert result["requires_retag"] is False
    assert result["tagging_result"] is reused_tagging
    assert result["output_path"] == tmp_path / "fontfix_repair_dicts.pdf"


@pytest.mark.asyncio
async def test_embed_lane_skips_local_attempt_when_diagnostics_rule_it_out(tmp_path, monkeypatch):
    tagged_pdf = tmp_path / "tagged.pdf"
    tagged_pdf.write_bytes(b"%PDF-1.7\n")

    async def fail_local_embed(*args, **kwargs):
        raise AssertionError("_embed_system_fonts should not run when diagnostics skip local embedding")

    async def fake_gs_embed(input_path, output_path, **kwargs):
        output_path.write_bytes(b"%PDF-1.7\n")
        return True, "ghostscript rewrite"

    async def fake_tag_pdf(*args, **kwargs):
        output = kwargs["output_path"]
        output.write_bytes(b"%PDF-1.7\n")
        return SimpleNamespace(
            output_path=output,
            headings_tagged=0,
            figures_tagged=0,
            decorative_figures_artifacted=0,
            tables_tagged=0,
            lists_tagged=0,
            links_tagged=0,
            bookmarks_added=0,
            tags_added=0,
            struct_elems_created=0,
            title_set=False,
            lang_set=False,
        )

    async def fake_validate(pdf_path, verapdf_path, flavour, **kwargs):
        return SimpleNamespace(
            compliant=False,
            violations=[],
            raw_report={},
            validated_path=Path(pdf_path),
        )

    monkeypatch.setattr(orchestrator, "create_job_dir", lambda job_id: tmp_path)
    monkeypatch.setattr(orchestrator, "_inspect_font_diagnostics", lambda *args, **kwargs: {
        "summary": {
            "unembedded_fonts": 1,
            "local_embed_candidate_count": 0,
        }
    })
    monkeypatch.setattr(orchestrator, "_embed_system_fonts", fail_local_embed)
    monkeypatch.setattr(orchestrator, "_rewrite_pdf_with_ghostscript_embed", fake_gs_embed)
    monkeypatch.setattr(orchestrator, "tag_pdf", fake_tag_pdf)
    monkeypatch.setattr(orchestrator, "validate_pdf", fake_validate)

    result = await orchestrator._attempt_font_lane(
        job_id="job-embed",
        job=SimpleNamespace(original_filename="sample.pdf"),
        settings=_settings(),
        working_pdf=tmp_path / "working.pdf",
        tagged_pdf=tagged_pdf,
        structure_json={},
        reviewed_alts=[],
        lane=FONT_LANE_EMBED,
        current_tagging_result=SimpleNamespace(),
    )

    assert result["success"] is True
    assert result["requires_retag"] is True
    assert result["details"]["local_embed_skipped"] is True


@pytest.mark.asyncio
async def test_ocr_lane_refreshes_structure_before_retag(tmp_path, monkeypatch):
    working_pdf = tmp_path / "working.pdf"
    working_pdf.write_bytes(b"%PDF-1.7\n")
    tagged_pdf = tmp_path / "tagged.pdf"
    tagged_pdf.write_bytes(b"%PDF-1.7\n")
    ocr_pdf = tmp_path / "redo.pdf"
    ocr_pdf.write_bytes(b"%PDF-1.7\n")
    captured = {}

    async def fake_run_ocr(*args, **kwargs):
        return SimpleNamespace(success=True, output_path=ocr_pdf, skipped=False, message="")

    async def fake_extract_structure(input_path, job_dir):
        return SimpleNamespace(document_json={"elements": [{"type": "paragraph", "text": "fresh", "page": 0}]})

    async def fake_tag_pdf(*args, **kwargs):
        captured["structure_json"] = kwargs["structure_json"]
        output = kwargs["output_path"]
        output.write_bytes(b"%PDF-1.7\n")
        return SimpleNamespace(
            output_path=output,
            headings_tagged=0,
            figures_tagged=0,
            decorative_figures_artifacted=0,
            tables_tagged=0,
            lists_tagged=0,
            links_tagged=0,
            bookmarks_added=0,
            tags_added=0,
            struct_elems_created=1,
            title_set=False,
            lang_set=False,
        )

    async def fake_validate(pdf_path, verapdf_path, flavour, **kwargs):
        return SimpleNamespace(
            compliant=True,
            violations=[],
            raw_report={},
            validated_path=Path(pdf_path),
        )

    monkeypatch.setattr(orchestrator, "create_job_dir", lambda job_id: tmp_path)
    monkeypatch.setattr(orchestrator, "run_ocr", fake_run_ocr)
    monkeypatch.setattr(orchestrator, "extract_structure", fake_extract_structure)
    monkeypatch.setattr(orchestrator, "tag_pdf", fake_tag_pdf)
    monkeypatch.setattr(orchestrator, "validate_pdf", fake_validate)

    result = await orchestrator._attempt_font_lane(
        job_id="job-ocr",
        job=SimpleNamespace(original_filename="sample.pdf"),
        settings=_settings(),
        working_pdf=working_pdf,
        tagged_pdf=tagged_pdf,
        structure_json={"elements": [{"type": "paragraph", "text": "stale", "page": 0}]},
        reviewed_alts=[],
        lane=FONT_LANE_OCR_REDO,
        current_tagging_result=SimpleNamespace(),
    )

    assert result["success"] is True
    assert result["requires_retag"] is True
    assert result["details"]["structure_refreshed"] is True
    assert captured["structure_json"]["elements"][0]["text"] == "fresh"
    assert result["structure_json"]["elements"][0]["text"] == "fresh"
