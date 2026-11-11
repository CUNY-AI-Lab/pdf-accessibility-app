import pikepdf

from app.services.pdf_objects import (
    pdf_dictionary,
    pdf_integer,
    pdf_name,
    pdf_named_resource,
    pdf_page_key,
    pdf_text,
)


def test_named_resources_require_a_dictionary_and_a_pdf_name():
    font = pikepdf.Dictionary(Type=pikepdf.Name.Font)
    resources = pikepdf.Dictionary(F1=font)
    assert pdf_named_resource(resources, pikepdf.Name.F1) == font
    assert pdf_named_resource(resources, pikepdf.String("/F1")) is None
    assert pdf_named_resource(pikepdf.Array(), pikepdf.Name.F1) is None
    assert pdf_named_resource(resources, pikepdf.Name.Missing) is None
    assert pdf_name(pikepdf.String("/F1")) is None


def test_page_reference_requires_a_dictionary():
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        assert pdf_dictionary(page.obj) == page.obj
        assert pdf_page_key(page.obj) == page.objgen
        assert pdf_page_key(pikepdf.Array([page.obj])) is None
        assert pdf_dictionary(pdf.make_stream(b"")) is None


def test_pdf_scalar_reads_use_defaults_for_wrong_types():
    values = pikepdf.Dictionary(
        Text=pikepdf.String("Hello"), Role=pikepdf.Name.H1, Span=2, Wrong=pikepdf.Array(),
    )
    assert pdf_text(values.get("/Text")) == "Hello"
    assert pdf_text(values.get("/Role")) == "/H1"
    assert pdf_text(values.get("/Missing")) == ""
    assert pdf_text(values.get("/Wrong"), "fallback") == "fallback"
    assert pdf_integer(values.get("/Span")) == 2
    assert pdf_integer(values.get("/Missing")) == 1
    assert pdf_integer(values.get("/Wrong")) == 1
