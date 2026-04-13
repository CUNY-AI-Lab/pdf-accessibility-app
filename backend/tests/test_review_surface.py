from app.services.review_surface import (
    filter_user_visible_review_tasks,
    is_user_visible_applied_change_type,
    is_user_visible_review_task_type,
)


def test_filter_user_visible_review_tasks_keeps_only_legible_follow_up_types():
    review_tasks = [
        {"task_type": "annotation_description", "blocking": False},
        {"task_type": "alt_text", "blocking": False},
        {"task_type": "reading_order", "blocking": True},
        {"task_type": "table_semantics", "blocking": True},
        {"task_type": "font_text_fidelity", "blocking": True},
    ]

    filtered = filter_user_visible_review_tasks(review_tasks)

    assert [task["task_type"] for task in filtered] == [
        "annotation_description",
        "alt_text",
        "table_semantics",
    ]


def test_user_visible_review_task_types_match_product_surface():
    assert is_user_visible_review_task_type("annotation_description") is True
    assert is_user_visible_review_task_type("alt_text") is True
    assert is_user_visible_review_task_type("reading_order") is False
    assert is_user_visible_review_task_type("table_semantics") is True
    assert is_user_visible_review_task_type("form_semantics") is False


def test_only_legible_applied_changes_are_reviewable():
    assert is_user_visible_applied_change_type("figure_semantics") is True
    assert is_user_visible_applied_change_type("reading_order") is False
    assert is_user_visible_applied_change_type("table_semantics") is False
