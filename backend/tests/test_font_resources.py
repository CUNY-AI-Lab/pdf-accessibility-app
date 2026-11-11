"""Traversal boundaries retained when the legacy font walkers are shared."""

import pikepdf
import pytest

from app.pipeline.fonts.resources import (
    DEFAULT_RESOURCES,
    DIAGNOSTIC_RESOURCES,
    TAGGER_RESOURCES,
    TOUNICODE_RESOURCES,
    ZERO_BYTE_RESOURCES,
    _iter_pdf_fonts,
)


def _resources(pdf, name):
    font = pdf.make_indirect(pikepdf.Dictionary(BaseFont=pikepdf.Name(f"/{name}")))
    return pdf.make_indirect(pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)))


def _names(pdf, policy):
    return [str(font.BaseFont) for font in _iter_pdf_fonts(pdf, policy=policy)]


@pytest.mark.parametrize(
    "policy",
    [
        DEFAULT_RESOURCES,
        DIAGNOSTIC_RESOURCES,
        TOUNICODE_RESOURCES,
        TAGGER_RESOURCES,
        ZERO_BYTE_RESOURCES,
    ],
)
def test_resources_follow_form_streams_and_appearances_once_in_order(policy):
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        page.Resources = _resources(pdf, "Page")
        form = pdf.make_stream(b"")
        form.Subtype = pikepdf.Name.Form
        form.Resources = _resources(pdf, "Form")
        page.Resources.XObject = pikepdf.Dictionary(Fm=form)
        # A Form may be referenced by multiple pages and point back to its parent.
        form.Resources.XObject = pikepdf.Dictionary(Parent=form)
        other_page = pdf.add_blank_page()
        other_page.Resources = page.Resources
        appearance = pdf.make_stream(b"")
        appearance.Resources = _resources(pdf, "Appearance")
        page.Annots = pikepdf.Array(
            [
                pdf.make_indirect(
                    pikepdf.Dictionary(AP=pikepdf.Dictionary(N=appearance, R=appearance))
                )
            ]
        )
        assert _names(pdf, policy) == ["/Page", "/Form", "/Appearance"]


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (DEFAULT_RESOURCES, ["/First"]),
        (DIAGNOSTIC_RESOURCES, ["/First", "/Second"]),
        (TOUNICODE_RESOURCES, ["/First", "/Second"]),
        (TAGGER_RESOURCES, []),
        (ZERO_BYTE_RESOURCES, ["/First", "/Second"]),
    ],
)
def test_direct_field_identity_and_tagger_scope_stay_pass_specific(policy, expected):
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        # Legacy default passes treat (0, 0) as a shared field identity;
        # diagnostics and ToUnicode only suppress real indirect field cycles.
        first = pikepdf.Dictionary(DR=_resources(pdf, "First"))
        second = pikepdf.Dictionary(DR=_resources(pdf, "Second"))
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([first, second]))
        assert _names(pdf, policy) == expected


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (DEFAULT_RESOURCES, ["/Child"]),
        (DIAGNOSTIC_RESOURCES, ["/Child"]),
        (TOUNICODE_RESOURCES, ["/Child"]),
        (TAGGER_RESOURCES, []),
        (ZERO_BYTE_RESOURCES, []),
    ],
)
def test_field_children_and_cycles_keep_the_original_pass_scope(policy, expected):
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        parent = pdf.make_indirect(pikepdf.Dictionary())
        child = pdf.make_indirect(pikepdf.Dictionary(DR=_resources(pdf, "Child")))
        parent.Kids = pikepdf.Array([child])
        child.Kids = pikepdf.Array([parent])
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([parent]))
        assert _names(pdf, policy) == expected
