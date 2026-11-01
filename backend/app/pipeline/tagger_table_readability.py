"""Reject semantic repairs when measured table text is not reliably ordered."""

import re
from collections import Counter, defaultdict

from app.pipeline.page_glyphs import MeasuredGlyph, join_glyphs
from app.pipeline.tagger_ownership import _content_targets, _owner_of

TABLE_READABILITY_REASON = "This table is rotated or its text could not be read in order"


def _geometric_text(glyphs: list[MeasuredGlyph]) -> str:
    """Upright Latin text, grouped by baseline before sorting left to right."""
    lines: list[list[MeasuredGlyph]] = []
    for glyph in sorted(glyphs, key=lambda g: (-g.bbox["b"], g.bbox["l"])):
        height = max(1.0, glyph.bbox["t"] - glyph.bbox["b"])
        if not lines or abs(lines[-1][0].bbox["b"] - glyph.bbox["b"]) > height * 0.5:
            lines.append([])
        lines[-1].append(glyph)
    for line in lines:
        line.sort(key=lambda g: g.bbox["l"])
    return " ".join(join_glyphs(line) for line in lines)


def _order_disagreement(text: str, geometry: str) -> tuple[int, dict | None]:
    # Require the same words, so OCR substitutions, equations, sparse labels,
    # repeated words and punctuation alone cannot supply evidence of disorder.
    source, drawn = [re.findall(r"[a-z]+", t.casefold()) for t in (text, geometry)]
    a, b = Counter(source), Counter(drawn)
    common = a & b
    matched = sum(common.values())
    if not source or not drawn or matched < 0.8 * max(len(source), len(drawn)):
        return matched, None
    unique = {word for word in common if a[word] == b[word] == 1}
    if len(unique) < 8:
        return matched, None
    positions = {word: i for i, word in enumerate(drawn) if word in unique}
    order = [positions[word] for word in source if word in unique]
    pairs = len(order) * (len(order) - 1) // 2
    inversions = sum(x > y for i, x in enumerate(order) for y in order[i + 1 :])
    if inversions / pairs < 0.4:
        return matched, None
    return matched, {
        "matched_words": matched,
        "matched_unique_words": len(unique),
        "inversion_fraction": round(inversions / pairs, 4),
    }


def _mark_unreadable_tables(elements: list[dict], glyphs: list[MeasuredGlyph]) -> list[dict]:
    """Run before layout/header decisions; keep ownership and extractor flags intact.

    Rotation counts non-space glyphs owned by table cells, including Forms.
    Order evidence compares both captured cell text and what its glyphs draw
    against geometry; a header cannot repair either kind of contradiction.
    Disorder must affect at least half the table's matched words, with at
    least 40% of unique-word pairs inverted in each disordered cell. Short
    and orderly cells still contribute to the table-wide word denominator.
    Absence of sufficient evidence is not a readability failure.
    """
    targets = _content_targets(elements)
    cells = defaultdict(list)
    for glyph in glyphs:
        if glyph.text.strip() and (owner := _owner_of(glyph.bbox, targets, glyph.turn)):
            if elements[owner.elem_idx].get("type") == "table":
                cells[owner.elem_idx, owner.cell_key].append(glyph)
    diagnostics = []
    for index, table in enumerate(elements):
        if table.get("type") != "table":
            continue
        table.pop("_table_readability", None)
        drawn = [g for (i, _), gs in cells.items() if i == index for g in gs]
        rotated = sum(bool(g.turn) for g in drawn)
        disorder = []
        matched_words = 0
        disordered_words = 0
        if drawn and rotated <= len(drawn) / 2:
            for cell in table.get("cells", []):
                key = (cell.get("row", 0), cell.get("col", 0))
                gs = cells.get((index, key), [])
                if not gs or any(g.turn for g in gs):
                    continue
                geometry = _geometric_text(gs)
                cell_matched = 0
                cell_evidence = None
                for text in (
                    str(cell.get("actual_text") or cell.get("text") or ""),
                    join_glyphs(gs),
                ):
                    matched, evidence = _order_disagreement(text, geometry)
                    cell_matched = max(cell_matched, matched)
                    if evidence is not None:
                        cell_evidence = evidence
                matched_words += cell_matched
                if cell_evidence is not None:
                    disorder.append({"row": key[0], "col": key[1], **cell_evidence})
                    disordered_words += cell_evidence["matched_words"]
        if rotated > len(drawn) / 2 or (disorder and disordered_words >= matched_words / 2):
            diagnostic = {
                "table_review_id": table.get(
                    "review_id", f"review-{table.get('_docling_order', index)}"
                ),
                "page": table.get("page", 0) + 1,
                "reason": TABLE_READABILITY_REASON,
                "cause": "rotated_glyphs" if rotated > len(drawn) / 2 else "cell_text_order",
                "glyphs": len(drawn),
                "rotated_glyphs": rotated,
                "matched_words": matched_words,
                "disordered_words": disordered_words,
                "disordered_cells": disorder,
            }
            table["_table_readability"] = diagnostic
            diagnostics.append(diagnostic)
    return diagnostics
