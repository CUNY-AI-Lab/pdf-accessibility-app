from app.pipeline.orchestrator import _prepare_user_review_surface


def test_prepare_user_review_surface_keeps_hidden_blockers_out_of_surface():
    review_tasks = [
        {"task_type": "reading_order", "blocking": True},
        {"task_type": "alt_text", "blocking": False},
    ]
    fidelity_report = {"passed": True, "summary": {}}

    user_visible_review_tasks, updated_fidelity_report = _prepare_user_review_surface(
        review_tasks,
        fidelity_report,
    )

    assert [task["task_type"] for task in user_visible_review_tasks] == ["alt_text"]
    assert updated_fidelity_report["passed"] is False
    assert updated_fidelity_report["summary"] == {
        "blocking_tasks": 1,
        "advisory_tasks": 1,
        "total_tasks": 2,
    }
