"""Normalize observed figure reply shapes without treating prose as a description."""

from typing import Any


def normalize_figure_decision(raw: dict[str, Any]) -> dict[str, Any]:
    result = dict(raw)
    action = raw.get("suggested_action") or raw.get("action") or raw.get("decision")
    text = next(
        (
            raw[key].strip()
            for key in ("alt_text", "set_alt_text", "resolved_alt_text", "resolved_text")
            if isinstance(raw.get(key), str) and raw[key].strip()
        ),
        "",
    )
    result["alt_text"] = text
    action = action.strip() if isinstance(action, str) else ""
    if action in {"keep_figure_semantics", "keep_figure"} and text:
        action = "set_alt_text"
    if not action and raw.get("mark_decorative") is True:
        action = "mark_decorative"
    result["suggested_action"] = (
        action
        if action
        in {
            "set_alt_text",
            "mark_decorative",
            "reclassify_region",
            "manual_only",
        }
        else "manual_only"
    )
    return result
