"""Match measured words to elements and recover unowned page text."""

import dataclasses
import math
from collections import Counter
from typing import Any

import pikepdf

from app.pipeline.page_frame import PageFrame
from app.pipeline.page_glyphs import MeasuredGlyph, MeasuredTextOp, union_bbox
from app.pipeline.tagger_content_geometry import (
    _extract_form_invocations,
    _extract_text_show_runs,
    _strip_existing_markers,
    _text_runs_look_like_hidden_ocr_layer,
)
from app.pipeline.tagger_geometry import (
    IDENTITY,
    LINK_TEXT_ELEMENT_TYPES,
    PATH_PAINTING_OPERATORS,
    ContentTarget,
    _as_positive_int,
    _bbox_area,
    _bbox_intersection,
    _bbox_iou,
    _containment_ratio,
    _element_boxes,
    _expand_bbox,
    _glyph_key,
    _mat_multiply,
    _table_cell_bbox,
    _transform_point,
)
from app.pipeline.tagger_semantics import _element_actual_text


def _content_targets(elements: list[dict]) -> list[ContentTarget]:
    """Every box text can belong to: each cell of a table with cells, and
    each box of a text or artifact element (a table without cells is read
    as text), with link boxes nested inside their source owners."""
    targets: list[ContentTarget] = []
    # Preserve ownership tie breaks when the page vote reorders elements.
    for idx, elem in sorted(enumerate(elements), key=lambda pair: pair[1].get("_docling_order", pair[0])):
        elem_type = elem.get("type")
        cells = [cell for cell in elem.get("cells") or [] if isinstance(cell, dict)]
        if elem_type == "table" and cells:
            for cell in cells:
                box = _table_cell_bbox(elem, cell)
                if box is not None:
                    key = (
                        _as_positive_int(cell.get("row", 0), default=0),
                        _as_positive_int(cell.get("col", 0), default=0),
                    )
                    for annotation, link_box in cell.get("_link_targets", []):
                        targets.append(ContentTarget(
                            idx, key, link_box, cell, link_key=annotation.objgen, annotation=annotation,
                        ))
                    targets.append(ContentTarget(idx, key, box, cell))
        elif elem_type in LINK_TEXT_ELEMENT_TYPES or elem_type in {"artifact", "table"}:
            for annotation, box in elem.get("_link_targets", []):
                targets.append(ContentTarget(
                    idx, box=box, link_key=annotation.objgen, annotation=annotation,
                ))
            targets.extend(ContentTarget(idx, None, box) for box in _element_boxes(elem))
    return targets


def _upright(box: dict[str, float], turn: int) -> dict[str, float]:
    """A user-space box turned so that text running `turn` quarter turns
    counterclockwise (see MeasuredGlyph.turn) runs left to right."""
    left, bottom, right, top = box["l"], box["b"], box["r"], box["t"]
    if turn == 1:
        return {"l": bottom, "b": -right, "r": top, "t": -left}
    if turn == 2:
        return {"l": -right, "b": -top, "r": -left, "t": -bottom}
    if turn == 3:
        return {"l": -top, "b": left, "r": -bottom, "t": right}
    return box


def _owner_of(
    box: dict[str, float], targets: list[ContentTarget], turn: int = 0
) -> ContentTarget | None:
    """The target content belongs to: one whose box holds the middle of the
    content's height and at least half of its width, as element text takes
    the words its boxes hold at least half of (see pdfium_text). Glyph boxes
    run from the font's descent to its ascent, taller than the ink Docling
    draws its boxes around, so height is judged by the middle. Of several
    (nested boxes, such as line numbers inside a paragraph's box), the
    smallest. A link refines that owner's content even when its annotation
    rectangle has more padding than the owner's box. Height and width are
    the text's own, for text that runs `turn` quarter turns from left to right."""
    box = _upright(box, turn)
    middle = (box["b"] + box["t"]) / 2
    width = box["r"] - box["l"]
    best: ContentTarget | None = None
    best_area = math.inf
    links: list[tuple[float, ContentTarget]] = []
    for target in targets:
        target_box = _upright(target.box, turn)
        if not target_box["b"] <= middle <= target_box["t"]:
            continue
        if width > 0:
            overlap = min(box["r"], target_box["r"]) - max(box["l"], target_box["l"])
            share = max(overlap, 0.0) / width
        else:
            share = 1.0 if target_box["l"] <= box["l"] <= target_box["r"] else 0.0
        area = _bbox_area(target_box)
        if share < 0.5:
            continue
        if target.link_key is not None:
            links.append((area, target))
            continue
        if (
            best is not None and target.cell is not None and best.cell is not None
            and target.elem_idx == best.elem_idx
        ):
            # Cell boxes fitted to adjacent lines can overlap. The smaller
            # box may be the next row's short number; choose the line whose
            # middle is closest before using area to break a tie.
            best_box = _upright(best.box, turn)
            distance = abs((target_box["b"] + target_box["t"]) / 2 - middle)
            best_distance = abs((best_box["b"] + best_box["t"]) / 2 - middle)
            better = (distance, area) < (best_distance, best_area)
        else:
            better = area < best_area
        if better:
            best, best_area = target, area
    if best is not None:
        best_link, best_link_area = None, math.inf
        for area, target in links:
            if (target.elem_idx, target.cell_key) == (best.elem_idx, best.cell_key) and area < best_link_area:
                best_link, best_link_area = target, area
        if best_link is not None:
            return best_link
    return best


# pdfminer's word rule (LAParams.word_margin): characters farther apart than
# this share of the larger one's size are separate words.
WORD_MARGIN = 0.1


def _word_break(before: dict[str, float], after: dict[str, float]) -> bool:
    """Whether a word ends between two glyphs drawn one after the other: the
    second is on another line, or past the first by more than WORD_MARGIN of
    its size, or well back from it."""
    if not after["b"] <= (before["b"] + before["t"]) / 2 <= after["t"]:
        return True
    size = max(before["r"] - before["l"], before["t"] - before["b"])
    gap = after["l"] - before["r"]
    return gap > WORD_MARGIN * size or gap < -size


def _measured_words(
    measured: dict[int, MeasuredTextOp], stream_starts: set[int] | None = None,
) -> tuple[list[list[MeasuredGlyph]], list[tuple[MeasuredGlyph, int]]]:
    """The words content is owned by (see _word_owners): glyphs in drawing
    order, broken at white space and by _word_break, and each space glyph
    with the index of the word before it."""
    words: list[list[MeasuredGlyph]] = []
    spaces: list[tuple[MeasuredGlyph, int]] = []
    previous: MeasuredGlyph | None = None
    for idx in sorted(measured):
        if stream_starts and idx in stream_starts:
            previous = None
        for glyph in measured[idx].glyphs:
            if not glyph.text.strip():
                spaces.append((glyph, len(words) - 1))
                previous = None
                continue
            if (
                previous is None
                or previous.turn != glyph.turn
                or _word_break(
                    _upright(previous.bbox, glyph.turn), _upright(glyph.bbox, glyph.turn)
                )
            ):
                words.append([])
            words[-1].append(glyph)
            previous = glyph
    return words, spaces


def _word_owners(
    measured: dict[int, MeasuredTextOp],
    targets: list[ContentTarget],
    *,
    elements: list[dict] | None = None,
    frame: PageFrame | None = None,
    scanned: bool = False,
    diagnostic: dict[str, int] | None = None,
    stream_starts: set[int] | None = None,
    vertical_rules: list[dict] | None = None,
) -> dict[tuple, ContentTarget | None]:
    """Each glyph's owner, decided for its whole word (see _owner_of), as
    element text takes whole words: a box that stops short of a word's last
    letters, or of a "%" drawn on its own, still holds them. Words are the
    glyphs in drawing order, broken at white space and by _word_break. A
    space glyph (some PDFs draw every word space) goes with the word before
    it, or the first word if none precedes it, so the words around it stay
    apart when read. Words and boxes are judged in the direction the text
    runs, as a page drawn sideways for /Rotate runs up."""
    words, spaces = _measured_words(measured, stream_starts)
    owners: dict[tuple, ContentTarget | None] = {}
    word_owner: list[ContentTarget | None] = []
    for word in words:
        owner = _owner_of(union_bbox(glyph.bbox for glyph in word), targets, word[0].turn)
        word_owner.append(owner)
        for glyph in word:
            owners[_glyph_key(glyph)] = owner
    before = sum(owner is None for owner in word_owner)
    if elements is not None and frame is not None:
        _recover_unowned_words(words, word_owner, targets, elements, frame, scanned, vertical_rules or [])
        for word, owner in zip(words, word_owner, strict=True):
            for glyph in word:
                owners[_glyph_key(glyph)] = owner
    if scanned:
        for index in _ocr_cell_rule_words(words, word_owner):
            word_owner[index] = None
            for glyph in words[index]:
                owners[_glyph_key(glyph)] = None
    if diagnostic is not None:
        diagnostic["before"] = before
        diagnostic["after"] = sum(owner is None for owner in word_owner)
    for glyph, word_index in spaces:
        if word_owner:
            owners[_glyph_key(glyph)] = word_owner[max(word_index, 0)]
    return owners


def _ocr_cell_rule_words(
    words: list[list[MeasuredGlyph]], owners: list[ContentTarget | None],
) -> set[int]:
    """Cell-edge OCR tokens aligned within +/-2 pt on three grid rows.

    Work on whole measured words, never substrings. Require other text in
    the cell so repeated genuine single-letter/roman-numeral cells survive.
    A cluster has a bounded width (4 pt), never a chain of near neighbors.
    """
    by_cell: dict[tuple[int, tuple[int, int]], list[int]] = {}
    for index, owner in enumerate(owners):
        if owner is not None and owner.cell_key is not None:
            by_cell.setdefault((owner.elem_idx, owner.cell_key), []).append(index)
    tables: dict[int, list[tuple[float, int, int]]] = {}
    for (table, (row, _)), indices in by_cell.items():
        if not any("".join(g.text for g in words[i]) not in {"|", "l", "I", "!"} for i in indices):
            continue
        turn = words[indices[0]][0].turn
        # Drawing order can differ from reading order; position decides cell edges.
        indices.sort(key=lambda i: (
            -round(_upright(words[i][0].bbox, turn)["b"], 1),
            _upright(words[i][0].bbox, turn)["l"],
        ))
        for index in {indices[0], indices[-1]}:
            word = words[index]
            if len(word) == 1 and word[0].text in {"|", "l", "I", "!"}:
                if (word[0].text == "I" and index == indices[0] and len(indices) > 1
                        and words[indices[1]][0].text[:1].islower()):
                    # Aligned survey answers can all begin "I thought ...".
                    # A plausible pronoun is content, even at a cell edge.
                    continue
                box = _upright(word[0].bbox, turn)
                tables.setdefault(table, []).append(((box["l"] + box["r"]) / 2, row, index))
    artifacts: set[int] = set()
    for candidates in tables.values():
        candidates.sort()
        for start, (x, _, _) in enumerate(candidates):
            cluster = []
            for candidate in candidates[start:]:
                if candidate[0] - x > 4.0:
                    break
                cluster.append(candidate)
            if len({row for _, row, _ in cluster}) >= 3:
                artifacts.update(index for _, _, index in cluster)
    return artifacts


# Only unclaimed text is subject to these guards. Existing ownership wins.
UNOWNED_MARGIN_BAND = 0.08
SCAN_PICTURE_MIN_PAGE_SHARE = 0.80
NEARBY_LINE_HEIGHTS = 1.5
LIST_LABEL_MAX_LINE_HEIGHTS = 2.0


def _axis_gap(a: dict, b: dict, low: str, high: str) -> float:
    return max(a[low] - b[high], b[low] - a[high], 0.0)


def _column_overlap(a: dict, b: dict) -> float:
    width = min(a["r"] - a["l"], b["r"] - b["l"])
    return max(0.0, min(a["r"], b["r"]) - max(a["l"], b["l"])) / max(width, 1.0)


def _fallback_reading_order(box: dict, elements: list[dict], turn: int) -> float:
    """Place new text beside its nearest existing reading-order anchor.

    Preserve Docling's order between existing elements, including columns;
    within the gap after/before an anchor, increasing distance moves the new
    paragraph farther after/before it.
    """
    anchors = []
    for elem in elements:
        if elem.get("type") not in LINK_TEXT_ELEMENT_TYPES | {"table", "figure"} or elem.get(
            "fallback"
        ):
            continue
        for raw in _element_boxes(elem):
            other = _upright(raw, turn)
            gap = _axis_gap(box, other, "b", "t")
            anchors.append(
                (
                    _column_overlap(box, other) < 0.5,
                    gap,
                    _axis_gap(box, other, "l", "r"),
                    elem["reading_order"],
                    other,
                )
            )
    if not anchors:
        return -box["t"] + box["l"] / 100000
    _, gap, _, order, other = min(anchors, key=lambda item: item[:4])
    distance = abs(other["t"] - box["t"])
    offset = 0.49 * (distance + 1) / (distance + max(box["t"] - box["b"], 1) + 1)
    return order + (offset if box["t"] < other["t"] else -offset)


def _word_lines(
    indices: list[int], words: list[list[MeasuredGlyph]], boxes: list[dict]
) -> list[list[int]]:
    """Physical lines, split at column gutters wider than two line heights."""
    lines: list[list[int]] = []
    for index in sorted(
        indices,
        key=lambda i: (
            words[i][0].turn,
            -_upright(boxes[i], words[i][0].turn)["t"],
            _upright(boxes[i], words[i][0].turn)["l"],
        ),
    ):
        box = _upright(boxes[index], words[index][0].turn)
        for line in reversed(lines):
            last = line[-1]
            other = _upright(boxes[last], words[last][0].turn)
            height = max(box["t"] - box["b"], 1.0)
            if (
                words[last][0].turn == words[index][0].turn
                and abs(box["b"] - other["b"]) <= 0.5 * height
                and _axis_gap(box, other, "l", "r") <= 2 * height
            ):
                line.append(index)
                break
        else:
            lines.append([index])
    return lines


def _recover_unowned_words(
    words: list[list[MeasuredGlyph]],
    owners: list[ContentTarget | None],
    targets: list[ContentTarget],
    elements: list[dict],
    frame: PageFrame,
    scanned: bool,
    vertical_rules: list[dict],
) -> None:
    """Recover text missed by layout boxes without disturbing any exact owner.

    Table rows precede hanging list labels, then nearby text in the same
    column. Remaining body words become paragraphs, grouped by physical line.
    Figure text is excluded unless an OCR layer is under a scan-sized picture.
    All comparisons run upright, like the primary ownership rule.
    """
    if all(owner is not None for owner in owners):
        return
    boxes = [union_bbox(g.bbox for g in word) for word in words]
    original = list(owners)
    line_boxes = {}
    for line in _word_lines(list(range(len(words))), words, boxes):
        line_box = union_bbox(boxes[i] for i in line)
        for i in line:
            line_boxes[i] = line_box
    page_box = {"l": frame.left, "b": frame.bottom, "r": frame.right, "t": frame.top}
    page_turn = frame.rotation // 90
    displayed_page = _upright(page_box, page_turn)
    pending: list[int] = []
    for index, (word, raw_box) in enumerate(zip(words, boxes, strict=True)):
        if owners[index] is not None:
            continue
        turn = word[0].turn
        box = _upright(raw_box, turn)
        line_box = _upright(line_boxes[index], turn)
        height = max(box["t"] - box["b"], 1.0)
        displayed = _upright(raw_box, page_turn)
        middle = (displayed["b"] + displayed["t"]) / 2
        band = UNOWNED_MARGIN_BAND * (displayed_page["t"] - displayed_page["b"])
        if not displayed_page["b"] + band < middle < displayed_page["t"] - band:
            continue
        # Explicit pagination regions remain hidden, even when a word only
        # grazes their box and so missed the primary half-width rule.
        if any(
            elem.get("artifact_type", elem.get("type")) in {"page_header", "page_footer"}
            and any(
                _bbox_intersection(line_boxes[index], region) > 0 for region in _element_boxes(elem)
            )
            for elem in elements
        ):
            continue

        tables = [
            ContentTarget(i, box=region)
            for i, elem in enumerate(elements)
            if elem.get("type") == "table"
            for region in _element_boxes(elem)
        ]
        # Unowned single strokes near table geometry are usually OCR's
        # reading of a rule. Keep exactly owned letters and body words.
        if "".join(g.text for g in word) in {"|", "l", "I", "!"}:
            near_table = any(
                _axis_gap(box, _upright(target.box, turn), "l", "r") <= height / 2
                and _axis_gap(box, _upright(target.box, turn), "b", "t") <= height / 2
                for target in tables
            )
            near_rule = any(
                rule["t"] - rule["b"] >= 3 * max(rule["r"] - rule["l"], 1)
                and _axis_gap(box, rule, "l", "r") <= height / 2
                and _axis_gap(box, rule, "b", "t") == 0
                for raw_rule in vertical_rules for rule in [_upright(raw_rule, turn)]
            )
            if near_table or near_rule:
                continue
        table = _owner_of(raw_box, tables, turn)
        if table is not None:
            rows: dict[int, list[ContentTarget]] = {}
            for target in targets:
                if target.elem_idx == table.elem_idx and target.cell_key is not None:
                    rows.setdefault(target.cell_key[0], []).append(target)
            if rows:
                row = min(
                    rows.values(),
                    key=lambda cells: (
                        _axis_gap(box, union_bbox(_upright(c.box, turn) for c in cells), "b", "t"),
                        abs(
                            sum(
                                _upright(c.box, turn)["b"] + _upright(c.box, turn)["t"]
                                for c in cells
                            )
                            / (2 * len(cells))
                            - (box["b"] + box["t"]) / 2
                        ),
                    ),
                )
                owners[index] = min(
                    row,
                    key=lambda cell: (
                        -max(
                            0,
                            min(box["r"], _upright(cell.box, turn)["r"])
                            - max(box["l"], _upright(cell.box, turn)["l"]),
                        ),
                        _axis_gap(box, _upright(cell.box, turn), "l", "r"),
                        abs(
                            sum(_upright(cell.box, turn)[s] for s in ("l", "r"))
                            - box["l"]
                            - box["r"]
                        ),
                    ),
                )
                continue

        labels = []
        nearby = []
        for target in targets:
            elem = elements[target.elem_idx]
            if elem.get("type") not in LINK_TEXT_ELEMENT_TYPES:
                continue
            other = _upright(target.box, turn)
            if (
                elem.get("type") == "list_item"
                and 0 <= other["l"] - box["r"] <= LIST_LABEL_MAX_LINE_HEIGHTS * height
            ):
                # Use the item's actual owned words, not its multi-line box,
                # to establish that the number is on the same physical line.
                if any(
                    owner is not None
                    and owner.elem_idx == target.elem_idx
                    and words[j][0].turn == turn
                    and abs(
                        sum(_upright(boxes[j], turn)[s] for s in ("b", "t")) - box["b"] - box["t"]
                    )
                    <= height
                    for j, owner in enumerate(original)
                ):
                    labels.append((other["l"] - box["r"], target))
            # A correction's ActualText replaces all its descendants; new
            # words must get their own P rather than be hidden by it.
            if elem.get("type") == "formula" or _element_actual_text(elem):
                continue
            gap = _axis_gap(box, other, "b", "t")
            if _column_overlap(line_box, other) >= 0.5 and gap <= NEARBY_LINE_HEIGHTS * height:
                nearby.append((gap, abs(other["t"] - box["t"]), target))
        if labels:
            owners[index] = dataclasses.replace(
                min(labels, key=lambda item: item[:1])[1], list_label=True
            )
            continue

        pictures = [
            region
            for elem in elements
            if elem.get("type") in {"figure", "picture"}
            for region in _element_boxes(elem)
            if _owner_of(raw_box, [ContentTarget(0, box=region)], turn)
        ]
        scan_picture = scanned and any(
            _bbox_intersection(region, page_box)
            >= SCAN_PICTURE_MIN_PAGE_SHARE * _bbox_area(page_box)
            for region in pictures
        )
        if pictures and not scan_picture:
            continue
        if nearby and not scan_picture:
            owners[index] = min(nearby, key=lambda item: item[:2])[2]
        else:
            pending.append(index)

    # A physical line is a safe P boundary when layout supplied no text box.
    lines = _word_lines(pending, words, boxes)
    for line in lines:
        raw_box = union_bbox(boxes[i] for i in line)
        turn = words[line[0]][0].turn
        elem = {
            "type": "paragraph",
            "page": elements[0].get("page", 0) if elements else 0,
            "bbox": raw_box,
            "fallback": True,
            "reading_order": _fallback_reading_order(_upright(raw_box, turn), elements, turn),
        }
        target = ContentTarget(len(elements), box=raw_box)
        elements.append(elem)
        for index in line:
            owners[index] = target


def _page_vertical_rules(owner, initial_ctm=IDENTITY, ancestors=frozenset()) -> list[dict]:
    """Painted straight rules in page coordinates, including inside forms."""
    try:
        instructions = _strip_existing_markers(
            list(pikepdf.parse_content_stream(owner)), owner.get("/Resources")
        )
    except pikepdf.PdfError:
        return []
    ctm, stack, pending, rules = initial_ctm, [], [], []
    point = None
    for instruction in instructions:
        op, operands = str(instruction.operator), instruction.operands
        if op == "q":
            stack.append(ctm)
        elif op == "Q" and stack:
            ctm = stack.pop()
        elif op == "cm" and len(operands) == 6:
            ctm = _mat_multiply(tuple(float(v) for v in operands), ctm)
        elif op in {"m", "l"} and len(operands) == 2:
            end = _transform_point(ctm, *(float(v) for v in operands))
            if op == "l" and point is not None:
                pending.append({"l": min(point[0], end[0]), "r": max(point[0], end[0]),
                                "b": min(point[1], end[1]), "t": max(point[1], end[1])})
            point = end
        elif op == "re" and len(operands) == 4:
            x, y, width, height = (float(v) for v in operands)
            points = [_transform_point(ctm, a, b) for a, b in
                      [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]]
            for first, last in zip(points, points[1:] + points[:1], strict=True):
                pending.append({"l": min(first[0], last[0]), "r": max(first[0], last[0]),
                                "b": min(first[1], last[1]), "t": max(first[1], last[1])})
        elif op in PATH_PAINTING_OPERATORS:
            if op != "n":
                rules.extend(box for box in pending if
                             min(box["r"] - box["l"], box["t"] - box["b"]) <= 2
                             and max(box["r"] - box["l"], box["t"] - box["b"]) >= 6)
            pending, point = [], None
    for call in _extract_form_invocations(owner, instructions):
        if call.stream.objgen not in ancestors:
            rules.extend(_page_vertical_rules(
                call.stream, _mat_multiply(call.initial_ctm, initial_ctm),
                ancestors | {call.stream.objgen},
            ))
    return rules


def _page_has_hidden_text(owner, ancestors=frozenset()) -> bool:
    try:
        instructions = _strip_existing_markers(
            list(pikepdf.parse_content_stream(owner)), owner.get("/Resources")
        )
    except pikepdf.PdfError:
        return False
    if _text_runs_look_like_hidden_ocr_layer(_extract_text_show_runs(instructions)):
        return True
    return any(
        _page_has_hidden_text(call.stream, ancestors | {call.stream.objgen})
        for call in _extract_form_invocations(owner, instructions)
        if call.stream.objgen not in ancestors
    )


def _owner_of_glyphs(
    glyphs: list[MeasuredGlyph], word_owners: dict[tuple, ContentTarget | None]
) -> ContentTarget | None:
    """The owner most of these glyphs' words have."""
    counts = Counter(word_owners.get(_glyph_key(glyph)) for glyph in glyphs if glyph.text.strip())
    if not counts:
        counts = Counter(word_owners.get(_glyph_key(glyph)) for glyph in glyphs)
    return counts.most_common(1)[0][0] if counts else None


def _best_bbox_match(
    target_bbox: dict[str, float] | None,
    items: list[dict[str, Any]] | None,
    *,
    margin: float = 6.0,
) -> dict[str, Any] | None:
    """Return the best bbox-overlap match for a target region."""
    if not target_bbox or not items:
        return None

    expanded_bbox = _expand_bbox(target_bbox, margin)
    best_item: dict[str, Any] | None = None
    best_score = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        for bbox in _element_boxes(item):
            overlap = _bbox_intersection(target_bbox, bbox)
            relaxed_overlap = _bbox_intersection(expanded_bbox, bbox)
            score = max(
                _bbox_iou(target_bbox, bbox),
                _containment_ratio(target_bbox, bbox),
                _containment_ratio(bbox, target_bbox),
            )
            if overlap <= 0 and relaxed_overlap <= 0 and score < 0.5:
                continue
            if score > best_score:
                best_item = item
                best_score = score
    return best_item
