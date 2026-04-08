from app.pipeline.orchestrator import _alt_text_step_result, _empty_llm_usage


def test_alt_text_step_result_includes_llm_usage_and_reclassification() -> None:
    llm_usage = {
        "request_count": 3,
        "prompt_tokens": 1200,
        "completion_tokens": 200,
        "total_tokens": 1400,
        "cost_usd": 0.0123,
    }
    figure_reclassification = {
        "attempted": True,
        "applied": False,
        "reason": "no_candidates",
    }

    result = _alt_text_step_result(
        count=6,
        approved=5,
        rejected=1,
        reviewable_changes=6,
        reclassified=0,
        auto_approve_enabled=True,
        llm_usage=llm_usage,
        figure_reclassification=figure_reclassification,
    )

    assert result["count"] == 6
    assert result["approved"] == 5
    assert result["rejected"] == 1
    assert result["reviewable_changes"] == 6
    assert result["reclassified"] == 0
    assert result["auto_approve_enabled"] is True
    assert result["llm_usage"] == llm_usage
    assert result["figure_reclassification"] == figure_reclassification


def test_alt_text_step_result_skip_shape_uses_empty_llm_usage() -> None:
    result = _alt_text_step_result(
        count=0,
        approved=0,
        rejected=0,
        reviewable_changes=0,
        reclassified=0,
        auto_approve_enabled=False,
        llm_usage=_empty_llm_usage(),
        reason="disabled_by_settings",
        skipped_figure_count=4,
    )

    assert result["reason"] == "disabled_by_settings"
    assert result["skipped_figure_count"] == 4
    assert result["llm_usage"] == {
        "request_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }
