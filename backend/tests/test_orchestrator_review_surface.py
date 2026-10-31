from app.pipeline.orchestrator import _prepare_user_review_surface


def test_prepare_user_review_surface_downgrades_unnamed_blockers_and_keeps_diagnostics():
    review_tasks = [
        {"task_type": "reading_order", "blocking": True},
        {"task_type": "alt_text", "blocking": False},
    ]
    fidelity_report = {"passed": True, "summary": {}}

    user_visible_review_tasks, updated_fidelity_report = _prepare_user_review_surface(
        review_tasks,
        fidelity_report,
    )

    assert [task["task_type"] for task in user_visible_review_tasks] == ["reading_order", "alt_text"]
    assert updated_fidelity_report["passed"] is True
    assert updated_fidelity_report["summary"] == {
        "blocking_tasks": 0,
        "advisory_tasks": 2,
        "total_tasks": 2,
    }


def test_unknown_check_error_has_a_visible_explanation_without_duplicate_tasks():
    tasks = [{"task_type": "future_check", "blocking": True,
              "metadata": {"check": "future_check"}}]
    report = {"checks": [{"check": "future_check", "status": "error"}]}

    for _ in range(2):
        visible, updated = _prepare_user_review_surface(tasks, report)
        assert len(visible) == 1
        assert visible[0]["task_type"] == "content_fidelity"
        assert visible[0]["blocking"] is True
        assert visible[0]["title"] and visible[0]["detail"]
        assert updated["passed"] is False
        assert updated["summary"]["blocking_tasks"] == 1
