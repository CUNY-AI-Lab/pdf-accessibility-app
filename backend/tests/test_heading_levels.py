"""Heading levels survive tagging: nesting is kept, the outline starts at H1,
and no level is skipped."""

import pytest

from app.pipeline.tagger import _normalize_heading_hierarchy


def _headings(levels: list[int]) -> list[dict]:
    return [
        {"type": "heading", "level": level, "page": 0, "bbox": {"t": 700 - 20 * index, "l": 72}}
        for index, level in enumerate(levels)
    ]


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        ([1, 2, 3, 2, 1, 2], [1, 2, 3, 2, 1, 2]),
        ([2, 3, 3, 2], [1, 2, 2, 1]),
        ([1, 3, 4], [1, 2, 3]),
        # A chapter label deeper than the section headings after it does not
        # squeeze their levels together.
        ([3, 1, 2, 3, 2], [1, 1, 2, 3, 2]),
        # Sections given one deep level stay siblings, not a staircase.
        ([1, 2, 5, 5, 5], [1, 2, 3, 3, 3]),
        ([1, 4, 4, 2, 4], [1, 2, 2, 2, 3]),
        ([1, 2, 3, 4, 5, 6, 7, 7], [1, 2, 3, 4, 5, 6, 6, 6]),
    ],
)
def test_heading_nesting_is_kept(levels, expected):
    elements = _headings(levels)
    _normalize_heading_hierarchy(elements)
    assert [element["level"] for element in elements] == expected
