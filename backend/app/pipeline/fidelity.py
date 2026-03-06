"""Deterministic post-remediation fidelity checks."""

from __future__ import annotations

import re
from bisect import bisect_right
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from pdfminer.high_level import extract_text

FONT_RULE_FRAGMENT = "-7.21."
TEXT_SAMPLE_MAX_PAGES = 10
TEXT_SAMPLE_MAX_CHARS = 20000
TEXT_SAMPLE_MIN_CHARS = 300
STRUCTURE_FRAGMENT_LIMIT = 24
STRUCTURE_FRAGMENT_MIN_LEN = 18
NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
TABLE_KEYWORDS = ("table", "thead", "tbody", "tfoot", "tr", "th", "td")


def _normalize_text(value: str | None) -> str:
    raw = str(value or "").lower()
    return " ".join(NORMALIZE_RE.sub(" ", raw).split())


def _extract_pdf_text_sample(path: Path) -> str:
    try:
        text = extract_text(
            str(path),
            page_numbers=list(range(TEXT_SAMPLE_MAX_PAGES)),
        )
    except Exception:
        return ""
    return _normalize_text(text)[:TEXT_SAMPLE_MAX_CHARS]


def _collect_structural_fragments(structure_json: dict[str, Any]) -> list[str]:
    elements = structure_json.get("elements", [])
    if not isinstance(elements, list):
        return []

    fragments: list[str] = []
    seen: set[str] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        text = _normalize_text(element.get("text"))
        if len(text) < STRUCTURE_FRAGMENT_MIN_LEN:
            continue
        fragment = text[:120]
        if fragment in seen:
            continue
        seen.add(fragment)
        fragments.append(fragment)
        if len(fragments) >= STRUCTURE_FRAGMENT_LIMIT:
            break
    return fragments


def _longest_nondecreasing_subsequence_len(values: list[int]) -> int:
    tails: list[int] = []
    for value in values:
        idx = bisect_right(tails, value)
        if idx == len(tails):
            tails.append(value)
        else:
            tails[idx] = value
    return len(tails)


def _reading_order_metrics(fragments: list[str], output_text: str) -> dict[str, float | int]:
    positions: list[int] = []
    for fragment in fragments:
        pos = output_text.find(fragment)
        if pos >= 0:
            positions.append(pos)

    hits = len(positions)
    hit_rate = hits / max(len(fragments), 1)
    ordered_hits = _longest_nondecreasing_subsequence_len(positions) if positions else 0
    order_rate = ordered_hits / max(hits, 1) if hits else 0.0

    return {
        "fragments_considered": len(fragments),
        "matched_fragments": hits,
        "ordered_fragments": ordered_hits,
        "hit_rate": round(hit_rate, 4),
        "order_rate": round(order_rate, 4),
    }


def _severity_rank(severity: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(severity, 0)


def _task_for_violation(violation: dict[str, Any]) -> tuple[str, str, str]:
    category = str(violation.get("category") or "").strip().lower()
    rule_id = str(violation.get("rule_id") or "")
    description = str(violation.get("description") or "").lower()

    if category == "fonts" or FONT_RULE_FRAGMENT in rule_id:
        return (
            "font_text_fidelity",
            "Verify font-to-text mapping",
            "Remaining font and Unicode errors can change what assistive technology reads.",
        )
    if category in {"annotations", "links"}:
        return (
            "annotation_description",
            "Verify annotations and link descriptions",
            "Annotations or links still need manual review for accessible naming and structure.",
        )
    if category == "figures" or "alternate text" in description or " alt " in f" {description} ":
        return (
            "alt_text",
            "Verify figure descriptions",
            "Some figure descriptions still require manual review for accuracy.",
        )
    if category == "tables" or any(keyword in description for keyword in TABLE_KEYWORDS):
        return (
            "table_semantics",
            "Verify table semantics",
            "Table structure still needs manual review for headers and reading order.",
        )
    return (
        "reading_order",
        "Verify reading order and structure",
        "Remaining structural issues may affect the document reading experience.",
    )


def assess_fidelity(
    *,
    input_pdf: Path,
    output_pdf: Path,
    structure_json: dict[str, Any],
    alt_entries: list[dict[str, Any]],
    validation_report: dict[str, Any],
    tagging_metrics: dict[str, Any],
    classification: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    tasks_by_key: dict[str, dict[str, Any]] = {}

    def add_task(
        key: str,
        *,
        task_type: str,
        title: str,
        detail: str,
        severity: str = "medium",
        blocking: bool = True,
        source: str = "fidelity",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        existing = tasks_by_key.get(key)
        if existing is None:
            tasks_by_key[key] = {
                "task_type": task_type,
                "title": title,
                "detail": detail,
                "severity": severity,
                "blocking": blocking,
                "status": "pending_review",
                "source": source,
                "metadata": metadata or {},
            }
            return

        if blocking and not existing["blocking"]:
            existing["blocking"] = True
        if _severity_rank(severity) > _severity_rank(str(existing.get("severity"))):
            existing["severity"] = severity
        if metadata:
            existing.setdefault("metadata", {}).update(metadata)

    violations = validation_report.get("violations", [])
    unresolved_errors = [
        violation
        for violation in violations
        if isinstance(violation, dict) and str(violation.get("severity")) == "error"
    ]

    if unresolved_errors:
        checks.append({
            "check": "compliance",
            "status": "fail",
            "message": f"{len(unresolved_errors)} validation rule(s) still require remediation.",
            "metrics": {
                "remaining_rules": len(unresolved_errors),
                "remaining_errors": validation_report.get("summary", {}).get("errors", 0),
            },
        })
        grouped: dict[str, dict[str, Any]] = {}
        for violation in unresolved_errors:
            task_type, title, detail = _task_for_violation(violation)
            key = f"review:{task_type}"
            entry = grouped.setdefault(
                key,
                {
                "task_type": task_type,
                "title": title,
                "detail": detail,
                "count": 0,
                "rules": [],
                },
            )
            count = violation.get("count", 1)
            entry["count"] += count if isinstance(count, int) and count > 0 else 1
            rule_id = str(violation.get("rule_id") or "").strip()
            if rule_id and rule_id not in entry["rules"]:
                entry["rules"].append(rule_id)

        for key, entry in grouped.items():
            add_task(
                key,
                task_type=entry["task_type"],
                title=entry["title"],
                detail=f"{entry['detail']} Remaining occurrences: {entry['count']}.",
                severity="high",
                blocking=True,
                source="validation",
                metadata={"rules": entry["rules"], "count": entry["count"]},
            )
    else:
        checks.append({
            "check": "compliance",
            "status": "pass",
            "message": "No remaining validation errors.",
            "metrics": {"remaining_rules": 0, "remaining_errors": 0},
        })

    source_text = _extract_pdf_text_sample(input_pdf)
    output_text = _extract_pdf_text_sample(output_pdf)
    if len(source_text) >= TEXT_SAMPLE_MIN_CHARS and len(output_text) >= TEXT_SAMPLE_MIN_CHARS:
        similarity = SequenceMatcher(None, source_text, output_text).ratio()
        length_ratio = len(output_text) / max(len(source_text), 1)
        status = "pass"
        if similarity < 0.82 or length_ratio < 0.7 or length_ratio > 1.45:
            status = "fail"
            add_task(
                "review:content_fidelity",
                task_type="content_fidelity",
                title="Verify extracted text fidelity",
                detail=(
                    "The remediated PDF text differs substantially from the source text sample. "
                    "Check for OCR drift, Unicode remaps, or missing content."
                ),
                severity="high",
                blocking=True,
                metadata={
                    "similarity": round(similarity, 4),
                    "length_ratio": round(length_ratio, 4),
                },
            )
        elif similarity < 0.9 or length_ratio < 0.85 or length_ratio > 1.2:
            status = "warning"
            add_task(
                "review:content_fidelity",
                task_type="content_fidelity",
                title="Spot-check extracted text fidelity",
                detail=(
                    "The remediated PDF text sample differs moderately from the source. "
                    "A manual spot-check is recommended."
                ),
                severity="medium",
                blocking=False,
                metadata={
                    "similarity": round(similarity, 4),
                    "length_ratio": round(length_ratio, 4),
                },
            )
        checks.append({
            "check": "text_drift",
            "status": status,
            "message": "Compared source and remediated text samples.",
            "metrics": {
                "similarity": round(similarity, 4),
                "length_ratio": round(length_ratio, 4),
                "source_chars": len(source_text),
                "output_chars": len(output_text),
            },
        })
    else:
        checks.append({
            "check": "text_drift",
            "status": "skip",
            "message": "Skipped text-drift comparison due to limited extractable text.",
            "metrics": {
                "source_chars": len(source_text),
                "output_chars": len(output_text),
                "classification": classification or "unknown",
            },
        })

    fragments = _collect_structural_fragments(structure_json)
    if fragments and output_text:
        order_metrics = _reading_order_metrics(fragments, output_text)
        hit_rate = float(order_metrics["hit_rate"])
        order_rate = float(order_metrics["order_rate"])
        status = "pass"
        if len(fragments) >= 8 and (hit_rate < 0.4 or order_rate < 0.55):
            status = "fail"
            add_task(
                "review:reading_order",
                task_type="reading_order",
                title="Verify reading order",
                detail=(
                    "Structural text fragments are not appearing in the remediated text in a stable order. "
                    "Manual reading-order review is required."
                ),
                severity="high",
                blocking=True,
                metadata=order_metrics,
            )
        elif len(fragments) >= 8 and (hit_rate < 0.65 or order_rate < 0.85):
            status = "warning"
            add_task(
                "review:reading_order",
                task_type="reading_order",
                title="Spot-check reading order",
                detail=(
                    "The structural text order signal is weaker than expected. "
                    "A manual reading-order spot-check is recommended."
                ),
                severity="medium",
                blocking=False,
                metadata=order_metrics,
            )

        checks.append({
            "check": "reading_order",
            "status": status,
            "message": "Compared structural text fragments against remediated text order.",
            "metrics": order_metrics,
        })
    else:
        checks.append({
            "check": "reading_order",
            "status": "skip",
            "message": "Skipped reading-order heuristic due to limited structural text.",
            "metrics": {"fragments_considered": len(fragments)},
        })

    elements = structure_json.get("elements", [])
    figure_captions: dict[int, str] = {}
    table_count = 0
    if isinstance(elements, list):
        for element in elements:
            if not isinstance(element, dict):
                continue
            if element.get("type") == "figure" and isinstance(element.get("figure_index"), int):
                figure_captions[element["figure_index"]] = _normalize_text(element.get("caption"))
            elif element.get("type") == "table":
                table_count += 1

    tagged_tables = tagging_metrics.get("tables_tagged", 0)
    tagged_table_count = tagged_tables if isinstance(tagged_tables, int) and tagged_tables >= 0 else 0
    if table_count > 0:
        coverage = tagged_table_count / table_count
        status = "pass" if tagged_table_count >= table_count else "warning"
        if tagged_table_count < table_count:
            add_task(
                "review:table_semantics",
                task_type="table_semantics",
                title="Verify table semantics",
                detail=(
                    "Not all detected tables were tagged in the final PDF. "
                    "Manual review is required for table headers and structure."
                ),
                severity="high" if coverage < 0.75 else "medium",
                blocking=True,
                metadata={
                    "detected_tables": table_count,
                    "tagged_tables": tagged_table_count,
                    "coverage": round(coverage, 4),
                },
            )
        checks.append({
            "check": "table_coverage",
            "status": status,
            "message": "Compared detected tables against tagged tables.",
            "metrics": {
                "detected_tables": table_count,
                "tagged_tables": tagged_table_count,
                "coverage": round(coverage, 4),
            },
        })
    else:
        checks.append({
            "check": "table_coverage",
            "status": "skip",
            "message": "No tables detected in structure extraction.",
            "metrics": {"detected_tables": 0, "tagged_tables": tagged_table_count},
        })

    machine_only_alt = 0
    caption_backed_alt = 0
    for entry in alt_entries:
        if not isinstance(entry, dict) or entry.get("status") != "approved":
            continue
        generated_text = _normalize_text(entry.get("generated_text"))
        edited_text = _normalize_text(entry.get("edited_text"))
        if not generated_text or edited_text:
            continue
        figure_index = entry.get("figure_index")
        caption_text = figure_captions.get(figure_index) if isinstance(figure_index, int) else ""
        if caption_text and generated_text == caption_text:
            caption_backed_alt += 1
        else:
            machine_only_alt += 1

    if machine_only_alt > 0:
        add_task(
            "review:alt_text",
            task_type="alt_text",
            title="Spot-check generated alt text",
            detail=(
                "Some figure descriptions were auto-approved without human edits. "
                "Spot-check them for accuracy before distribution."
            ),
            severity="medium",
            blocking=False,
            metadata={
                "machine_only_alt": machine_only_alt,
                "caption_backed_alt": caption_backed_alt,
            },
        )
        status = "warning"
    else:
        status = "pass"
    checks.append({
        "check": "alt_text_provenance",
        "status": status,
        "message": "Tracked whether approved alt text was human-edited, caption-backed, or machine-only.",
        "metrics": {
            "machine_only_alt": machine_only_alt,
            "caption_backed_alt": caption_backed_alt,
            "reviewed_alt_entries": len(alt_entries),
        },
    })

    font_remediation = validation_report.get("remediation", {}).get("font_remediation", {})
    unicode_gate = font_remediation.get("unicode_gate", {})
    font_diagnostics = (
        font_remediation.get("postflight_diagnostics")
        or font_remediation.get("preflight_diagnostics")
        or {}
    )
    font_diagnostics_summary = (
        font_diagnostics.get("summary")
        if isinstance(font_diagnostics, dict)
        else {}
    )
    top_font_profiles = []
    if isinstance(font_diagnostics, dict):
        raw_profiles = font_diagnostics.get("profiles")
        if isinstance(raw_profiles, list):
            for profile in raw_profiles[:3]:
                if not isinstance(profile, dict):
                    continue
                top_font_profiles.append({
                    "base_font": str(profile.get("base_font") or ""),
                    "subtype": str(profile.get("subtype") or ""),
                    "issue_tags": profile.get("issue_tags") if isinstance(profile.get("issue_tags"), list) else [],
                    "missing_used_code_count": int(profile.get("missing_used_code_count", 0) or 0),
                    "invalid_tounicode_entries": int(profile.get("invalid_tounicode_entries", 0) or 0),
                    "embedded": bool(profile.get("embedded", False)),
                })
    remaining_font_errors = sum(
        (
            violation.get("count", 1) if isinstance(violation.get("count"), int) and violation.get("count", 1) > 0 else 1
        )
        for violation in unresolved_errors
        if isinstance(violation, dict) and (
            str(violation.get("category") or "").lower() == "fonts"
            or FONT_RULE_FRAGMENT in str(violation.get("rule_id") or "")
        )
    )
    if remaining_font_errors > 0 or (
        isinstance(unicode_gate, dict) and not unicode_gate.get("allow_automatic", True)
    ):
        add_task(
            "review:font_text_fidelity",
            task_type="font_text_fidelity",
            title="Verify accessible text against visible text",
            detail=(
                "Font or Unicode mapping risk remains. Check that screen readers announce the same text "
                "that sighted readers see."
            ),
            severity="high",
            blocking=True,
            metadata={
                "remaining_font_errors": remaining_font_errors,
                "unicode_gate": unicode_gate,
                "font_diagnostics_summary": font_diagnostics_summary,
                "top_font_profiles": top_font_profiles,
            },
        )
        checks.append({
            "check": "font_text_fidelity",
            "status": "fail",
            "message": "Font-related compliance or Unicode-gate risk requires manual review.",
            "metrics": {
                "remaining_font_errors": remaining_font_errors,
                "automatic_unicode_allowed": bool(unicode_gate.get("allow_automatic", True))
                if isinstance(unicode_gate, dict)
                else True,
            },
        })
    else:
        checks.append({
            "check": "font_text_fidelity",
            "status": "pass",
            "message": "No residual font-text fidelity risk was detected.",
            "metrics": {"remaining_font_errors": 0},
        })

    tasks = sorted(
        tasks_by_key.values(),
        key=lambda task: (
            0 if task["blocking"] else 1,
            -_severity_rank(str(task.get("severity"))),
            str(task.get("task_type")),
        ),
    )
    blocking_tasks = [task for task in tasks if task["blocking"]]
    advisory_tasks = [task for task in tasks if not task["blocking"]]

    report = {
        "passed": len(blocking_tasks) == 0,
        "summary": {
            "blocking_tasks": len(blocking_tasks),
            "advisory_tasks": len(advisory_tasks),
            "total_tasks": len(tasks),
        },
        "checks": checks,
    }
    return report, tasks
