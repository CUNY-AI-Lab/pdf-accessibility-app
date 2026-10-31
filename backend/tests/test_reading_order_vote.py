"""Conservative votes and layout cases that should not follow drawing order."""

import pytest

from app.pipeline.reading_order import choose_order, kendall_tau, xy_cut_order


def _box(left, bottom, right, top):
    return {"l": left, "b": bottom, "r": right, "t": top}


def test_xy_cut_prefers_wide_gutter_over_row_whitespace():
    boxes = [_box(x, y, x + 200, y + 20) for y in (650, 450) for x in (50, 350)]
    assert xy_cut_order(boxes) == [0, 2, 1, 3]


def test_spanning_heading_separates_two_column_runs():
    # The heading blocks a page-wide gutter, and touches the column boxes
    # above and below: masking it is necessary to recover either run.
    boxes = [
        _box(50, 660, 250, 700), _box(350, 660, 550, 700),
        _box(50, 620, 550, 660),
        _box(50, 580, 250, 620), _box(350, 580, 550, 620),
        _box(50, 520, 250, 560), _box(350, 520, 550, 560),
    ]
    assert xy_cut_order(boxes) == [0, 1, 2, 3, 5, 4, 6]


def test_missing_geometry_and_undrawn_elements_keep_docling_slots():
    boxes = [_box(50, 600, 250, 620), None, _box(350, 600, 550, 620),
             _box(50, 500, 250, 520), _box(350, 500, 550, 520)]
    chosen, diagnostic = choose_order(boxes, {0: (0, 0), 3: (1, 0), 2: (2, 0), 4: (3, 0)})
    assert chosen == [0, 1, 3, 2, 4]
    assert diagnostic["order_used"] == "drawing"
    assert diagnostic["evidence_element_count"] == 4


def test_no_drawing_evidence_keeps_docling():
    boxes = [_box(x, y, x + 200, y + 20) for y in (650, 450) for x in (50, 350)]
    chosen, diagnostic = choose_order(boxes, {})
    assert chosen == list(range(4))
    assert diagnostic["order_used"] == "docling"


@pytest.mark.parametrize("permutation", [[0, 2, 1, 3, 4, 5, 6, 7], [1, 0, 3, 2, 4, 5, 6, 7]])
def test_small_or_grey_zone_docling_disagreement_keeps_docling(permutation):
    positions = [_box(50, 700 - i * 30, 250, 720 - i * 30) for i in range(8)]
    boxes = [positions[i] for i in permutation]
    chosen, diagnostic = choose_order(boxes, {i: (position, 0) for i, position in enumerate(permutation)})
    assert chosen == list(range(8))
    assert diagnostic["agreement"]["drawing_xy_cut"] == 1.0
    assert diagnostic["agreement"]["docling_drawing"] > 0.8


@pytest.mark.parametrize("count", [0, 1, 4, 20])
def test_kendall_tau_counts_reversed_pairs_and_shared_elements(count):
    order = list(range(count))
    assert kendall_tau(order, order) == 1.0
    assert kendall_tau(order, list(reversed(order))) == (-1.0 if count > 1 else 1.0)
    assert kendall_tau([99, 0, 1, 2], [0, 2, 1, 100]) == pytest.approx(1 / 3)
