from pathlib import Path

import pikepdf

from app.services.form_fields import (
    apply_field_accessible_names,
    extract_widget_fields,
    field_label_quality,
)


def _pdf_with_parented_widget(path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    parent = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/FT": pikepdf.Name("/Tx"),
                "/T": pikepdf.String("f1_01[0]"),
            }
        )
    )
    widget = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Annot"),
                "/Subtype": pikepdf.Name("/Widget"),
                "/Rect": pikepdf.Array([10, 10, 80, 28]),
                "/Parent": parent,
            }
        )
    )
    parent["/Kids"] = pikepdf.Array([widget])
    page["/Annots"] = pikepdf.Array([widget])
    pdf.Root["/AcroForm"] = pikepdf.Dictionary(
        {
            "/Fields": pikepdf.Array([parent]),
        }
    )
    pdf.save(path)


def _pdf_with_container_tu(path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    top = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/T": pikepdf.String("topmostSubform[0]"),
                "/TU": pikepdf.String("Firm's EIN"),
            }
        )
    )
    page_container = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/T": pikepdf.String("Page1[0]"),
                "/Parent": top,
            }
        )
    )
    widget = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Annot"),
                "/Subtype": pikepdf.Name("/Widget"),
                "/FT": pikepdf.Name("/Btn"),
                "/T": pikepdf.String("c1_9[0]"),
                "/Rect": pikepdf.Array([10, 10, 18, 18]),
                "/Parent": page_container,
            }
        )
    )
    page_container["/Kids"] = pikepdf.Array([widget])
    top["/Kids"] = pikepdf.Array([page_container])
    page["/Annots"] = pikepdf.Array([widget])
    pdf.Root["/AcroForm"] = pikepdf.Dictionary(
        {
            "/Fields": pikepdf.Array([top]),
        }
    )
    pdf.save(path)


def test_extract_widget_fields_reports_missing_accessible_label(tmp_path):
    pdf_path = tmp_path / "widget.pdf"
    _pdf_with_parented_widget(pdf_path)

    fields = extract_widget_fields(pdf_path)

    assert len(fields) == 1
    field = fields[0]
    assert field["field_type"] == "text"
    assert field["field_name"] == "f1_01[0]"
    assert field["accessible_name"] == ""
    assert field["label_quality"] == "missing"
    assert field["bbox"] == {"l": 10.0, "t": 28.0, "r": 80.0, "b": 10.0}


def test_apply_field_accessible_names_sets_tu_on_parent_and_widget(tmp_path):
    pdf_path = tmp_path / "widget.pdf"
    output_path = tmp_path / "labeled.pdf"
    _pdf_with_parented_widget(pdf_path)
    fields = extract_widget_fields(pdf_path)

    applied = apply_field_accessible_names(
        input_pdf=pdf_path,
        output_pdf=output_path,
        labels_by_review_id={fields[0]["field_review_id"]: "First name and middle initial"},
    )

    assert applied == [fields[0]["field_review_id"]]
    with pikepdf.Pdf.open(output_path) as pdf:
        parent = pdf.Root["/AcroForm"]["/Fields"][0]
        widget = pdf.pages[0]["/Annots"][0]
        assert str(parent["/TU"]) == "First name and middle initial"
        assert str(widget["/TU"]) == "First name and middle initial"


def test_field_label_quality_treats_plain_address_as_good():
    assert field_label_quality(accessible_name="Address", field_name="f1_03[0]") == "good"


def test_field_label_quality_keeps_step_labels_with_digits_as_good():
    label = "Step 3(a) Multiply the number of qualifying children under age 17 by $2,200"
    assert field_label_quality(accessible_name=label, field_name="f1_06[0]") == "good"


def test_field_label_quality_allows_human_checkbox_labels():
    label = "6a Social security benefits, Lump-sum election method checkbox 3"
    assert field_label_quality(accessible_name=label, field_name="c1_37[0]") == "good"


def test_extract_widget_fields_ignores_container_level_tu(tmp_path):
    pdf_path = tmp_path / "container-tu.pdf"
    _pdf_with_container_tu(pdf_path)

    fields = extract_widget_fields(pdf_path)

    assert len(fields) == 1
    field = fields[0]
    assert field["field_name"] == "c1_9[0]"
    assert field["accessible_name"] == ""
    assert field["label_quality"] == "missing"


def test_apply_field_accessible_names_does_not_write_tu_to_containers(tmp_path):
    pdf_path = tmp_path / "container-tu.pdf"
    output_path = tmp_path / "container-tu-labeled.pdf"
    _pdf_with_container_tu(pdf_path)
    fields = extract_widget_fields(pdf_path)

    applied = apply_field_accessible_names(
        input_pdf=pdf_path,
        output_pdf=output_path,
        labels_by_review_id={fields[0]["field_review_id"]: "Qualified business income checkbox"},
    )

    assert applied == [fields[0]["field_review_id"]]
    with pikepdf.Pdf.open(output_path) as pdf:
        top = pdf.Root["/AcroForm"]["/Fields"][0]
        page_container = top["/Kids"][0]
        widget = pdf.pages[0]["/Annots"][0]
        assert str(widget["/TU"]) == "Qualified business income checkbox"
        assert str(top["/TU"]) == "Firm's EIN"
        assert "/TU" not in page_container
