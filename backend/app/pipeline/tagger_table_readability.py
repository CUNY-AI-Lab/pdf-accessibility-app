"""Reject semantic repairs when measured table text is not reliably ordered."""

import re
from collections import Counter, defaultdict
from dataclasses import replace

from app.pipeline.page_glyphs import MeasuredGlyph, join_glyphs, union_bbox
from app.pipeline.tagger_ownership import _content_targets, _owner_of, _upright

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


def _grid_order(table: dict, boxes: dict[tuple[int, int], dict[str, float]]) -> tuple[int, int]:
    """Pairs of neighboring grid cells whose drawn text lies apart along the
    grid's axis, and how many of those lie in the opposite order: a cell of
    the next row above one in the row before, or a cell of the next column
    left of one in the column before, where their rows meet. Boxes are in
    the text's own frame, so a grid read in another frame (rows that run
    across the lines of text) inverts about half its pairs, and a grid read
    in this one inverts none."""
    by_row, by_col = defaultdict(list), defaultdict(list)
    for cell in table.get("cells", []):
        row, col = cell.get("row", 0), cell.get("col", 0)
        if (box := boxes.get((row, col))) is not None:
            last_row = row + max(1, int(cell.get("row_span") or 1)) - 1
            last_col = col + max(1, int(cell.get("col_span") or 1)) - 1
            by_row[row].append((row, last_row, col, last_col, box))
            by_col[col].append((row, last_row, col, last_col, box))
    compared = inverted = 0
    for row, last_row, _, last_col, box in (span for spans in by_row.values() for span in spans):
        for *_, other in by_row.get(last_row + 1, []):
            if other["t"] < box["b"] or box["t"] < other["b"]:
                compared += 1
                inverted += box["t"] < other["b"]
        for other_row, other_last_row, *_, other in by_col.get(last_col + 1, []):
            if (
                other_row <= last_row
                and row <= other_last_row
                and (other["l"] > box["r"] or box["l"] > other["r"])
            ):
                compared += 1
                inverted += box["l"] > other["r"]
    return compared, inverted


def _mark_unreadable_tables(
    elements: list[dict], glyphs: list[MeasuredGlyph], display_turn: int = 0
) -> list[dict]:
    """Run before layout/header decisions; keep ownership and extractor flags intact.

    A table is read in its text's frame: the direction (MeasuredGlyph.turn)
    a strict majority of the non-space glyphs its cells own run in, Forms
    included, turned to run left to right. `display_turn` is the page's
    /Rotate in quarter turns, the direction of text that reads upright as
    shown. A table with no majority direction, most of it not upright as
    shown, cannot be read in one frame and is held.

    Order evidence compares both captured cell text and what its glyphs draw
    against geometry; a header cannot repair either kind of contradiction.
    Disorder must affect at least half the table's matched words, with at
    least 40% of unique-word pairs inverted in each disordered cell. Short
    and orderly cells still contribute to the table-wide word denominator.

    The grid must run with the text: it is held when at least a quarter of
    at least 8 neighboring-cell pairs are inverted in the text's frame (see
    _grid_order), as when an extractor read a table printed sideways in the
    page's frame instead of the text's.
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
        rotated = sum(g.turn != display_turn for g in drawn)
        turn, count = Counter(g.turn for g in drawn).most_common(1)[0] if drawn else (0, 0)
        if count * 2 <= len(drawn):
            turn = display_turn
        mixed = turn == display_turn and rotated * 2 > len(drawn)
        # Each cell's glyphs in the text's frame, when all run that way.
        turned = {
            key: [replace(g, bbox=_upright(g.bbox, turn)) for g in gs]
            for (i, key), gs in cells.items()
            if i == index and gs and all(g.turn == turn for g in gs)
        }
        disorder = []
        matched_words = 0
        disordered_words = 0
        if drawn and not mixed:
            for cell in table.get("cells", []):
                key = (cell.get("row", 0), cell.get("col", 0))
                if not (gs := turned.get(key)):
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
        compared = inverted = 0
        if drawn and not mixed:
            compared, inverted = _grid_order(
                table, {key: union_bbox(g.bbox for g in gs) for key, gs in turned.items()}
            )
        across = compared >= 8 and inverted * 4 >= compared
        if mixed or across or (disorder and disordered_words >= matched_words / 2):
            diagnostic = {
                "table_review_id": table.get(
                    "review_id", f"review-{table.get('_docling_order', index)}"
                ),
                "page": table.get("page", 0) + 1,
                "reason": TABLE_READABILITY_REASON,
                "cause": (
                    "mixed_text_direction"
                    if mixed
                    else "grid_across_text"
                    if across
                    else "cell_text_order"
                ),
                "glyphs": len(drawn),
                "rotated_glyphs": rotated,
                "text_turn": turn,
                "grid_pairs": compared,
                "inverted_grid_pairs": inverted,
                "matched_words": matched_words,
                "disordered_words": disordered_words,
                "disordered_cells": disorder,
            }
            table["_table_readability"] = diagnostic
            diagnostics.append(diagnostic)
    return diagnostics
