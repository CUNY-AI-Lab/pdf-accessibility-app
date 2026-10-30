"""The release decision at the end of `_run_tagging_and_validation`.

Tagging, veraPDF, fidelity and the model-backed steps are replaced by fakes keyed
on the output file name, so each test states which candidate PDF is compliant and
which review tasks its fidelity pass raises. The orchestrator's own choices
(which candidate it adopts, which status the job ends in, which review tasks it
stores) run for real.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Job, JobStep, ReviewTask
from app.pipeline import orchestrator

TAGGED = "accessible_sample.pdf"
AUTO_REVIEW = "accessible_auto_review_sample.pdf"
FONT_MAP = "font_map_candidate.pdf"


class _RecordingJobManager:
    def __init__(self):
        self.events: list[dict[str, object]] = []

    def emit_progress(self, job_id: str, **event_data):
        self.events.append({"job_id": job_id, **event_data})


def _audit(**overrides):
    return {
        "applied": False,
        "applied_count": 0,
        "llm_usage": orchestrator._empty_llm_usage(),
        **overrides,
    }


def _blocking(task_type: str = "table_semantics") -> dict[str, object]:
    return {
        "task_type": task_type,
        "title": "Blocking review",
        "detail": "",
        "severity": "high",
        "blocking": True,
        "status": "pending_review",
        "source": "fidelity",
        "metadata": {},
    }


def _write_pdf(path: Path) -> None:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(path)


def _fidelity(tasks: list[dict[str, object]]) -> dict[str, object]:
    blocking = len([task for task in tasks if task.get("blocking")])
    return {
        "passed": blocking == 0,
        "summary": {
            "blocking_tasks": blocking,
            "advisory_tasks": len(tasks) - blocking,
            "total_tasks": len(tasks),
        },
        "checks": [],
    }


async def _run_release(
    tmp_path: Path,
    monkeypatch,
    *,
    compliant: dict[str, bool],
    fidelity_tasks: dict[str, list[dict[str, object]]],
    structure_json: dict | None = None,
    auto_structure_specs: list[dict[str, object]] | None = None,
    font_map_candidate: bool = False,
):
    """Run the tagging/validation flow and return (job, review task rows, tagged names)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    input_pdf = tmp_path / "input.pdf"
    _write_pdf(input_pdf)
    tagged_names: list[str] = []

    async def fake_validate_pdf(*, pdf_path, **_kwargs):
        name = Path(pdf_path).name
        is_compliant = compliant.get(name, False)
        violations = (
            []
            if is_compliant
            else [
                SimpleNamespace(
                    rule_id="ISO 14289-1:2014-7.1-3",
                    description="Content is neither marked as Artifact nor tagged",
                    severity="error",
                    location=None,
                    count=1,
                    category="structure",
                    fix_hint=None,
                )
            ]
        )
        return SimpleNamespace(
            compliant=is_compliant,
            violations=violations,
            raw_report={"report": {"jobs": []}},
        )

    async def fake_tag_pdf(*, output_path, **_kwargs):
        _write_pdf(output_path)
        tagged_names.append(Path(output_path).name)
        return SimpleNamespace(
            output_path=output_path,
            tags_added=1,
            lang_set=True,
            struct_elems_created=1,
            headings_tagged=0,
            figures_tagged=0,
            decorative_figures_artifacted=0,
            tables_tagged=0,
            lists_tagged=0,
            links_tagged=0,
            bookmarks_added=0,
            title_set=True,
        )

    def fake_assess_fidelity(*, output_pdf, **_kwargs):
        tasks = [dict(task) for task in fidelity_tasks.get(Path(output_pdf).name, [])]
        return _fidelity(tasks), tasks

    async def fake_grounded_text(**kwargs):
        return kwargs["structure_json"], _audit(applied_code_text_count=0)

    async def fake_table_intelligence(**kwargs):
        return kwargs["structure_json"], _audit(confirmed_count=0, set_headers_count=0)

    async def fake_widget_rationalization(**kwargs):
        return kwargs["working_pdf"], _audit()

    async def fake_form_intelligence(**kwargs):
        return kwargs["working_pdf"], _audit()

    async def fake_auto_structure(*, review_tasks, structure_json, **_kwargs):
        if not auto_structure_specs:
            return review_tasks, structure_json, []
        changed = {**structure_json, "auto_applied": True}
        return review_tasks, changed, list(auto_structure_specs)

    async def fake_font_map(*, review_tasks, current_validation, **_kwargs):
        audit = {"enabled": True, "attempted": font_map_candidate, "applied": False, "reason": ""}
        if not font_map_candidate:
            return audit, None, None, {}
        candidate_pdf = tmp_path / FONT_MAP
        _write_pdf(candidate_pdf)
        audit["applied"] = True
        candidate_validation = await fake_validate_pdf(pdf_path=candidate_pdf)
        return audit, candidate_validation, candidate_pdf, {}

    monkeypatch.setattr(orchestrator, "validate_pdf", fake_validate_pdf)
    monkeypatch.setattr(orchestrator, "tag_pdf", fake_tag_pdf)
    monkeypatch.setattr(orchestrator, "assess_fidelity", fake_assess_fidelity)
    monkeypatch.setattr(orchestrator, "get_output_path", lambda _job_id, name: tmp_path / name)
    monkeypatch.setattr(
        orchestrator, "_apply_pretag_grounded_text_resolutions", fake_grounded_text
    )
    monkeypatch.setattr(orchestrator, "_apply_pretag_table_intelligence", fake_table_intelligence)
    monkeypatch.setattr(
        orchestrator, "_apply_pretag_widget_rationalization", fake_widget_rationalization
    )
    monkeypatch.setattr(orchestrator, "_apply_pretag_form_intelligence", fake_form_intelligence)
    monkeypatch.setattr(
        orchestrator,
        "docling_pretag_ambiguity_router",
        lambda **_kwargs: {
            "plan": {},
            "table_targets": [],
            "widget_targets": [],
            "form_targets": [],
        },
    )
    monkeypatch.setattr(orchestrator, "auto_apply_structure_tasks", fake_auto_structure)
    monkeypatch.setattr(orchestrator, "_attempt_auto_llm_font_map", fake_font_map)

    settings = SimpleNamespace(
        verapdf_path="verapdf",
        verapdf_flavour="ua1",
        subprocess_timeout_validation=30,
        auto_apply_llm_font_map=font_map_candidate,
    )
    structure = structure_json if structure_json is not None else {"elements": []}

    async with session_maker() as db:
        db.add(
            Job(
                id="job-1",
                filename="stored.pdf",
                original_filename="sample.pdf",
                owner_session_hash="owner",
                input_path=str(input_pdf),
                status="processing",
                classification="digital",
                structure_json=json.dumps(structure),
            )
        )
        for step_name in ("tagging", "validation", "fidelity"):
            db.add(JobStep(job_id="job-1", step_name=step_name))
        await db.commit()

        await orchestrator.run_tagging_and_validation(
            "job-1",
            db,
            settings,
            _RecordingJobManager(),
            working_pdf=input_pdf,
            structure_json=structure,
        )

        job = await db.get(Job, "job-1")
        review_rows = (
            (await db.execute(select(ReviewTask).where(ReviewTask.job_id == "job-1")))
            .scalars()
            .all()
        )

    await engine.dispose()
    return job, review_rows, tagged_names


@pytest.mark.asyncio
async def test_compliant_output_without_blocking_tasks_is_released(tmp_path, monkeypatch):
    job, review_rows, tagged = await _run_release(
        tmp_path,
        monkeypatch,
        compliant={TAGGED: True},
        fidelity_tasks={TAGGED: []},
    )

    assert job.status == "complete"
    assert Path(job.output_path).name == TAGGED
    assert tagged == [TAGGED]
    assert review_rows == []
    assert json.loads(job.fidelity_json)["passed"] is True


@pytest.mark.asyncio
async def test_blocking_fidelity_task_holds_a_compliant_output(tmp_path, monkeypatch):
    job, review_rows, _ = await _run_release(
        tmp_path,
        monkeypatch,
        compliant={TAGGED: True},
        fidelity_tasks={TAGGED: [_blocking("content_fidelity")]},
    )

    assert job.status == "manual_remediation"
    assert Path(job.output_path).name == TAGGED
    assert [(row.task_type, row.blocking) for row in review_rows] == [("content_fidelity", True)]
    assert json.loads(job.fidelity_json)["passed"] is False


@pytest.mark.asyncio
async def test_hidden_blocking_task_still_holds_the_output(tmp_path, monkeypatch):
    job, review_rows, _ = await _run_release(
        tmp_path,
        monkeypatch,
        compliant={TAGGED: True},
        fidelity_tasks={TAGGED: [_blocking("figure_semantics")]},
    )

    # figure_semantics is not a user-visible task type, but it still blocks release.
    assert job.status == "manual_remediation"
    assert review_rows == []
    assert json.loads(job.fidelity_json)["summary"]["blocking_tasks"] == 1


@pytest.mark.asyncio
async def test_noncompliant_output_is_held_even_when_fidelity_passes(tmp_path, monkeypatch):
    job, _, _ = await _run_release(
        tmp_path,
        monkeypatch,
        compliant={TAGGED: False},
        fidelity_tasks={TAGGED: []},
    )

    assert job.status == "manual_remediation"
    assert json.loads(job.validation_json)["compliant"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auto_compliant", "auto_tasks", "expected_output", "expected_status"),
    [
        # Compliant with fewer blockers: the auto-structure retry replaces the first output.
        (True, [], AUTO_REVIEW, "complete"),
        # Fewer blockers but not compliant: the first output stays and the job is held.
        (False, [], TAGGED, "manual_remediation"),
        # Compliant but no fewer blockers: the first output stays.
        (True, [_blocking("table_semantics")], TAGGED, "manual_remediation"),
    ],
)
async def test_auto_structure_retry_is_adopted_only_when_compliant_and_better(
    tmp_path,
    monkeypatch,
    auto_compliant,
    auto_tasks,
    expected_output,
    expected_status,
):
    job, review_rows, tagged = await _run_release(
        tmp_path,
        monkeypatch,
        compliant={TAGGED: True, AUTO_REVIEW: auto_compliant},
        fidelity_tasks={TAGGED: [_blocking("table_semantics")], AUTO_REVIEW: auto_tasks},
        structure_json={"elements": [{"type": "paragraph", "text": "Body", "page": 0}]},
        auto_structure_specs=[{"task_type": "table_semantics", "title": "Set table headers"}],
    )

    assert tagged == [TAGGED, AUTO_REVIEW]
    assert Path(job.output_path).name == expected_output
    assert job.status == expected_status
    assert bool(json.loads(job.structure_json).get("auto_applied")) is (
        expected_output == AUTO_REVIEW
    )
    assert len(review_rows) == (0 if expected_status == "complete" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate_tasks", "expected_output", "expected_status"),
    [
        ([], FONT_MAP, "complete"),
        ([_blocking("font_text_fidelity"), _blocking("table_semantics")], TAGGED, "manual_remediation"),
    ],
)
async def test_font_map_candidate_is_adopted_only_when_fidelity_is_not_worse(
    tmp_path,
    monkeypatch,
    candidate_tasks,
    expected_output,
    expected_status,
):
    job, _, _ = await _run_release(
        tmp_path,
        monkeypatch,
        compliant={TAGGED: False, FONT_MAP: True},
        fidelity_tasks={TAGGED: [_blocking("font_text_fidelity")], FONT_MAP: candidate_tasks},
        structure_json={},
        font_map_candidate=True,
    )

    assert Path(job.output_path).name == expected_output
    assert job.status == expected_status
    font_map_audit = json.loads(job.validation_json)["remediation"]["llm_font_map_auto"]
    assert font_map_audit["applied"] is (expected_output == FONT_MAP)
