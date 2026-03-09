from __future__ import annotations

from pathlib import Path
from typing import Any

import pikepdf


BUTTON_FLAG_RADIO = 1 << 15
BUTTON_FLAG_PUSHBUTTON = 1 << 16
CHOICE_FLAG_COMBO = 1 << 17


def _objgen_str(obj: Any) -> str | None:
    objgen = getattr(obj, "objgen", None)
    if (
        isinstance(objgen, tuple)
        and len(objgen) == 2
        and isinstance(objgen[0], int)
        and isinstance(objgen[1], int)
    ):
        return f"{objgen[0]} {objgen[1]}"
    return None


def _resolve_dictionary(obj: Any) -> pikepdf.Dictionary | None:
    return obj if isinstance(obj, pikepdf.Dictionary) else None


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _rect_to_bbox(rect: Any) -> dict[str, float] | None:
    if not isinstance(rect, pikepdf.Array) or len(rect) < 4:
        return None
    try:
        left = float(rect[0])
        bottom = float(rect[1])
        right = float(rect[2])
        top = float(rect[3])
    except Exception:
        return None
    if right <= left or top <= bottom:
        return None
    return {
        "l": left,
        "t": top,
        "r": right,
        "b": bottom,
    }


def _iter_field_lineage(widget: pikepdf.Dictionary) -> list[pikepdf.Dictionary]:
    lineage: list[pikepdf.Dictionary] = []
    current: pikepdf.Dictionary | None = widget
    seen: set[tuple[int, int]] = set()
    while isinstance(current, pikepdf.Dictionary):
        objgen = getattr(current, "objgen", None)
        if isinstance(objgen, tuple) and objgen in seen:
            break
        if isinstance(objgen, tuple):
            seen.add(objgen)
        lineage.append(current)
        parent = current.get("/Parent")
        current = parent if isinstance(parent, pikepdf.Dictionary) else None
    return lineage


def _semantic_field_entries(lineage: list[pikepdf.Dictionary]) -> list[pikepdf.Dictionary]:
    field_entries = [
        entry
        for entry in lineage
        if isinstance(entry, pikepdf.Dictionary) and entry.get("/FT") is not None
    ]
    if field_entries:
        return field_entries
    return lineage[:1]


def _lineage_attr(lineage: list[pikepdf.Dictionary], key: str) -> Any:
    for entry in lineage:
        value = entry.get(key)
        if value is not None:
            return value
    return None


def _field_type_name(ft_value: Any, ff_value: Any) -> str:
    ft = str(ft_value or "").strip()
    flags = int(ff_value) if isinstance(ff_value, int) else 0
    if ft == "/Tx":
        return "text"
    if ft == "/Btn":
        if flags & BUTTON_FLAG_PUSHBUTTON:
            return "push_button"
        if flags & BUTTON_FLAG_RADIO:
            return "radio_button"
        return "checkbox"
    if ft == "/Ch":
        if flags & CHOICE_FLAG_COMBO:
            return "combo_box"
        return "list_box"
    if ft == "/Sig":
        return "signature"
    return ft.lstrip("/") or "unknown"


def _is_technical_field_name(value: str) -> bool:
    text = _normalize_text(value)
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith("topmostsubform"):
        return True
    if any(token in lowered for token in ("[0]", ".page", ".subform", "textfield")):
        return True
    if any(char in "_[]" for char in text):
        return True
    if "." in text and " " not in text:
        return True
    if text.isalnum() and any(char.isdigit() for char in text):
        return True
    return False


def field_label_quality(*, accessible_name: str, field_name: str) -> str:
    label = _normalize_text(accessible_name)
    technical_name = _normalize_text(field_name)
    if not label:
        return "missing"
    if _is_technical_field_name(label):
        return "weak"
    if technical_name and label == technical_name and _is_technical_field_name(technical_name):
        return "weak"
    return "good"


def _field_review_id(widget: pikepdf.Dictionary, terminal_field: pikepdf.Dictionary, *, page_number: int, index: int) -> str:
    widget_obj = _objgen_str(widget)
    terminal_obj = _objgen_str(terminal_field)
    if widget_obj:
        return f"field-widget-{widget_obj.replace(' ', '-')}"
    if terminal_obj:
        return f"field-{terminal_obj.replace(' ', '-')}"
    return f"field-page-{page_number}-{index}"


def _iter_widget_field_entries(pdf: pikepdf.Pdf):
    for page_number, page in enumerate(pdf.pages, start=1):
        annots = page.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            continue
        widget_index = 0
        for annot in annots:
            widget = _resolve_dictionary(annot)
            if widget is None:
                continue
            try:
                if widget.get("/Subtype") != pikepdf.Name("/Widget"):
                    continue
            except Exception:
                continue
            widget_index += 1
            lineage = _iter_field_lineage(widget)
            field_entries = _semantic_field_entries(lineage)
            terminal_field = field_entries[0] if field_entries else widget
            ft_value = _lineage_attr(field_entries, "/FT")
            ff_value = _lineage_attr(field_entries, "/Ff")
            accessible_name = _normalize_text(_lineage_attr(field_entries, "/TU"))
            field_name = _normalize_text(_lineage_attr(field_entries, "/T"))
            value_text = _normalize_text(_lineage_attr(field_entries, "/V"))
            bbox = _rect_to_bbox(widget.get("/Rect") or _lineage_attr(lineage, "/Rect"))
            yield {
                "field_review_id": _field_review_id(widget, terminal_field, page_number=page_number, index=widget_index),
                "page": page_number,
                "order": widget_index,
                "field_type": _field_type_name(ft_value, ff_value),
                "field_name": field_name,
                "accessible_name": accessible_name,
                "value_text": value_text,
                "bbox": bbox,
                "widget_objgen": _objgen_str(widget),
                "field_objgen": _objgen_str(terminal_field),
                "label_quality": field_label_quality(
                    accessible_name=accessible_name,
                    field_name=field_name,
                ),
                "_widget": widget,
                "_field": terminal_field,
                "_field_entries": field_entries,
            }


def extract_widget_fields(pdf_path: Path) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    with pikepdf.Pdf.open(pdf_path) as pdf:
        for entry in _iter_widget_field_entries(pdf):
            fields.append(
                {
                    "field_review_id": entry["field_review_id"],
                    "page": entry["page"],
                    "order": entry["order"],
                    "field_type": entry["field_type"],
                    "field_name": entry["field_name"],
                    "accessible_name": entry["accessible_name"],
                    "value_text": entry["value_text"],
                    "bbox": entry["bbox"],
                    "widget_objgen": entry["widget_objgen"],
                    "field_objgen": entry["field_objgen"],
                    "label_quality": entry["label_quality"],
                }
            )
    return fields


def apply_field_accessible_names(
    *,
    input_pdf: Path,
    output_pdf: Path,
    labels_by_review_id: dict[str, str],
) -> list[str]:
    normalized_labels = {
        str(review_id).strip(): _normalize_text(label)
        for review_id, label in labels_by_review_id.items()
        if str(review_id).strip() and _normalize_text(label)
    }
    if not normalized_labels:
        output_pdf.write_bytes(input_pdf.read_bytes())
        return []

    applied: list[str] = []
    with pikepdf.Pdf.open(input_pdf) as pdf:
        for entry in _iter_widget_field_entries(pdf):
            review_id = str(entry["field_review_id"])
            label = normalized_labels.get(review_id)
            if not label:
                continue
            widget_target = entry["_widget"]
            field_targets = [
                target
                for target in entry.get("_field_entries", [])
                if isinstance(target, pikepdf.Dictionary)
            ]
            try:
                for field_target in field_targets:
                    field_target["/TU"] = pikepdf.String(label)
                widget_target["/TU"] = pikepdf.String(label)
                applied.append(review_id)
            except Exception:
                continue
        pdf.save(output_pdf)
    return applied
