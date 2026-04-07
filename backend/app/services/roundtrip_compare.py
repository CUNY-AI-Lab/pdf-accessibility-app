from __future__ import annotations

import json
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import pikepdf

from app.pipeline.fidelity import (
    _canonical_named_destination,
    _collect_structural_fragments,
    _extract_pdf_text_sample,
    _is_generated_link_contents,
    _is_implausible_link_text,
    _is_poor_link_text,
    _reading_order_metrics,
)
from app.pipeline.structure import extract_structure
from app.services.form_fields import extract_widget_fields
from app.services.page_intelligence import normalize_visible_text, text_similarity_score

RECOVERABLE_STRUCTURE_TYPES = (
    "heading",
    "paragraph",
    "list_item",
    "table",
    "figure",
    "note",
    "code",
    "formula",
)


def load_roundtrip_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Round-trip manifest must be a JSON object.")
    return data


def _normalize_text(value: Any) -> str:
    return normalize_visible_text(str(value or "")).lower()


def _split_alpha_digit_boundaries(value: str) -> str:
    if not value:
        return ""

    parts: list[str] = []
    previous = ""
    for character in value:
        if previous and (
            (previous.isalpha() and character.isdigit())
            or (previous.isdigit() and character.isalpha())
        ):
            parts.append(" ")
        parts.append(character)
        previous = character
    return "".join(parts)


def _normalize_loose_text(value: Any) -> str:
    normalized = _split_alpha_digit_boundaries(_normalize_text(value))
    if not normalized:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", normalized)).strip()


def _normalize_title_text(value: Any) -> str:
    normalized = _split_alpha_digit_boundaries(_normalize_text(value))
    if not normalized:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", normalized)).strip()


def _primary_lang_subtag(value: Any) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        return ""
    return normalized.split("-", 1)[0]


def _language_tags_match(actual: Any, expected: Any) -> bool:
    actual_primary = _primary_lang_subtag(actual)
    expected_primary = _primary_lang_subtag(expected)
    return bool(actual_primary and expected_primary and actual_primary == expected_primary)


def _matches_text(actual: Any, expected: Any, *, exact: bool = False) -> bool:
    actual_text = _normalize_text(actual)
    expected_text = _normalize_text(expected)
    if not actual_text or not expected_text:
        return False
    return actual_text == expected_text if exact else expected_text in actual_text


def _matches_ordered_terms(actual: Any, expected: Any) -> bool:
    actual_terms = [term for term in _normalize_loose_text(actual).split() if term]
    expected_terms = [term for term in _normalize_loose_text(expected).split() if term]
    if not actual_terms or not expected_terms:
        return False

    cursor = 0
    for expected_term in expected_terms:
        try:
            cursor = actual_terms.index(expected_term, cursor) + 1
        except ValueError:
            return False
    return True


def _matches_contiguous_terms(actual: Any, expected: Any) -> bool:
    actual_terms = [term for term in _normalize_loose_text(actual).split() if term]
    expected_terms = [term for term in _normalize_loose_text(expected).split() if term]
    if not actual_terms or not expected_terms or len(expected_terms) > len(actual_terms):
        return False

    window = len(expected_terms)
    return any(actual_terms[index : index + window] == expected_terms for index in range(len(actual_terms) - window + 1))


def _matches_loose_text(actual: Any, expected: Any, *, exact: bool = False) -> bool:
    actual_text = _normalize_loose_text(actual)
    expected_text = _normalize_loose_text(expected)
    if not actual_text or not expected_text:
        return False
    return actual_text == expected_text if exact else expected_text in actual_text


def _matches_title_text(actual: Any, expected: Any, *, exact: bool = False) -> bool:
    actual_text = _normalize_title_text(actual)
    expected_text = _normalize_title_text(expected)
    if not actual_text or not expected_text:
        return False
    return actual_text == expected_text if exact else expected_text in actual_text


def _matches_any_text(actual: Any, expected_values: list[Any], *, exact: bool = False) -> bool:
    return any(_matches_text(actual, expected, exact=exact) for expected in expected_values)


def _normalize_terms(values: Any) -> list[str]:
    if isinstance(values, (str, int, float)):
        normalized = _normalize_text(values)
        return [normalized] if normalized else []
    if not isinstance(values, list):
        return []
    normalized_terms: list[str] = []
    for value in values:
        text = _normalize_text(value)
        if text:
            normalized_terms.append(text)
    return normalized_terms


def _type_counts(structure_json: dict[str, Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    elements = structure_json.get("elements", [])
    if not isinstance(elements, list):
        return {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        elem_type = str(element.get("type") or "").strip()
        if not elem_type:
            continue
        counts[elem_type] += 1
    return dict(sorted(counts.items()))


def _structure_type_metrics(
    gold_counts: dict[str, int],
    candidate_counts: dict[str, int],
) -> dict[str, Any]:
    tracked_types: dict[str, dict[str, int | float | None]] = {}
    matched_total = 0
    gold_total = 0

    for elem_type in RECOVERABLE_STRUCTURE_TYPES:
        gold = int(gold_counts.get(elem_type, 0) or 0)
        candidate = int(candidate_counts.get(elem_type, 0) or 0)
        if gold <= 0 and candidate <= 0:
            continue
        matched = min(gold, candidate)
        gold_total += gold
        matched_total += matched
        tracked_types[elem_type] = {
            "gold": gold,
            "candidate": candidate,
            "matched": matched,
            "recall": round(matched / gold, 4) if gold else None,
            "surplus": max(candidate - gold, 0),
        }

    return {
        "tracked_types": tracked_types,
        "gold_total": gold_total,
        "matched_total": matched_total,
        "recoverable_type_recall": round(matched_total / gold_total, 4) if gold_total else None,
    }


def _structure_transcript(structure_json: dict[str, Any]) -> str:
    elements = structure_json.get("elements", [])
    if not isinstance(elements, list):
        return ""

    parts: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        elem_type = str(element.get("type") or "")
        if elem_type == "artifact" or elem_type.startswith("toc_"):
            continue
        text = normalize_visible_text(element.get("text"))
        if text:
            parts.append(text)
            continue
        if elem_type == "figure":
            caption = normalize_visible_text(element.get("caption"))
            if caption:
                parts.append(caption)
    return " ".join(parts)


def _outline_titles(pdf: pikepdf.Pdf) -> list[str]:
    titles: list[str] = []
    try:
        with pdf.open_outline() as outline:
            stack = list(getattr(outline, "root", []))
            while stack:
                item = stack.pop(0)
                title = normalize_visible_text(getattr(item, "title", ""))
                if title:
                    titles.append(title)
                children = list(getattr(item, "children", []))
                if children:
                    stack[0:0] = children
    except Exception:
        return []
    return titles


def _page_object_lookup(pdf: pikepdf.Pdf) -> dict[tuple[int, int], int]:
    lookup: dict[tuple[int, int], int] = {}
    for page_number, page in enumerate(pdf.pages, start=1):
        try:
            objgen = tuple(page.obj.objgen)
        except Exception:
            continue
        if len(objgen) == 2:
            lookup[(int(objgen[0]), int(objgen[1]))] = page_number
    return lookup


def _page_number_for_destination_ref(page_lookup: dict[tuple[int, int], int], value: Any) -> int | None:
    try:
        objgen = tuple(value.objgen)
    except Exception:
        return None
    if len(objgen) != 2:
        return None
    return page_lookup.get((int(objgen[0]), int(objgen[1])))


def _canonical_link_destination(
    pdf: pikepdf.Pdf,
    value: Any,
    *,
    page_lookup: dict[tuple[int, int], int] | None = None,
) -> str:
    if value is None:
        return ""
    if isinstance(value, pikepdf.Array) and len(value) >= 1:
        page_lookup = page_lookup or _page_object_lookup(pdf)
        page_number = _page_number_for_destination_ref(page_lookup, value[0])
        if page_number is not None:
            parts = [f"page:{page_number}"]
            if len(value) >= 2:
                view_mode = _canonical_named_destination(value[1])
                if view_mode:
                    parts.append(view_mode)
            numeric_parts: list[str] = []
            for index in range(2, min(len(value), 5)):
                item = value[index]
                if item is None:
                    numeric_parts.append("")
                    continue
                try:
                    numeric_parts.append(f"{float(item):.2f}")
                except Exception:
                    numeric_parts.append(_canonical_named_destination(item) or "")
            if numeric_parts:
                parts.append(",".join(numeric_parts))
            return "|".join(parts)
    return _canonical_named_destination(value) or ""


def _extract_link_entries(pdf: pikepdf.Pdf) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    page_lookup = _page_object_lookup(pdf)
    for page_number, page in enumerate(pdf.pages, start=1):
        annots = page.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            continue
        link_order = 0
        for annot in annots:
            try:
                if annot.get("/Subtype") != pikepdf.Name("/Link"):
                    continue
            except Exception:
                continue
            link_order += 1
            contents = normalize_visible_text(annot.get("/Contents"))
            action = annot.get("/A")
            uri = ""
            dest = ""
            if action is not None and hasattr(action, "get"):
                uri = normalize_visible_text(action.get("/URI"))
                if str(action.get("/S", "")) == "/GoTo":
                    dest = _canonical_link_destination(pdf, action.get("/D"), page_lookup=page_lookup)
            if not dest:
                dest = _canonical_link_destination(pdf, annot.get("/Dest"), page_lookup=page_lookup)
            generated = _is_generated_link_contents(contents, uri=uri, has_dest=bool(dest))
            poor = (_is_poor_link_text(contents) or _is_implausible_link_text(contents)) if contents else True
            links.append(
                {
                    "page": page_number,
                    "order": link_order,
                    "contents": contents,
                    "uri": uri,
                    "dest": dest,
                    "descriptive": bool(contents) and not generated and not poor,
                }
            )
    return links


def _pdf_features(pdf_path: Path) -> dict[str, Any]:
    with pikepdf.Pdf.open(pdf_path) as pdf:
        title = normalize_visible_text(pdf.docinfo.get("/Title"))
        lang = normalize_visible_text(pdf.Root.get("/Lang"))
        pages = len(pdf.pages)
        outline_titles = _outline_titles(pdf)
        links = _extract_link_entries(pdf)

    fields = extract_widget_fields(pdf_path)
    raw_text = _extract_pdf_text_sample(pdf_path)
    return {
        "title": title,
        "lang": lang,
        "pages": pages,
        "outline_titles": outline_titles,
        "links": links,
        "fields": fields,
        "raw_text": raw_text,
    }


def _field_key(field: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(field.get("page") or 0),
        int(field.get("order") or 0),
        str(field.get("field_type") or ""),
        _normalize_text(field.get("field_name")),
    )


def _link_key(link: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(link.get("page") or 0),
        int(link.get("order") or 0),
        _normalize_text(link.get("uri")),
        _normalize_text(link.get("dest")),
    )


def _compare_fields(gold_fields: list[dict[str, Any]], candidate_fields: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_by_key = {_field_key(field): field for field in candidate_fields}
    named_gold_fields = [field for field in gold_fields if normalize_visible_text(field.get("accessible_name"))]
    matched_fields = 0
    matched_types = 0
    matched_named = 0
    missing: list[dict[str, Any]] = []

    for gold_field in gold_fields:
        candidate_field = candidate_by_key.get(_field_key(gold_field))
        if candidate_field:
            matched_fields += 1
            if _normalize_text(candidate_field.get("field_type")) == _normalize_text(gold_field.get("field_type")):
                matched_types += 1
    for gold_field in named_gold_fields:
        candidate_field = candidate_by_key.get(_field_key(gold_field))
        gold_label = _normalize_text(gold_field.get("accessible_name"))
        candidate_label = _normalize_text(candidate_field.get("accessible_name")) if candidate_field else ""
        if candidate_field and gold_label and gold_label == candidate_label:
            matched_named += 1
            continue
        missing.append(
            {
                "page": gold_field.get("page"),
                "field_name": gold_field.get("field_name"),
                "expected_accessible_name": gold_field.get("accessible_name"),
                "actual_accessible_name": candidate_field.get("accessible_name") if candidate_field else "",
            }
        )

    return {
        "gold_total": len(gold_fields),
        "candidate_total": len(candidate_fields),
        "matched_fields": matched_fields,
        "field_presence_match_rate": round(matched_fields / max(len(gold_fields), 1), 4)
        if gold_fields
        else None,
        "matched_field_types": matched_types,
        "field_type_match_rate": round(matched_types / max(len(gold_fields), 1), 4)
        if gold_fields
        else None,
        "gold_named_fields": len(named_gold_fields),
        "matched_named_fields": matched_named,
        "named_field_match_rate": round(matched_named / max(len(named_gold_fields), 1), 4)
        if named_gold_fields
        else None,
        "mismatches": missing[:10],
    }


def _compare_links(gold_links: list[dict[str, Any]], candidate_links: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_by_key = {_link_key(link): link for link in candidate_links}
    descriptive_gold_links = [link for link in gold_links if bool(link.get("descriptive"))]
    matched_descriptive = 0
    missing: list[dict[str, Any]] = []

    for gold_link in descriptive_gold_links:
        candidate_link = candidate_by_key.get(_link_key(gold_link))
        gold_contents = _normalize_text(gold_link.get("contents"))
        candidate_contents = _normalize_text(candidate_link.get("contents")) if candidate_link else ""
        if candidate_link and gold_contents and (
            gold_contents == candidate_contents
            or _matches_loose_text(candidate_link.get("contents"), gold_link.get("contents"), exact=True)
            or _matches_ordered_terms(candidate_link.get("contents"), gold_link.get("contents"))
        ):
            matched_descriptive += 1
            continue
        missing.append(
            {
                "page": gold_link.get("page"),
                "uri": gold_link.get("uri"),
                "expected_contents": gold_link.get("contents"),
                "actual_contents": candidate_link.get("contents") if candidate_link else "",
            }
        )

    return {
        "gold_total": len(gold_links),
        "candidate_total": len(candidate_links),
        "gold_descriptive_links": len(descriptive_gold_links),
        "matched_descriptive_links": matched_descriptive,
        "descriptive_link_match_rate": round(
            matched_descriptive / max(len(descriptive_gold_links), 1), 4
        )
        if descriptive_gold_links
        else None,
        "mismatches": missing[:10],
    }


def _compare_bookmarks(
    gold_titles: list[str],
    candidate_titles: list[str],
    *,
    gold_visible_sources: list[str] | None = None,
) -> dict[str, Any]:
    recoverable_gold_total = 0
    recoverable_matched = 0
    higher_order_gold_total = 0
    higher_order_matched = 0
    matched = 0
    missing: list[str] = []
    missing_recoverable: list[str] = []
    missing_higher_order: list[str] = []
    for gold_title in gold_titles:
        title_matched = any(
            _matches_title_text(candidate_title, gold_title)
            or _matches_ordered_terms(candidate_title, gold_title)
            for candidate_title in candidate_titles
            if candidate_title
        )
        if title_matched:
            matched += 1
        else:
            missing.append(gold_title)

        recoverable = any(
            _matches_contiguous_terms(source, gold_title)
            for source in (gold_visible_sources or [])
            if source
        )
        if recoverable:
            recoverable_gold_total += 1
            if title_matched:
                recoverable_matched += 1
            else:
                missing_recoverable.append(gold_title)
            continue

        higher_order_gold_total += 1
        if title_matched:
            higher_order_matched += 1
        else:
            missing_higher_order.append(gold_title)
    return {
        "gold_total": len(gold_titles),
        "candidate_total": len(candidate_titles),
        "matched_titles": matched,
        "bookmark_match_rate": round(matched / max(len(gold_titles), 1), 4) if gold_titles else None,
        "missing_titles": missing[:10],
        "recoverable_gold_total": recoverable_gold_total,
        "recoverable_matched_titles": recoverable_matched,
        "recoverable_bookmark_match_rate": round(
            recoverable_matched / max(recoverable_gold_total, 1),
            4,
        )
        if recoverable_gold_total
        else None,
        "missing_recoverable_titles": missing_recoverable[:10],
        "higher_order_gold_total": higher_order_gold_total,
        "higher_order_matched_titles": higher_order_matched,
        "higher_order_bookmark_match_rate": round(
            higher_order_matched / max(higher_order_gold_total, 1),
            4,
        )
        if higher_order_gold_total
        else None,
        "missing_higher_order_titles": missing_higher_order[:10],
    }


def _text_probe_sources(features: dict[str, Any]) -> list[str]:
    sources = [
        normalize_visible_text(features.get("raw_text")),
        normalize_visible_text(features.get("structure_transcript")),
    ]
    return [source for source in sources if source]


def _bookmark_probe_sources(structure_json: dict[str, Any]) -> list[str]:
    elements = structure_json.get("elements", [])
    if not isinstance(elements, list):
        return []

    bookmark_like_types = {
        "heading",
        "title",
        "subtitle",
        "toc_caption",
        "toc_group_heading",
        "toc_item",
        "toc_item_table",
    }
    sources: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        elem_type = str(element.get("type") or "")
        if elem_type not in bookmark_like_types:
            continue
        text = normalize_visible_text(element.get("text"))
        if text:
            sources.append(text)
    return sources


def _ordered_text_present(sources: list[str], expected_parts: list[str]) -> bool:
    normalized_parts = [_normalize_text(part) for part in expected_parts if _normalize_text(part)]
    if not normalized_parts:
        return False
    for source in sources:
        probe = _normalize_text(source)
        cursor = 0
        matched = True
        for part in normalized_parts:
            index = probe.find(part, cursor)
            if index < 0:
                matched = False
                break
            cursor = index + len(part)
        if matched:
            return True
    return False


def _find_matching_fields(fields: list[dict[str, Any]], assertion: dict[str, Any]) -> list[dict[str, Any]]:
    page = assertion.get("page")
    field_name = assertion.get("field_name")
    field_type = assertion.get("field_type")
    matches: list[dict[str, Any]] = []
    for field in fields:
        if page is not None and int(field.get("page") or 0) != int(page):
            continue
        if field_type and str(field.get("field_type") or "") != str(field_type):
            continue
        if field_name and _normalize_text(field.get("field_name")) != _normalize_text(field_name):
            continue
        matches.append(field)
    return matches


def _match_field_assertion(fields: list[dict[str, Any]], assertion: dict[str, Any]) -> bool:
    expected = assertion.get("expected")
    expected_any = assertion.get("expected_any")
    match_mode = str(assertion.get("match_mode") or "exact").strip().lower()
    required_terms_all = _normalize_terms(assertion.get("required_terms_all"))
    required_terms_any = _normalize_terms(assertion.get("required_terms_any"))
    for field in _find_matching_fields(fields, assertion):
        accessible_name = field.get("accessible_name")
        if expected_any:
            if isinstance(expected_any, list) and _matches_any_text(accessible_name, expected_any, exact=True):
                return True
            continue
        if expected is not None:
            if _matches_text(accessible_name, expected, exact=match_mode == "exact"):
                return True
            continue
        normalized_name = _normalize_text(accessible_name)
        if not normalized_name:
            continue
        if required_terms_all and not all(term in normalized_name for term in required_terms_all):
            continue
        if required_terms_any and not any(term in normalized_name for term in required_terms_any):
            continue
        if required_terms_all or required_terms_any:
            return True
    return False


def _match_link_assertion(links: list[dict[str, Any]], assertion: dict[str, Any]) -> bool:
    expected = assertion.get("expected")
    page = assertion.get("page")
    uri = assertion.get("uri")
    dest = assertion.get("dest")
    for link in links:
        if page is not None and int(link.get("page") or 0) != int(page):
            continue
        if uri and _normalize_text(link.get("uri")) != _normalize_text(uri):
            continue
        if dest and _normalize_text(link.get("dest")) != _normalize_text(dest):
            continue
        if _matches_text(link.get("contents"), expected, exact=True):
            return True
        if _matches_loose_text(link.get("contents"), expected, exact=True):
            return True
        if _matches_ordered_terms(link.get("contents"), expected):
            return True
    return False


def _match_assertion(features: dict[str, Any], assertion: dict[str, Any]) -> bool:
    kind = str(assertion.get("kind") or "").strip()
    expected = assertion.get("expected")
    sources = _text_probe_sources(features)

    if kind == "document_lang":
        return _language_tags_match(features.get("lang"), expected)
    if kind == "title_equals":
        return _matches_text(features.get("title"), expected, exact=True) or _matches_title_text(
            features.get("title"),
            expected,
            exact=True,
        )
    if kind == "title_contains":
        return (
            _matches_text(features.get("title"), expected, exact=False)
            or _matches_title_text(features.get("title"), expected, exact=False)
            or _matches_ordered_terms(features.get("title"), expected)
        )
    if kind == "text_contains":
        return any(_matches_text(source, expected, exact=False) for source in sources)
    if kind == "ordered_text":
        sequence = assertion.get("expected")
        return isinstance(sequence, list) and _ordered_text_present(sources, sequence)
    if kind == "field_present":
        return bool(_find_matching_fields(features.get("fields", []), assertion))
    if kind == "field_type":
        return bool(_find_matching_fields(features.get("fields", []), assertion))
    if kind == "field_accessible_name":
        return _match_field_assertion(features.get("fields", []), assertion)
    if kind == "link_contents":
        return _match_link_assertion(features.get("links", []), assertion)
    if kind == "bookmark_title":
        return any(
            _matches_text(title, expected, exact=False) or _matches_loose_text(title, expected, exact=False)
            for title in features.get("outline_titles", [])
        )
    raise ValueError(f"Unsupported round-trip assertion kind: {kind}")


def _evaluate_assertions(
    assertions: list[dict[str, Any]],
    *,
    gold_features: dict[str, Any],
    candidate_features: dict[str, Any],
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    passed = 0
    invalid = 0

    for index, assertion in enumerate(assertions, start=1):
        if not isinstance(assertion, dict):
            continue
        assertion_id = str(assertion.get("id") or f"assertion_{index}")
        kind = str(assertion.get("kind") or "")
        try:
            gold_holds = _match_assertion(gold_features, assertion)
            candidate_holds = _match_assertion(candidate_features, assertion)
            status = "pass" if candidate_holds else "fail"
            if not gold_holds:
                status = "invalid"
                invalid += 1
            elif candidate_holds:
                passed += 1
            results.append(
                {
                    "id": assertion_id,
                    "kind": kind,
                    "status": status,
                    "gold_holds": gold_holds,
                    "candidate_holds": candidate_holds,
                    "expected": assertion.get("expected"),
                }
            )
        except ValueError as exc:
            invalid += 1
            results.append(
                {
                    "id": assertion_id,
                    "kind": kind,
                    "status": "invalid",
                    "gold_holds": False,
                    "candidate_holds": False,
                    "error": str(exc),
                }
            )

    return {
        "total": len(results),
        "passed": passed,
        "failed": sum(1 for result in results if result["status"] == "fail"),
        "invalid": invalid,
        "results": results,
    }


def _compare_structure(gold_structure: dict[str, Any], candidate_structure: dict[str, Any]) -> dict[str, Any]:
    gold_type_counts = _type_counts(gold_structure)
    candidate_type_counts = _type_counts(candidate_structure)
    gold_fragments = _collect_structural_fragments(gold_structure)
    candidate_transcript = _structure_transcript(candidate_structure)
    gold_transcript = _structure_transcript(gold_structure)
    reading_order = (
        _reading_order_metrics(gold_fragments, candidate_transcript)
        if gold_fragments and candidate_transcript
        else {
            "fragments_considered": len(gold_fragments),
            "matched_fragments": 0,
            "ordered_fragments": 0,
            "hit_rate": 0.0,
            "order_rate": 0.0,
            "match_mode": "none",
        }
    )
    type_metrics = _structure_type_metrics(gold_type_counts, candidate_type_counts)
    return {
        "gold_type_counts": gold_type_counts,
        "candidate_type_counts": candidate_type_counts,
        "type_metrics": type_metrics,
        "gold_fragments": len(gold_fragments),
        "candidate_fragments": len(_collect_structural_fragments(candidate_structure)),
        "transcript_similarity": round(text_similarity_score(gold_transcript, candidate_transcript), 4),
        "reading_order": reading_order,
        "gold_transcript_chars": len(gold_transcript),
        "candidate_transcript_chars": len(candidate_transcript),
    }


async def _ensure_structure(
    *,
    pdf_path: Path,
    structure_json: dict[str, Any] | None,
    work_dir: Path,
    label: str,
) -> dict[str, Any]:
    if structure_json is not None:
        return structure_json
    job_dir = work_dir / label
    job_dir.mkdir(parents=True, exist_ok=True)
    result = await extract_structure(pdf_path, job_dir, include_figure_images=False)
    return result.document_json


async def _compare_roundtrip_with_workdir(
    *,
    gold_pdf: Path,
    candidate_pdf: Path,
    manifest: dict[str, Any] | None,
    work_dir: Path,
    gold_structure_json: dict[str, Any] | None,
    candidate_structure_json: dict[str, Any] | None,
) -> dict[str, Any]:
    gold_structure = await _ensure_structure(
        pdf_path=gold_pdf,
        structure_json=gold_structure_json,
        work_dir=work_dir,
        label="gold_structure",
    )
    candidate_structure = await _ensure_structure(
        pdf_path=candidate_pdf,
        structure_json=candidate_structure_json,
        work_dir=work_dir,
        label="candidate_structure",
    )

    gold_features = _pdf_features(gold_pdf)
    candidate_features = _pdf_features(candidate_pdf)
    gold_features["structure_transcript"] = _structure_transcript(gold_structure)
    candidate_features["structure_transcript"] = _structure_transcript(candidate_structure)

    manifest_data = manifest or {}
    recoverable_assertions = manifest_data.get("recoverable_assertions", [])
    hidden_assertions = manifest_data.get("hidden_semantics_assertions", [])
    if not isinstance(recoverable_assertions, list):
        raise ValueError("recoverable_assertions must be a list when present.")
    if not isinstance(hidden_assertions, list):
        raise ValueError("hidden_semantics_assertions must be a list when present.")

    metadata = {
        "page_count_match": gold_features["pages"] == candidate_features["pages"],
        "document_lang_match": _language_tags_match(
            gold_features["lang"],
            candidate_features["lang"],
        ),
        "title_match": _matches_text(candidate_features["title"], gold_features["title"], exact=False)
        or _matches_text(gold_features["title"], candidate_features["title"], exact=False)
        or _matches_title_text(candidate_features["title"], gold_features["title"], exact=False)
        or _matches_title_text(gold_features["title"], candidate_features["title"], exact=False)
        or _matches_ordered_terms(candidate_features["title"], gold_features["title"])
        or _matches_ordered_terms(gold_features["title"], candidate_features["title"]),
        "raw_text_similarity": round(
            text_similarity_score(gold_features["raw_text"], candidate_features["raw_text"]),
            4,
        ),
        "gold_raw_text_chars": len(gold_features["raw_text"]),
        "candidate_raw_text_chars": len(candidate_features["raw_text"]),
    }

    comparisons = {
        "structure": _compare_structure(gold_structure, candidate_structure),
        "fields": _compare_fields(gold_features["fields"], candidate_features["fields"]),
        "links": _compare_links(gold_features["links"], candidate_features["links"]),
        "bookmarks": _compare_bookmarks(
            gold_features["outline_titles"],
            candidate_features["outline_titles"],
            gold_visible_sources=_bookmark_probe_sources(gold_structure),
        ),
    }

    assertion_results = {
        "recoverable": _evaluate_assertions(
            recoverable_assertions,
            gold_features=gold_features,
            candidate_features=candidate_features,
        ),
        "hidden_semantics": _evaluate_assertions(
            hidden_assertions,
            gold_features=gold_features,
            candidate_features=candidate_features,
        ),
    }

    return {
        "gold_pdf": str(gold_pdf),
        "candidate_pdf": str(candidate_pdf),
        "manifest_counts": {
            "recoverable_assertions": len(recoverable_assertions),
            "hidden_semantics_assertions": len(hidden_assertions),
        },
        "metadata": metadata,
        "comparisons": comparisons,
        "assertions": assertion_results,
    }


async def compare_roundtrip_pdfs(
    *,
    gold_pdf: Path,
    candidate_pdf: Path,
    manifest: dict[str, Any] | None = None,
    work_dir: Path | None = None,
    gold_structure_json: dict[str, Any] | None = None,
    candidate_structure_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        return await _compare_roundtrip_with_workdir(
            gold_pdf=gold_pdf,
            candidate_pdf=candidate_pdf,
            manifest=manifest,
            work_dir=work_dir,
            gold_structure_json=gold_structure_json,
            candidate_structure_json=candidate_structure_json,
        )

    with tempfile.TemporaryDirectory(prefix="roundtrip-compare-") as temp_dir:
        return await _compare_roundtrip_with_workdir(
            gold_pdf=gold_pdf,
            candidate_pdf=candidate_pdf,
            manifest=manifest,
            work_dir=Path(temp_dir),
            gold_structure_json=gold_structure_json,
            candidate_structure_json=candidate_structure_json,
        )


def render_roundtrip_markdown(report: dict[str, Any]) -> str:
    metadata = report.get("metadata", {})
    structure = report.get("comparisons", {}).get("structure", {})
    fields = report.get("comparisons", {}).get("fields", {})
    links = report.get("comparisons", {}).get("links", {})
    bookmarks = report.get("comparisons", {}).get("bookmarks", {})
    recoverable = report.get("assertions", {}).get("recoverable", {})
    hidden = report.get("assertions", {}).get("hidden_semantics", {})

    lines = [
        "# Round-Trip Comparison",
        "",
        f"- Gold PDF: `{report.get('gold_pdf', '')}`",
        f"- Candidate PDF: `{report.get('candidate_pdf', '')}`",
        "",
        "## Automatic Checks",
        "",
        f"- Page count match: `{metadata.get('page_count_match')}`",
        f"- Document language match: `{metadata.get('document_lang_match')}`",
        f"- Title match: `{metadata.get('title_match')}`",
        f"- Raw text similarity: `{metadata.get('raw_text_similarity')}`",
        f"- Structure transcript similarity: `{structure.get('transcript_similarity')}`",
        f"- Recoverable structure-type recall: `{structure.get('type_metrics', {}).get('recoverable_type_recall')}`",
        f"- Reading-order hit rate: `{structure.get('reading_order', {}).get('hit_rate')}`",
        f"- Reading-order order rate: `{structure.get('reading_order', {}).get('order_rate')}`",
        f"- Field presence match rate: `{fields.get('field_presence_match_rate')}`",
        f"- Field type match rate: `{fields.get('field_type_match_rate')}`",
        f"- Named field match rate: `{fields.get('named_field_match_rate')}`",
        f"- Descriptive link match rate: `{links.get('descriptive_link_match_rate')}`",
        f"- Bookmark match rate: `{bookmarks.get('bookmark_match_rate')}`",
        f"- Recoverable bookmark match rate: `{bookmarks.get('recoverable_bookmark_match_rate')}`",
        f"- Higher-order bookmark match rate: `{bookmarks.get('higher_order_bookmark_match_rate')}`",
        "",
        "## Assertions",
        "",
        f"- Recoverable assertions: `{recoverable.get('passed', 0)}/{recoverable.get('total', 0)}` passed",
        f"- Hidden-semantics assertions: `{hidden.get('passed', 0)}/{hidden.get('total', 0)}` passed",
    ]

    recoverable_failures = [
        result
        for result in recoverable.get("results", [])
        if isinstance(result, dict) and result.get("status") != "pass"
    ]
    if recoverable_failures:
        lines.extend(["", "## Recoverable Assertion Gaps", ""])
        for result in recoverable_failures[:10]:
            lines.append(
                f"- `{result.get('id')}` [{result.get('status')}]: `{result.get('kind')}`"
            )

    tracked_types = structure.get("type_metrics", {}).get("tracked_types", {})
    if tracked_types:
        lines.extend(["", "## Recoverable Structure Types", ""])
        for elem_type in RECOVERABLE_STRUCTURE_TYPES:
            metrics = tracked_types.get(elem_type)
            if not isinstance(metrics, dict):
                continue
            lines.append(
                f"- `{elem_type}`: gold={metrics.get('gold')} "
                f"candidate={metrics.get('candidate')} "
                f"recall={metrics.get('recall')}"
            )

    missing_recoverable_titles = bookmarks.get("missing_recoverable_titles", [])
    if missing_recoverable_titles:
        lines.extend(["", "## Recoverable Bookmark Gaps", ""])
        for title in missing_recoverable_titles[:10]:
            lines.append(f"- `{title}`")

    missing_higher_order_titles = bookmarks.get("missing_higher_order_titles", [])
    if missing_higher_order_titles:
        lines.extend(["", "## Higher-Order Bookmark Gaps", ""])
        for title in missing_higher_order_titles[:10]:
            lines.append(f"- `{title}`")

    return "\n".join(lines) + "\n"
