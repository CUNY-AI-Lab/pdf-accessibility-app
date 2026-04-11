from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.document_intelligence import build_document_model
from app.services.semantic_pretag_policy import (
    form_targets_for_intelligence,
    widget_targets_for_rationalization,
)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _has_native_toc_children(native_toc: Any) -> bool:
    if not isinstance(native_toc, dict):
        return False
    children = native_toc.get("children")
    return isinstance(children, list) and any(isinstance(child, dict) for child in children)


def docling_structure_escalation_plan(structure_json: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    structure = structure_json if isinstance(structure_json, dict) else {}
    title = _normalize_text(structure.get("title"))
    has_title = bool(title)
    has_native_toc = _has_native_toc_children(structure.get("native_toc"))

    return {
        "title": {
            "decision": "docling" if has_title else "gemini",
            "reason": (
                "docling_title_present"
                if has_title
                else "missing_docling_title"
            ),
            "signal": title,
        },
        "toc": {
            "decision": "gemini",
            "reason": "visible_toc_page_semantics_not_docling_authoritative",
            "signal": "visible_toc_requires_page_semantics",
        },
        "bookmarks": {
            "decision": "docling" if has_native_toc else "gemini",
            "reason": (
                "docling_native_toc_present"
                if has_native_toc
                else "missing_docling_native_toc"
            ),
            "signal": "native_toc" if has_native_toc else "",
        },
        "language": {
            "decision": "docling",
            "reason": "docling_element_language_metadata",
            "signal": _normalize_text(structure.get("language")),
        },
    }


def docling_table_targets_for_intelligence(
    structure_json: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    document = build_document_model(structure_json=structure_json)
    targets: list[dict[str, Any]] = []
    for page in document.pages:
        for table in page.tables:
            if table.num_rows < 2 or table.num_cols < 2:
                continue
            ambiguity_reasons: list[str] = []
            if not table.header_rows and not table.row_header_columns:
                ambiguity_reasons.append("missing_docling_simple_headers")
            if any(cell.row_span > 1 or cell.col_span > 1 for cell in table.cells):
                ambiguity_reasons.append("merged_cells_present")
            if not ambiguity_reasons:
                continue
            targets.append(
                {
                    "table_review_id": table.table_review_id,
                    "page": page.page_number,
                    "bbox": table.bbox.to_dict() if table.bbox else None,
                    "num_rows": table.num_rows,
                    "num_cols": table.num_cols,
                    "header_rows": list(table.header_rows),
                    "row_header_columns": list(table.row_header_columns),
                    "cells": [cell.to_dict() for cell in table.cells],
                    "text_excerpt": table.text_excerpt[:240],
                    "provenance": table.provenance,
                    "confidence": table.confidence,
                    "risk_reasons": list(ambiguity_reasons),
                    "risk_score": float(len(ambiguity_reasons)),
                }
            )
    return targets


def _lane_plan(
    *,
    targets: list[dict[str, Any]],
    ambiguous_reason: str,
    resolved_reason: str,
) -> dict[str, Any]:
    pages = sorted(
        {
            int(target["page"])
            for target in targets
            if isinstance(target, dict) and isinstance(target.get("page"), int)
        }
    )
    return {
        "decision": "gemini" if targets else "docling",
        "reason": ambiguous_reason if targets else resolved_reason,
        "candidate_count": len(targets),
        "pages": pages,
    }


def docling_pretag_ambiguity_router(
    *,
    working_pdf: Path | None,
    structure_json: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(structure_json, dict):
        empty_plan = {
            "forms": {
                "decision": "docling",
                "reason": "missing_structure",
                "candidate_count": 0,
                "pages": [],
            },
            "tables": {
                "decision": "docling",
                "reason": "missing_structure",
                "candidate_count": 0,
                "pages": [],
            },
            "widgets": {
                "decision": "docling",
                "reason": "missing_structure",
                "candidate_count": 0,
                "pages": [],
            },
        }
        return {
            "plan": empty_plan,
            "form_targets": [],
            "table_targets": [],
            "widget_targets": [],
        }

    table_targets = docling_table_targets_for_intelligence(structure_json)
    form_targets: list[dict[str, Any]] = []
    widget_targets: list[dict[str, Any]] = []
    if isinstance(working_pdf, Path) and working_pdf.exists():
        form_targets = form_targets_for_intelligence(
            working_pdf=working_pdf,
            structure_json=structure_json,
        )
        widget_targets = widget_targets_for_rationalization(
            working_pdf=working_pdf,
            structure_json=structure_json,
        )

    plan = {
        "forms": _lane_plan(
            targets=form_targets,
            ambiguous_reason="docling_fields_with_missing_or_weak_labels",
            resolved_reason="docling_field_labels_sufficient_or_absent",
        ),
        "tables": _lane_plan(
            targets=table_targets,
            ambiguous_reason="docling_tables_with_missing_simple_headers_or_spans",
            resolved_reason="docling_table_headers_sufficient_or_absent",
        ),
        "widgets": _lane_plan(
            targets=widget_targets,
            ambiguous_reason="docling_widget_candidates_require_rationalization",
            resolved_reason="no_docling_widget_rationalization_candidates",
        ),
    }
    return {
        "plan": plan,
        "form_targets": form_targets,
        "table_targets": table_targets,
        "widget_targets": widget_targets,
    }
