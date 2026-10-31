"""Complete table grids, infer headers and describe table and list attributes."""

import math
import re
from collections import Counter
from typing import Any

import pikepdf

from app.pipeline.page_glyphs import MeasuredGlyph, union_bbox
from app.pipeline.tagger_geometry import (
    IDENTITY,
    PATH_PAINTING_OPERATORS,
    _as_positive_int,
    _mat_multiply,
    _transform_point,
)
from app.pipeline.tagger_ownership import _content_targets, _owner_of, _upright


def _complete_table_grid_cells(
    cells: Any,
    *,
    num_rows: int,
    num_cols: int,
) -> list[dict]:
    """Return explicit table cells plus empty TD placeholders for uncovered slots."""
    if not isinstance(cells, list):
        return []
    if num_rows <= 0 or num_cols <= 0:
        return []

    normalized: list[dict] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        row = _as_positive_int(cell.get("row", 0), default=0)
        col = _as_positive_int(cell.get("col", 0), default=0)
        row_span = _as_positive_int(cell.get("row_span", 1), default=1)
        col_span = _as_positive_int(cell.get("col_span", 1), default=1)
        if row >= num_rows or col >= num_cols:
            continue
        normalized.append(dict(cell, row=row, col=col, row_span=row_span, col_span=col_span))

    rows: dict[int, list[dict]] = {}
    covered_by_rowspans: dict[int, set[int]] = {row: set() for row in range(num_rows)}
    for cell in normalized:
        row = _as_positive_int(cell.get("row", 0), default=0)
        col = _as_positive_int(cell.get("col", 0), default=0)
        if row >= num_rows or col >= num_cols:
            continue
        row_span = min(_as_positive_int(cell.get("row_span", 1), default=1), num_rows - row)
        col_span = min(_as_positive_int(cell.get("col_span", 1), default=1), num_cols - col)
        cell["row_span"] = row_span
        cell["col_span"] = col_span
        rows.setdefault(row, []).append(cell)
        for span_row in range(row + 1, row + row_span):
            covered_by_rowspans.setdefault(span_row, set()).update(range(col, col + col_span))

    completed: list[dict] = []
    for row in range(num_rows):
        occupied = set(covered_by_rowspans.get(row, set()))
        row_cells = sorted(rows.get(row, []), key=lambda item: _as_positive_int(item.get("col", 0), default=0))
        for cell in row_cells:
            col = _as_positive_int(cell.get("col", 0), default=0)
            col_span = min(_as_positive_int(cell.get("col_span", 1), default=1), num_cols - col)
            occupied.update(range(col, col + col_span))
            completed.append(cell)
        for col in range(num_cols):
            if col in occupied:
                continue
            completed.append({
                "row": row,
                "col": col,
                "row_span": 1,
                "col_span": 1,
                "text": "",
                "is_header": False,
                "_generated_empty": True,
            })
            occupied.add(col)

    return sorted(
        completed,
        key=lambda item: (
            _as_positive_int(item.get("row", 0), default=0),
            _as_positive_int(item.get("col", 0), default=0),
        ),
    )


def _clean_table_summary_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()[:500]


def _painted_table_rules(page) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Painted rules and the boundaries of filled row bands, in user space.
    Curves, unpainted paths, and Form XObjects supply no header evidence."""
    ctm, stack, segments, rectangles, rules = IDENTITY, [], [], [], []
    point = None
    for instruction in pikepdf.parse_content_stream(page):
        op, args = str(instruction.operator), instruction.operands
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            ctm = stack.pop() if stack else IDENTITY
        elif op == "cm":
            ctm = _mat_multiply(tuple(float(a) for a in args), ctm)
        elif op == "m":
            point = _transform_point(ctm, *map(float, args))
        elif op == "l":
            end = _transform_point(ctm, *map(float, args))
            if point is not None:
                segments.append((point, end))
            point = end
        elif op == "re":
            x, y, w, h = map(float, args)
            width = abs(w) * math.hypot(ctm[0], ctm[1])
            height = abs(h) * math.hypot(ctm[2], ctm[3])
            if min(width, height) <= 2:
                if abs(w) >= abs(h):
                    ends = ((x, y + h / 2), (x + w, y + h / 2))
                else:
                    ends = ((x + w / 2, y), (x + w / 2, y + h))
                rectangles.append(tuple(_transform_point(ctm, *p) for p in ends))
            # A painted cell/row band has a visible lower boundary too.
            # Keep its horizontal edges; clipping rectangles end with `n`
            # and never contribute evidence.
            rectangles.extend((
                (_transform_point(ctm, x, y), _transform_point(ctm, x + w, y)),
                (_transform_point(ctm, x, y + h), _transform_point(ctm, x + w, y + h)),
            ))
            point = None
        elif op in {"c", "v", "y", "h"}:
            point = None
        elif op in PATH_PAINTING_OPERATORS:
            if op != "n":
                rules.extend(rectangles)
                if op in {"f", "F", "f*", "B", "B*", "b", "b*"} and segments:
                    xs = [p[0] for segment in segments for p in segment]
                    ys = [p[1] for segment in segments for p in segment]
                    if 0 < max(ys) - min(ys) <= 2.0:
                        y = (max(ys) + min(ys)) / 2
                        rules.append(((min(xs), y), (max(xs), y)))
                if op in {"S", "s", "B", "B*", "b", "b*"}:
                    rules.extend(segments)
            segments, rectangles, point = [], [], None
    return rules


def _numeric_table_text(text: str) -> bool:
    value = text.strip().replace(",", "").replace("\u2212", "-")
    value = value.strip("$€£¥()% ")
    return bool(re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value))


def _infer_table_headers(elements: list[dict], glyphs: list[MeasuredGlyph], page) -> None:
    """Conservative tag-time fallbacks for the PubTables canonicalization
    cases. Any extractor header flag disables inference for that table.
    Only local copies of captured structure are changed; no model is used."""
    eligible = {
        index
        for index, table in enumerate(elements)
        if table.get("type") == "table"
        and table.get("num_rows", 0) >= 2
        and table.get("num_cols", 0) >= 2
        and not any(
            c.get("is_header") or c.get("column_header") or c.get("row_header")
            for c in table.get("cells", [])
            if isinstance(c, dict)
        )
    }
    if not eligible:
        return
    targets = [target for target in _content_targets(elements) if target.elem_idx in eligible]
    cell_glyphs: dict[tuple[int, int, int], list[MeasuredGlyph]] = {}
    for glyph in glyphs:
        if glyph.text.strip() and (owner := _owner_of(glyph.bbox, targets, glyph.turn)):
            if owner.cell_key is not None:
                cell_glyphs.setdefault((owner.elem_idx, *owner.cell_key), []).append(glyph)
    rules = None
    for index, table in enumerate(elements):
        if index not in eligible:
            continue
        cells = [c for c in table.get("cells", []) if isinstance(c, dict)]
        if not cells:
            continue
        rows: dict[int, list[dict]] = {}
        for cell in cells:
            rows.setdefault(cell.get("row", 0), []).append(cell)
        first = sorted(rows.get(0, []), key=lambda c: c.get("col", 0))
        # A complete, unspanned first row avoids guessing from extraction holes.
        regular_first = (
            len(first) == table["num_cols"]
            and [c.get("col", 0) for c in first] == list(range(table["num_cols"]))
            and all(c.get("row_span", 1) == 1 and c.get("col_span", 1) == 1 for c in first)
        )

        def text(cell):
            return str(cell.get("text") or "").strip()

        def mark(cell, scope):
            cell.update(is_header=True, column_header=scope == "Column", row_header=scope == "Row")

        # Terms followed by explanatory prose are a list/definition layout,
        # even when its first entry is bold and underlined.
        paired = [cs for cs in rows.values() if len(cs) == 2]
        if table["num_cols"] == 2 and paired and sum(
            len(text(sorted(cs, key=lambda c: c.get("col", 0))[0]).split()) <= 6
            and len(text(sorted(cs, key=lambda c: c.get("col", 0))[1]).split()) >= 8
            for cs in paired
        ) >= len(paired) / 2:
            continue

        if table["num_cols"] == 2 and len(paired) >= 3:
            pairs = [sorted(cs, key=lambda c: c.get("col", 0)) for cs in paired]
            numbers = [text(cs[1]) for cs in pairs]
            if (all(re.fullmatch(r"[1-9]\d*", n) for n in numbers)
                    and all(int(a) < int(b) for a, b in zip(numbers, numbers[1:]))
                    and sum(len(text(cs[0]).split()) >= 3 for cs in pairs) >= len(pairs) / 2):
                # Sentence-like entries beside increasing integers can be a
                # contents/index list. Numeric values alone are not enough.
                continue

        top_nonblank = [c for c in first if text(c)]
        blank_corner = (
            regular_first
            and not text(first[0])
            and len(top_nonblank) == len(first) - 1
            and any(text(c) and c.get("col", 0) == 0 for c in rows.get(1, []))
        )
        # Section labels are not the style reference for a real header row.
        body = [c for r, cs in rows.items() if r > 0
                and sum(bool(text(c)) for c in cs) >= 2 for c in cs if text(c)]

        def style(cell):
            drawn = cell_glyphs.get((index, cell.get("row", 0), cell.get("col", 0)), [])
            styles = Counter(
                (re.sub(r"^[A-Z]{6}\+", "", g.font_name), round(g.font_size, 1))
                for g in drawn
                if g.font_name and g.font_size > 0
            )
            if not styles:
                return None
            dominant, count = styles.most_common(1)[0]
            return dominant if count / len(drawn) >= 0.8 else None

        style_difference = False
        body_styles = [s for c in body if (s := style(c)) is not None]
        top_styles = [style(c) for c in top_nonblank]
        if (regular_first and len(body_styles) >= 2
                and (len(top_styles) >= 2 or (blank_corner and len(top_styles) == 1))):
            body_style, count = Counter(body_styles).most_common(1)[0]
            if count / len(body) >= 0.8 and all(top_styles):
                font, size = body_style
                style_difference = all(
                    s[0] != font or abs(s[1] - size) >= max(1.0, size * 0.1) for s in top_styles
                )

        # The rule must sit in the actual text gap after row zero, no more
        # than 12 pt below it, and cover >=80% of the first two rows' width.
        rule_below = False
        top_drawn = [g for c in first for g in cell_glyphs.get((index, 0, c.get("col", 0)), [])]
        next_drawn = [
            g for c in rows.get(1, []) for g in cell_glyphs.get((index, 1, c.get("col", 0)), [])
        ]
        if (
            regular_first
            and (len(top_nonblank) >= 2 or blank_corner)
            and top_drawn
            and next_drawn
        ):
            turn = top_drawn[0].turn
            top_box = union_bbox(_upright(g.bbox, turn) for g in top_drawn)
            next_box = union_bbox(_upright(g.bbox, turn) for g in next_drawn)
            width_box = union_bbox([top_box, next_box])
            if rules is None:
                rules = _painted_table_rules(page)
            intervals = []
            for start, end in rules:
                line = _upright(
                    {"l": min(start[0], end[0]), "r": max(start[0], end[0]),
                     "b": min(start[1], end[1]), "t": max(start[1], end[1])}, turn,
                )
                y = (line["b"] + line["t"]) / 2
                if (line["t"] - line["b"] <= 1.0
                        and next_box["t"] < y < top_box["b"]
                        and top_box["b"] - y <= 12.0):
                    intervals.append((y, line["l"], line["r"]))
            for y, left, right in sorted(intervals):
                # Count union coverage at this height, not a sum that could
                # count overlapping/duplicate paint more than once.
                spans = sorted((left, right) for yy, left, right in intervals if abs(yy - y) <= 1.0)
                merged = []
                for left, right in spans:
                    if merged and left <= merged[-1][1] + 2.0:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], right))
                    else:
                        merged.append((left, right))
                overlap = sum(max(0, min(right, width_box["r"]) - max(left, width_box["l"]))
                              for left, right in merged)
                if overlap >= 0.8 * (width_box["r"] - width_box["l"]):
                    rule_below = True
                    break
        numeric_body = bool(body) and sum(_numeric_table_text(text(c)) for c in body) / len(body) >= 0.75
        numeric_contrast = (
            bool(top_nonblank) and all(not _numeric_table_text(text(c)) for c in top_nonblank)
            and numeric_body
        )
        column_header = (
            regular_first and len(top_nonblank) >= max(1, table["num_cols"] - 1)
            and all(not _numeric_table_text(text(c)) for c in top_nonblank)
            and sum((blank_corner, style_difference, rule_below, numeric_contrast)) >= 2
        )
        if column_header:
            for cell in first:
                mark(cell, "Column")

        data_rows = [r for r in sorted(rows) if not (column_header and r == 0)]
        labels = [c for r in data_rows for c in rows[r] if c.get("col", 0) == 0]
        good_labels = [c for c in labels if text(c) and not _numeric_table_text(text(c))
                       and any(ch.isalpha() for ch in text(c))
                       and len(text(c).split()) <= 6 and len(text(c)) <= 60
                       and c.get("row_span", 1) == 1
                       and c.get("col_span", 1) in {1, table["num_cols"]}]
        consistent_labels = (
            len(data_rows) >= 2 and len(good_labels) >= 0.8 * len(data_rows)
            and len({text(c).casefold() for c in good_labels}) >= 0.8 * len(good_labels)
        )
        values = [c for r in data_rows for c in rows[r] if c.get("col", 0) > 0 and text(c)]
        numeric_values = (len(values) >= 2
                          and sum(_numeric_table_text(text(c)) for c in values) / len(values) >= 0.75)
        value_styles = [style(c) for c in values]
        label_styles = [style(c) for c in good_labels]
        label_difference = False
        if value_styles and all(value_styles) and label_styles and all(label_styles):
            value_style, count = Counter(value_styles).most_common(1)[0]
            label_difference = (count / len(values) >= 0.8
                                and sum(s != value_style for s in label_styles) >= 0.8 * len(label_styles))
        sections = [c for c in good_labels
                    if sum(bool(text(other)) for other in rows[c.get("row", 0)]) == 1
                    and sum(bool(text(other)) for other in rows.get(c.get("row", 0) + 1, [])) >= 2]
        # Sparse section labels never create row headers. A recognized column
        # header takes priority over projected sections in the same table.
        if (not column_header and consistent_labels and len(sections) >= 2
                and len(sections) >= 0.5 * len(data_rows)):
            for cell in sections:
                if numeric_values or (label_difference and style(cell)):
                    mark(cell, "Row")
                    cell["_projected_row_header"] = True
        elif consistent_labels and (numeric_values or label_difference):
            # Only ordinary, populated rows: a section with empty value cells
            # is not a row header merely because its neighbors contain numbers.
            for cell in good_labels:
                if any(text(v) for v in rows[cell.get("row", 0)] if v.get("col", 0) > 0):
                    mark(cell, "Row")


def _table_summary_text(table_data: dict, *, num_rows: int, num_cols: int) -> str:
    """Build a factual table summary from explicit extraction metadata."""
    candidates = [
        table_data.get("table_summary"),
        table_data.get("summary"),
    ]
    if bool(table_data.get("table_llm_confirmed")):
        candidates.append(table_data.get("table_llm_summary"))
    candidates.append(table_data.get("caption"))

    content_summary = ""
    for candidate in candidates:
        content_summary = _clean_table_summary_text(candidate)
        if content_summary:
            break

    dimensions_summary = ""
    if num_rows > 0 and num_cols > 0:
        row_label = "row" if num_rows == 1 else "rows"
        col_label = "column" if num_cols == 1 else "columns"
        dimensions_summary = f"Table with {num_rows} {row_label} and {num_cols} {col_label}."

    if content_summary and dimensions_summary:
        if content_summary.lower().startswith("table with "):
            return content_summary
        return f"{dimensions_summary} {content_summary}"
    return content_summary or dimensions_summary


def _list_numbering_name(marker: Any, *, enumerated: bool = False) -> str | None:
    text = str(marker or "").strip()
    token = text.strip("()[]{}.:")

    if enumerated or re.match(r"^\d+", token):
        return "Decimal"

    roman_re = r"^(?=[ivxlcdm]+$)[ivxlcdm]+$"
    if re.match(roman_re, token, flags=re.IGNORECASE):
        return "UpperRoman" if token.isupper() else "LowerRoman"

    if len(token) == 1 and token.isalpha():
        return "UpperAlpha" if token.isupper() else "LowerAlpha"

    if text in {"-", "*", "+"} or (len(text) == 1 and ord(text) in {0x2022, 0x25CF}):
        return "Disc"
    if len(text) == 1 and ord(text) in {0x25E6, 0x25CB}:
        return "Circle"
    if len(text) == 1 and ord(text) in {0x25AA, 0x25A0}:
        return "Square"

    return None


def _set_list_numbering_attr(
    list_elem: pikepdf.Object,
    marker: Any,
    *,
    enumerated: bool = False,
) -> None:
    numbering = _list_numbering_name(marker, enumerated=enumerated)
    if not numbering:
        return

    attrs = list_elem.get("/A")
    if not isinstance(attrs, pikepdf.Dictionary):
        attrs = pikepdf.Dictionary({"/O": pikepdf.Name("/List")})
        list_elem["/A"] = attrs
    elif attrs.get("/O") is None:
        attrs["/O"] = pikepdf.Name("/List")

    if attrs.get("/ListNumbering") is None:
        attrs["/ListNumbering"] = pikepdf.Name(f"/{numbering}")
