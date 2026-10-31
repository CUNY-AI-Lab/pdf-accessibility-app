"""Recognize only unambiguous headerless lists and independent prose blocks.

Length, holes and missing headers are not sufficient evidence: definition
tables, rubrics and continuation records can have all three.
"""

import re
from collections import defaultdict

BULLET = r"[•●◦▪■♦◆\-*+]"
MARKER = re.compile(rf"^\s*({BULLET}|\d+[.)]|\d+(?:\.\d+)+\.?|[A-Za-z][.)])(?=\s|[A-Za-z])")
LEADER = re.compile(r"(?:\.\s*){3,}\s*(?:\d+|[ivxlcdm]+)\s*$", re.I)
CONTENTS = re.compile(r"^(?:table of contents|contents|list of (?:tables|figures)|inhalt|inhaltsverzeichnis|indhold)$", re.I)


def _layout_table_plan(table: dict, page_elements: list[dict]) -> dict | None:
    """Return a structure-only plan; uncertain grids remain data tables.

    All boxes are already in PDF user space (bottom-up). Row order is the
    default. Independent prose columns require a positive gutter and staggered
    row tops crossing another column's row boundary. Aligned pairs such as
    rubrics/definitions are never unzipped.
    """
    cells = [c for c in table.get("cells", []) if isinstance(c, dict)]
    if any(c.get(f) for c in cells for f in ("is_header", "column_header", "row_header")):
        return None
    populated = [c for c in cells if str(c.get("text") or "").strip()]
    if len(populated) < 3 or any(not c.get("bbox") for c in populated):
        return None
    rows: dict[int, list[dict]] = defaultdict(list)
    for cell in populated:
        rows[cell.get("row", 0)].append(cell)
    ordered = [sorted(rows[r], key=lambda c: c.get("col", 0)) for r in sorted(rows)]

    def text(cell):
        return " ".join(str(cell.get("text") or "").split())

    def key(cell):
        return (cell.get("row", 0), cell.get("col", 0))

    def block(role, groups):
        return {"role": role, "groups": [[key(c) for c in group] for group in groups]}

    def plan(reason, blocks, order="row"):
        return {"reason": reason, "order": order, "blocks": blocks}

    # Printed leaders ending in destinations are much stronger evidence than
    # an arbitrary increasing numeric column (which may contain real values).
    if len(ordered) >= 3 and sum(
        bool(LEADER.search(" ".join(text(c) for c in row))) for row in ordered
    ) >= 0.8 * len(ordered):
        return plan("contents leaders and destinations", [block("L", ordered)])

    box = table.get("bbox", {})
    nearby_contents = any(
        e.get("type") in {"heading", "paragraph", "toc_caption"}
        and CONTENTS.fullmatch(" ".join(str(e.get("text") or "").split()))
        and (b := e.get("bbox")) and box
        and 0 <= b["b"] - box["t"] <= 72
        and b["l"] < box["r"] and b["r"] > box["l"]
        for e in page_elements
    )
    destinations = [text(row[-1]) for row in ordered if len(row) == 2]
    numbers = [int(n) for value in destinations for n in value.split() if n.isdigit()]
    if (nearby_contents and len(destinations) >= 0.8 * len(ordered)
            and all(re.fullmatch(r"\d+(?:\s+\d+)*", n) for n in destinations)
            and numbers == sorted(numbers)
            and all(any(ch.isalpha() for ch in text(row[0])) for row in ordered)):
        return plan("contents heading and page destinations", [block("L", ordered)])

    # A separate column of repeated bullet glyphs is a label column, not
    # comparable data. Permit a single full-width title above the list.
    body = ordered
    title = []
    if (len(body[0]) == 1 and body[0][0].get("col_span", 1) == table.get("num_cols")
            and table.get("num_cols", 0) > 1):
        title, body = body[:1], body[1:]
    if (len(body) >= 3 and all(len(row) == 2 and re.fullmatch(BULLET, text(row[0]))
                              and len(text(row[1]).split()) >= 3 for row in body)):
        return plan("bullet label column", [block("P", title), block("L", body)])
    if (len(body) >= 3 and all(len(row) == 1 and MARKER.match(text(row[0])) for row in body)):
        return plan("single column of marked items", [block("P", title), block("L", body)])

    # Paired prompts/replies and process grids can be author-tagged data
    # tables. Markers in multiple columns do not prove a list: retain them.

    # Independent flowing prose: every cell is substantial, no short values,
    # no spans, a clear gutter, predominantly staggered tops, and overlapping
    # text boxes across nominal rows. Centered cells in a real data row can
    # have different tops; only incompatible row boundaries justify unzipping.
    columns: dict[int, list[dict]] = defaultdict(list)
    for cell in populated:
        columns[cell.get("col", 0)].append(cell)
    if (len(columns) >= 2 and all(len(cs) >= 2 for cs in columns.values())
            and all(len(text(c).split()) >= 20 and c.get("col_span", 1) == 1
                    and c.get("row_span", 1) == 1 for c in populated)):
        cols = [columns[col] for col in sorted(columns)]
        gutter = all(max(c["bbox"]["r"] for c in a) + 12 <= min(c["bbox"]["l"] for c in b)
                     for a, b in zip(cols, cols[1:]))
        staggered = sum(max(c["bbox"]["t"] for c in row) - min(c["bbox"]["t"] for c in row) > 12
                        for row in ordered if len(row) > 1)
        crossings = sum(
            min(a["bbox"]["t"], b["bbox"]["t"]) - max(a["bbox"]["b"], b["bbox"]["b"]) > 12
            for i, col in enumerate(cols) for other in cols[i + 1:]
            for a in col for b in other if a.get("row", 0) != b.get("row", 0)
        )
        if (gutter and staggered >= max(2, 0.75 * len(ordered))
                and crossings >= len(ordered) - 1):
            groups = [[c] for col in cols for c in sorted(col, key=lambda c: -c["bbox"]["t"])]
            return plan("gutter-separated staggered prose columns", [block("P", groups)], "column")
    return None
