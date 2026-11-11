"""Attributing a failed olmOCR-Bench test to the stage that lost it."""

from scripts.attribute_failures import (
    bucket,
    docling_text,
    fold,
    layer_has_words,
    passages,
    summarize,
    words_in_order,
)


def test_docling_text_reads_elements_once_without_artifacts_or_figures():
    structure_json = {
        "elements": [
            {"type": "artifact", "text": "Running head"},
            {"type": "heading", "level": 2, "text": "Methods"},
            {"type": "paragraph", "text": "First  part\nof text"},
            {"type": "figure", "caption": "Figure 1"},
            {"type": "list_item", "text": "an item"},
            {"type": "paragraph", "text": "First part of text", "continuation": True},
            {
                "type": "table",
                "cells": [
                    {"row": 1, "col": 0, "text": "1 < 2"},
                    {"row": 0, "col": 0, "text": "Name", "is_header": True, "col_span": 2},
                ],
            },
        ]
    }
    assert docling_text(structure_json, markdown=True) == (
        "## Methods\n\nFirst part of text\n\n- an item\n\n"
        '<table><tr><th colspan="2">Name</th></tr><tr><td>1 &lt; 2</td></tr></table>'
    )
    assert docling_text(structure_json, markdown=False) == (
        "Methods\n\nFirst part of text\n\nan item\n\nName\n1 < 2"
    )


def test_passages_are_what_a_test_needs():
    assert passages({"type": "present", "text": "a"}) == ["a"]
    assert passages({"type": "order", "before": "a", "after": "b"}) == ["a", "b"]
    assert passages({"type": "table", "cell": "x", "up": "", "left_heading": "h"}) == ["x", "h"]
    assert passages({"type": "math", "math": "x^2"}) == []


def test_fold_sets_aside_punctuation_spacing_case_and_accents():
    assert fold("Evolução … the ﬁrst, non-\nseeded") == fold("evoluça˜o ... The first nonseeded")


def test_words_in_order_judges_present_and_order_tests_only():
    order = {"type": "order", "before": "First part.", "after": "Second part"}
    assert words_in_order(order, "first part … second-part")
    assert not words_in_order(order, "second part, first part")
    assert words_in_order({"type": "present", "text": "a b"}, "A  b!")
    assert words_in_order({"type": "absent", "text": "a"}, "a") is None


def test_layer_has_words_in_any_order():
    order = {"type": "order", "before": "alpha beta", "after": "gamma"}
    assert layer_has_words(order, "gamma 441 alpha 442 beta")
    assert layer_has_words({"type": "present", "text": "effectiveness"}, "effective- 457 ness")
    assert not layer_has_words(order, "alpha gamma")
    assert layer_has_words({"type": "absent", "text": "x"}, "") is None


def test_bucket_follows_the_stages_back_from_the_output():
    def attributed(
        tagged=False, docling=False, docling_order=False, layer=True, source=None, ocred=True
    ):
        return bucket(
            tagged_in_order=tagged,
            docling_passes=docling,
            docling_in_order=docling_order,
            layer_words=layer,
            source_words=source,
            ocred=ocred,
        )

    assert attributed(tagged=True, docling=True) == "characters"
    assert attributed(docling=True) == "tagger"
    assert attributed(docling_order=True, layer=False) == "tagger"
    assert attributed(layer=False) == "ocr"
    assert attributed(layer=False, source=False, ocred=False) == "pdf text"
    assert attributed(layer=False, source=True, ocred=False) == "font repair"
    assert attributed(layer=True) == "docling"
    assert attributed(layer=None) == "docling"


def test_summarize_counts_by_subset_type_and_bucket():
    summary = summarize(
        [
            {"subset": "s", "type": "present", "bucket": "ocr"},
            {"subset": "s", "type": "order", "bucket": "tagger"},
            {"subset": "s", "type": "present", "bucket": "ocr"},
        ]
    )
    assert summary["s"]["all"] == {"ocr": 2, "tagger": 1}
    assert summary["s"]["present"] == {"ocr": 2}
