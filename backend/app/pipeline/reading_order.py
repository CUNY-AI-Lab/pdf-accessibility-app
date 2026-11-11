"""Conservative page order voting, independent of text and model output.

XY-cut uses displayed, bottom-left-origin boxes. It recursively cuts at
projection whitespace, preferring wide vertical gutters. Wide elements are
masked to find columns and reinserted as horizontal bands (the inexpensive
spanning-element refinement of XY-Cut++); this is not the full XY-Cut++ model.
"""

import math


def _fill_slots(original: list[int], ordered: list[int]) -> list[int]:
    """Elements without evidence keep their Docling slots and relative order."""
    movable = set(ordered)
    replacement = iter(ordered)
    return [next(replacement) if index in movable else index for index in original]


def _gaps(indices: list[int], boxes: dict[int, dict], low: str, high: str) -> list[tuple[float, float]]:
    intervals = sorted((boxes[i][low], boxes[i][high]) for i in indices)
    gaps = []
    end = intervals[0][1]
    for start, stop in intervals[1:]:
        if start > end:
            gaps.append((end, start))
        end = max(end, stop)
    return gaps


def xy_cut_order(boxes: list[dict | None]) -> list[int]:
    """Recursive XY-cut. Missing/degenerate boxes keep their original slots.

    A gutter must be >= 6 points and >= 2% of the region width. Mask boxes
    >= 70% of that width only when the remaining boxes support columns with
    overlapping vertical extents. Spanners divide the columns into bands at
    their vertical centres, so a mid-page heading separates two column runs.
    Overlapping leaves use top-to-bottom, left-to-right order, then source
    index. Cap recursion at 64 for pathological layouts.
    """
    valid = {
        i: box for i, box in enumerate(boxes)
        if isinstance(box, dict)
        and all(isinstance(box.get(k), (int, float)) and math.isfinite(box[k]) for k in "lbrt")
        and box["r"] > box["l"] and box["t"] > box["b"]
    }

    def cut(indices: list[int], depth: int = 0) -> list[int]:
        if len(indices) < 2:
            return indices
        by_position = sorted(indices, key=lambda i: (-valid[i]["t"], valid[i]["l"], i))
        if depth >= 64:
            return by_position
        width = max(valid[i]["r"] for i in indices) - min(valid[i]["l"] for i in indices)
        narrow = [i for i in indices if valid[i]["r"] - valid[i]["l"] < 0.7 * width]
        # First try the unmasked projection, then look behind spanning boxes.
        for candidates in (indices, narrow):
            if len(candidates) < 2:
                continue
            gutters = sorted(_gaps(candidates, valid, "l", "r"), key=lambda g: (-(g[1] - g[0]), g[0]))
            for left, right in gutters:
                if right - left < max(6.0, 0.02 * width):
                    continue
                first = [i for i in indices if valid[i]["r"] <= left]
                second = [i for i in indices if valid[i]["l"] >= right]
                if not first or not second:
                    continue
                if min(max(valid[i]["t"] for i in first), max(valid[i]["t"] for i in second)) <= max(
                    min(valid[i]["b"] for i in first), min(valid[i]["b"] for i in second)
                ):
                    continue
                spanners = [i for i in by_position if i not in first and i not in second]
                if spanners:
                    remaining = first + second
                    ordered = []
                    for span in spanners:
                        middle = (valid[span]["b"] + valid[span]["t"]) / 2
                        above = [i for i in remaining if (valid[i]["b"] + valid[i]["t"]) / 2 > middle]
                        ordered.extend(cut(above, depth + 1))
                        ordered.append(span)
                        above_set = set(above)
                        remaining = [i for i in remaining if i not in above_set]
                    return ordered + cut(remaining, depth + 1)
                return cut(first, depth + 1) + cut(second, depth + 1)
        gaps = _gaps(indices, valid, "b", "t")
        if gaps:
            bottom, top = max(gaps, key=lambda g: (g[1] - g[0], g[1]))
            above = [i for i in indices if valid[i]["b"] >= top]
            below = [i for i in indices if valid[i]["t"] <= bottom]
            return cut(above, depth + 1) + cut(below, depth + 1)
        return by_position

    return _fill_slots(list(range(len(boxes))), cut(list(valid)))


def kendall_tau(first: list[int], second: list[int]) -> float:
    """Tau-a over shared elements (orders have no ties), O(n log n)."""
    ranks = {index: rank for rank, index in enumerate(second)}
    shared = [ranks[index] for index in first if index in ranks]
    if len(shared) < 2:
        return 1.0
    tree = [0] * (len(second) + 1)
    inversions = 0
    for seen, rank in enumerate(shared):
        position, prefix = rank + 1, 0
        while position:
            prefix += tree[position]
            position -= position & -position
        inversions += seen - prefix
        position = rank + 1
        while position < len(tree):
            tree[position] += 1
            position += position & -position
    return 1.0 - 4.0 * inversions / (len(shared) * (len(shared) - 1))


def choose_order(
    boxes: list[dict | None], first_operators: dict[int, tuple[int, int]]
) -> tuple[list[int], dict]:
    """Only replace Docling when drawing and layout independently agree.

    Up to 5 elements, agreement means exact equality. Otherwise tau >= .9
    agrees, tau <= .8 disagrees; the intervening grey zone keeps Docling.
    Require >= 2 elements with both drawn content and valid geometry, and
    apply the same vote to that evidence subset, so fixed slots cannot
    manufacture corroboration on a sparsely drawn page.
    """
    docling = list(range(len(boxes)))
    drawing = _fill_slots(docling, sorted(first_operators, key=lambda i: (first_operators[i], i)))
    layout = xy_cut_order(boxes)

    def vote(orders: list[list[int]]) -> tuple[bool, dict]:
        d, p, x = orders
        agreement = {
            "docling_drawing": kendall_tau(d, p),
            "docling_xy_cut": kendall_tau(d, x),
            "drawing_xy_cut": kendall_tau(p, x),
        }
        if len(d) <= 5:
            use_drawing = p == x and d != p and d != x
        else:
            use_drawing = (
                agreement["drawing_xy_cut"] >= 0.9
                and agreement["docling_drawing"] <= 0.8
                and agreement["docling_xy_cut"] <= 0.8
            )
        return use_drawing, agreement

    orders = [docling, drawing, layout]
    use_drawing, agreement = vote(orders)
    evidence = {
        i for i in first_operators
        if boxes[i] is not None and all(
            isinstance(boxes[i].get(k), (int, float)) and math.isfinite(boxes[i][k]) for k in "lbrt"
        ) and boxes[i]["r"] > boxes[i]["l"] and boxes[i]["t"] > boxes[i]["b"]
    }
    evidence_vote, evidence_agreement = vote([[i for i in order if i in evidence] for order in orders])
    use_drawing = use_drawing and len(evidence) >= 2 and evidence_vote
    return (drawing if use_drawing else docling), {
        "order_used": "drawing" if use_drawing else "docling",
        "element_count": len(boxes),
        "drawn_element_count": len(first_operators),
        "evidence_element_count": len(evidence),
        "agreement": agreement,
        "evidence_agreement": evidence_agreement,
    }
