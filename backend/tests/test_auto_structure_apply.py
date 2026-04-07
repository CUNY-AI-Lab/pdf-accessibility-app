import asyncio
from types import SimpleNamespace

import pytest

from app.services import auto_structure_apply


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _settings(**overrides):
    values = {
        "llm_timeout": 120,
        "llm_max_retries": 3,
        "llm_pretag_timeout": 45,
        "llm_pretag_max_retries": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_auto_apply_structure_tasks_uses_pretag_llm_profile(monkeypatch):
    captured: dict[str, object] = {}
    fake_client = _FakeClient()

    def _fake_make_llm_client_with_overrides(settings, **overrides):
        captured.update(overrides)
        return fake_client

    async def _fake_generate_remediation_intelligence(**kwargs):
        return {
            "task_type": "table_semantics",
            "summary": "Headers already look right.",
            "suggested_action": "confirm_current_headers",
        }

    monkeypatch.setattr(
        auto_structure_apply,
        "make_llm_client_with_overrides",
        _fake_make_llm_client_with_overrides,
    )
    monkeypatch.setattr(
        auto_structure_apply,
        "generate_remediation_intelligence",
        _fake_generate_remediation_intelligence,
    )

    remaining_tasks, updated_structure, applied_specs = await auto_structure_apply.auto_apply_structure_tasks(
        job=SimpleNamespace(),
        settings=_settings(llm_pretag_timeout=33, llm_pretag_max_retries=1),
        review_tasks=[
            {
                "task_type": "table_semantics",
                "title": "Review complex table semantics",
                "detail": "Review headers.",
                "severity": "high",
                "blocking": True,
                "source": "fidelity",
                "metadata": {"pages_to_check": [1]},
            }
        ],
        structure_json={"elements": []},
    )

    assert captured == {"timeout": 33, "max_retries": 1}
    assert remaining_tasks == []
    assert updated_structure == {"elements": []}
    assert applied_specs == []
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_auto_apply_structure_tasks_times_out_and_keeps_task(monkeypatch):
    fake_client = _FakeClient()

    monkeypatch.setattr(
        auto_structure_apply,
        "make_llm_client_with_overrides",
        lambda settings, **overrides: fake_client,
    )
    monkeypatch.setattr(
        auto_structure_apply,
        "_auto_structure_task_timeout_seconds",
        lambda settings: 0.01,
    )

    async def _slow_generate_remediation_intelligence(**kwargs):
        await asyncio.sleep(0.05)
        return {
            "task_type": "table_semantics",
            "summary": "slow",
            "suggested_action": "manual_only",
        }

    monkeypatch.setattr(
        auto_structure_apply,
        "generate_remediation_intelligence",
        _slow_generate_remediation_intelligence,
    )

    review_task = {
        "task_type": "table_semantics",
        "title": "Review complex table semantics",
        "detail": "Review headers.",
        "severity": "high",
        "blocking": True,
        "source": "fidelity",
        "metadata": {"pages_to_check": [7]},
    }

    remaining_tasks, updated_structure, applied_specs = await auto_structure_apply.auto_apply_structure_tasks(
        job=SimpleNamespace(),
        settings=_settings(),
        review_tasks=[review_task],
        structure_json={"elements": [{"type": "table", "page": 6}]},
    )

    assert updated_structure == {"elements": [{"type": "table", "page": 6}]}
    assert applied_specs == []
    assert len(remaining_tasks) == 1
    assert remaining_tasks[0]["task_type"] == "table_semantics"
    assert remaining_tasks[0]["metadata"]["auto_apply_attempted"] is True
    assert remaining_tasks[0]["metadata"]["auto_apply_error"] == "timeout"
    assert remaining_tasks[0]["metadata"]["auto_apply_timeout_seconds"] == 0.0
    assert fake_client.closed is True
