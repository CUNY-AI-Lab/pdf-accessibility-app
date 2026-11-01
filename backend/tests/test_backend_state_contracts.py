"""Production runner contracts, including legacy reads and stale review writes."""

import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.review import apply_review_action
from app.models import AltTextEntry, AppliedChange, Base, Job, JobStep, ReviewRevision, ReviewTask
from app.services.applied_changes import list_figure_changes
from app.services.candidate_state import (
    CANDIDATE_TRANSITIONS,
    CandidateDecision,
    CandidatePhase,
    candidate_decision,
)
from app.services.figure_state import set_figure_status
from app.services.job_transitions import (
    JOB_TRANSITIONS,
    STEP_TRANSITIONS,
    JobStatus,
    StepStatus,
    guard_transition,
    set_step_status,
)
from app.services.review_revisions import (
    fail_review,
    finish_review,
    prepare_review,
    reconcile_review_tasks,
    start_review,
)
from app.services.review_state import transition_review


@pytest.mark.parametrize("current,target", itertools.product(JobStatus, repeat=2))
def test_job_edges(current, target):
    allowed = {
        "queued": {"processing", "failed"},
        "processing": {"complete", "manual_remediation", "failed"},
        "complete": {"processing", "failed"},
        "manual_remediation": {"processing", "failed"},
        "failed": set(),
    }
    if current == target or target in allowed[current]:
        guard_transition(current, target, JOB_TRANSITIONS, entity="job")
    else:
        with pytest.raises(ValueError):
            guard_transition(current, target, JOB_TRANSITIONS, entity="job")


@pytest.mark.parametrize("current,target", itertools.product(StepStatus, repeat=2))
def test_step_edges(current, target):
    allowed = {
        "pending": {"running", "complete", "skipped", "failed"},
        "running": {"complete", "skipped", "failed"},
        "complete": {"pending", "failed"},
        "skipped": {"pending", "failed"},
        "failed": {"pending"},
    }
    if current == target or target in allowed[current]:
        guard_transition(current, target, STEP_TRANSITIONS, entity="step")
    else:
        with pytest.raises(ValueError):
            guard_transition(current, target, STEP_TRANSITIONS, entity="step")


def test_steps_clear_stale_payloads_and_preserve_duplicate_start_time():
    step = JobStep(
        status="complete", completed_at=object(), result_json='{"old":true}', error="old"
    )
    set_step_status(step, "pending")
    set_step_status(step, "running")
    started = step.started_at
    assert step.completed_at is None and step.result_json is None and step.error is None
    set_step_status(step, "running")
    assert step.started_at == started
    set_step_status(step, "complete", result={}, error="")
    assert step.result_json == "{}" and step.error == "" and step.completed_at is not None


def test_figure_approved_requires_usable_description():
    row = AltTextEntry(status="pending_review", generated_text="[figure]")
    with pytest.raises(ValueError):
        set_figure_status(row, "approved")
    row.edited_text = "Enrollment rose from 12 to 24 students."
    set_figure_status(row, "approved")
    set_figure_status(row, "rejected")
    with pytest.raises(ValueError):
        set_figure_status(row, "reclassified")


@pytest.mark.parametrize(
    "compliant,nonregressing,improvement", itertools.product((True, False), repeat=3)
)
def test_candidate_selection_contract(compliant, nonregressing, improvement):
    result = candidate_decision(
        compliant=compliant, nonregressing=nonregressing, improvement=improvement
    )
    assert (result == CandidateDecision.ACCEPTED) == (compliant and nonregressing and improvement)


@pytest.mark.parametrize("current,target", itertools.product(CandidatePhase, repeat=2))
def test_candidate_phases(current, target):
    allowed = {
        "inputs": {"tagged"},
        "tagged": {"validated"},
        "validated": {"assessing"},
        "assessing": {"assessed"},
        "assessed": {"assessing", "published"},
        "published": set(),
    }
    if current == target or target in allowed[current]:
        guard_transition(current, target, CANDIDATE_TRANSITIONS, entity="candidate")
    else:
        with pytest.raises(ValueError):
            guard_transition(current, target, CANDIDATE_TRANSITIONS, entity="candidate")


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def seed(db, tmp_path):
    output = tmp_path / "output.pdf"
    output.write_bytes(b"old published bytes")
    job = Job(
        id="j",
        filename="input.pdf",
        original_filename="input.pdf",
        owner_session_hash="runner",
        input_path=str(output),
        output_path=str(output),
        status="complete",
        validation_json='{"compliant": true}',
    )
    db.add(job)
    for index in range(2):
        db.add(
            AltTextEntry(
                job_id="j",
                figure_index=index,
                image_path="/tmp/image.png",
                generated_text=f"Description {index}",
                status="approved",
            )
        )
        db.add(
            AppliedChange(
                job_id="j",
                change_type="figure_semantics",
                title="Figure",
                detail="",
                reviewable=True,
                metadata_json=json.dumps({"figure_index": index}),
                undo_payload_json=json.dumps(
                    {"kind": "alt_text_entry", "figure_index": index, "status": "pending_review"}
                ),
            )
        )
    db.add(JobStep(job_id="j", step_name="tagging", status="complete"))
    await db.commit()
    return job, output


async def test_review_rerun_keeps_edits_other_pending_changes_and_old_output(sessions, tmp_path):
    async with sessions() as db:
        job, output = await seed(db, tmp_path)
        action = {"type": "edit", "change_id": 1, "text": "Enrollment by year"}
        revision = await prepare_review(db, job, action)
        await apply_review_action(job=job, db=db, action="edit", change_id=1, text=action["text"])
        await start_review(db, job, revision)
        await db.commit()
        # Simulate the candidate persistence boundary: it must not delete changes.
        await reconcile_review_tasks(db, "j", [])
        from app.services.job_transitions import set_job_status

        set_job_status(job, "complete")
        await finish_review(db, job, revision)
        output.write_bytes(b"a later candidate")
        history = json.loads(revision.before_json)
        assert Path(history["jobs"][0]["output_path"]).read_bytes() == b"old published bytes"
        changes = (await db.scalars(select(AppliedChange).order_by(AppliedChange.id))).all()
        assert [row.review_status for row in changes] == [
            "undone",
            "pending_review",
            "pending_review",
        ]
        assert {row.id for row in await list_figure_changes(db=db, job_id="j")} == {2, 3}
        assert json.loads(revision.edits_json)["alt_texts"][0]["edited_text"] == action["text"]
        assert revision.published_json and revision.phase == "published"


async def test_failed_review_restores_all_edits_and_retains_failed_command(sessions, tmp_path):
    async with sessions() as db:
        job, _ = await seed(db, tmp_path)
        revision = await prepare_review(db, job, {"type": "edit", "change_id": 1})
        await apply_review_action(
            job=job, db=db, action="edit", change_id=1, text="New description"
        )
        await start_review(db, job, revision)
        number = revision.revision
        await db.commit()
        await fail_review(db, "j", number, "tagging failed")
        restored = await db.get(Job, "j")
        assert restored.status == "complete"
        assert (await db.get(AltTextEntry, 1)).edited_text is None
        assert (await db.get(AppliedChange, 1)).review_status == "pending_review"
        assert await db.get(AppliedChange, 3) is None
        history = await db.get(ReviewRevision, ("j", number))
        assert history.phase == "failed" and history.edits_json


async def test_stale_review_writer_cannot_overwrite_winner(sessions, tmp_path):
    async with sessions() as db:
        await seed(db, tmp_path)
    async with sessions() as first, sessions() as stale:
        winner = await first.get(AppliedChange, 1)
        loser = await stale.get(AppliedChange, 1)
        await transition_review(first, winner, "kept")
        await first.commit()
        with pytest.raises(HTTPException) as exc:
            await transition_review(stale, loser, "undone")
        assert exc.value.status_code == 409


async def test_override_replay_cannot_reopen_terminal_review(sessions, tmp_path):
    async with sessions() as db:
        await seed(db, tmp_path)
        change = await db.get(AppliedChange, 1)
        await transition_review(db, change, "kept", replay=True)
        await transition_review(db, change, "kept", replay=True)
        with pytest.raises(HTTPException):
            await transition_review(db, change, "pending_review", replay=True)


async def test_same_review_evidence_keeps_acknowledgement_and_identity(sessions, tmp_path):
    task = {
        "task_type": "alt_text",
        "title": "Figure",
        "detail": "No description",
        "blocking": True,
    }
    async with sessions() as db:
        await seed(db, tmp_path)
        await reconcile_review_tasks(db, "j", [task])
        row = await db.scalar(select(ReviewTask))
        old_id = row.id
        await transition_review(db, row, "resolved")
        await reconcile_review_tasks(db, "j", [task])
        assert (await db.scalar(select(ReviewTask))).status == "resolved"
        await reconcile_review_tasks(db, "j", [{**task, "detail": "New evidence"}])
        await db.flush()
        row = await db.scalar(select(ReviewTask))
        assert row.id != old_id and row.status == "pending_review"


async def test_unknown_legacy_state_is_readable_but_cannot_transition(sessions, tmp_path):
    async with sessions() as db:
        await seed(db, tmp_path)
        await db.execute(text("UPDATE jobs SET status='legacy_unknown' WHERE id='j'"))
        await db.commit()
        db.expire_all()
        job = await db.get(Job, "j")
        assert job.status == "legacy_unknown"
        job.status = "complete"
        with pytest.raises(ValueError):
            await db.flush()


@pytest.mark.parametrize(
    "current,target", itertools.product(("pending_review", "kept", "undone"), repeat=2)
)
async def test_change_disposition_edges(sessions, tmp_path, current, target):
    async with sessions() as db:
        await seed(db, tmp_path)
        await db.execute(
            text("UPDATE applied_changes SET review_status=:state WHERE id=1"), {"state": current}
        )
        db.expire_all()
        row = await db.get(AppliedChange, 1)
        if current == target or (current, target) in {
            ("pending_review", "kept"),
            ("pending_review", "undone"),
            ("kept", "undone"),
        }:
            await transition_review(db, row, target, replay=True)
        else:
            with pytest.raises(HTTPException):
                await transition_review(db, row, target, replay=True)


@pytest.mark.parametrize(
    "current,target", itertools.product(("pending_review", "resolved"), repeat=2)
)
async def test_acknowledgement_edges(sessions, tmp_path, current, target):
    async with sessions() as db:
        await seed(db, tmp_path)
        row = ReviewTask(
            job_id="j", task_type="alt_text", title="Figure", detail="", status=current
        )
        db.add(row)
        await db.flush()
        if current == target or (current, target) == ("pending_review", "resolved"):
            await transition_review(db, row, target, replay=True)
        else:
            with pytest.raises(HTTPException):
                await transition_review(db, row, target, replay=True)


def test_running_clears_legacy_completion_time_and_empty_success_clears_results():
    step = JobStep(
        status="pending", completed_at=object(), result_json='{"old": true}', error="old"
    )
    set_step_status(step, "running")
    assert step.completed_at is None
    set_step_status(step, "complete", result={})
    set_step_status(step, "complete", result=None)
    assert step.result_json is None and step.error is None


async def test_compensation_cannot_overwrite_newer_revision(sessions, tmp_path):
    async with sessions() as db:
        job, _ = await seed(db, tmp_path)
        first = await prepare_review(db, job, {"type": "edit", "change_id": 1})
        await start_review(db, job, first)
        db.add(
            ReviewRevision(
                job_id="j",
                revision=2,
                command_id="newer",
                command_json="{}",
                before_json="{}",
                phase="running",
            )
        )
        await db.commit()
        with pytest.raises(RuntimeError):
            await fail_review(db, "j", 1, "late failure")
        assert (await db.get(Job, "j")).status == "processing"


async def test_duplicate_command_cannot_apply_twice(sessions, tmp_path):
    async with sessions() as db:
        job, _ = await seed(db, tmp_path)
        command = {"type": "edit", "change_id": 1}
        revision = await prepare_review(db, job, command)
        await start_review(db, job, revision)
        job.status = "complete"
        await finish_review(db, job, revision)
        with pytest.raises(HTTPException):
            await prepare_review(db, job, command)


def test_saved_release_decisions_and_reasons_are_identical():
    from pathlib import Path

    from scripts.replay_release_policy import replay

    path = Path(__file__).parents[1] / "eval/release-gate-ai-e128b6e.json"
    assert replay(path) == json.loads(path.read_text())


@pytest.mark.parametrize(
    "current,target", itertools.product(("prepared", "running", "published", "failed"), repeat=2)
)
def test_revision_phase_edges(current, target):
    from app.services.review_revisions import advance_revision

    allowed = {
        "prepared": {"running", "failed"},
        "running": {"published", "failed"},
        "published": set(),
        "failed": set(),
    }
    row = SimpleNamespace(phase=current)
    if current == target or target in allowed[current]:
        advance_revision(row, target)
    else:
        with pytest.raises(ValueError):
            advance_revision(row, target)


@pytest.mark.parametrize(
    "current,target", itertools.product(("approved", "pending_review", "rejected"), repeat=2)
)
def test_persisted_figure_edges(current, target):
    row = AltTextEntry(status=current, generated_text="A chart of enrollment by year")
    set_figure_status(row, target)
    assert row.status == target


def test_unknown_check_verdict_cannot_be_written():
    from app.services.candidate_state import set_check_verdict

    for verdict in ("pass", "fail", "warning", "skip", "error"):
        check = {"status": "fail"}
        set_check_verdict(check, verdict)
        assert check["status"] == verdict
    with pytest.raises(ValueError):
        set_check_verdict({"status": "pass"}, "unknown")


@pytest.mark.parametrize(
    "result,expected",
    [
        (None, "not_requested"),
        ({"blocks": []}, "cleared"),
        ({"blocks": [{"should_block_accessibility": True}]}, "confirmed"),
    ],
)
def test_absent_empty_and_confirmed_adjudication_are_distinct(result, expected):
    from app.services.grounded_text_intelligence import apply_grounded_text_adjudication

    tasks = [
        {
            "task_type": "content_fidelity",
            "source": "fidelity",
            "blocking": True,
            "metadata": {
                "grounded_text_candidate": True,
                "flagged_blocks": [],
                "grounded_text_llm_error": "old error",
            },
        }
    ]
    remaining, report = apply_grounded_text_adjudication(tasks, {"checks": []}, result)
    assert report["adjudication_state"] == expected
    assert all("grounded_text_llm_error" not in task["metadata"] for task in remaining)


async def test_failed_command_can_be_retried_as_a_new_revision(sessions, tmp_path):
    async with sessions() as db:
        job, _ = await seed(db, tmp_path)
        action = {"type": "edit", "change_id": 1}
        first = await prepare_review(db, job, action)
        await start_review(db, job, first)
        await db.commit()
        await fail_review(db, "j", 1, "failed candidate")
        job = await db.get(Job, "j")
        retry = await prepare_review(db, job, action)
        assert retry.revision == 2
        assert retry.command_id != (await db.get(ReviewRevision, ("j", 1))).command_id


@pytest.mark.parametrize("covered", [[], [7]])
def test_reclassified_figure_is_omitted_only_with_verified_replacement(covered):
    from app.services.figure_state import resolve_figure_for_storage

    row = SimpleNamespace(status="reclassified", figure_index=7, generated_text="")
    assert resolve_figure_for_storage(row, covered) == (not covered)
    if not covered:
        assert row.status == "pending_review"


@pytest.mark.parametrize(
    "current,target", itertools.product(("pass", "fail", "warning", "skip", "error"), repeat=2)
)
def test_check_verdict_edges(current, target):
    from app.services.candidate_state import set_check_verdict

    row = {"status": current}
    set_check_verdict(row, target)
    assert row["status"] == target


@pytest.mark.parametrize(
    "current,target",
    itertools.product(("not_requested", "pending", "confirmed", "cleared", "error"), repeat=2),
)
def test_adjudication_edges(current, target):
    from app.services.candidate_state import set_adjudication_state

    allowed = {
        "not_requested": {"pending"},
        "pending": {"not_requested", "confirmed", "cleared", "error"},
        "confirmed": {"pending"},
        "cleared": {"pending"},
        "error": {"pending"},
    }
    report = {"adjudication_state": current}
    if current == target or target in allowed[current]:
        set_adjudication_state(report, target)
        assert report["adjudication_state"] == target
    else:
        with pytest.raises(ValueError):
            set_adjudication_state(report, target)


async def test_stale_rerun_reservation_cannot_reopen_processing_job(sessions, tmp_path):
    async with sessions() as db:
        await seed(db, tmp_path)
    async with sessions() as winner, sessions() as stale:
        first_job = await winner.get(Job, "j")
        stale_job = await stale.get(Job, "j")
        first = await prepare_review(winner, first_job, {"type": "edit", "change_id": 1})
        await start_review(winner, first_job, first)
        await winner.commit()
        second = await prepare_review(stale, stale_job, {"type": "edit", "change_id": 2})
        with pytest.raises(HTTPException):
            await start_review(stale, stale_job, second)
        await stale.rollback()
        assert (await stale.get(Job, "j")).status == "processing"
        assert await stale.get(ReviewRevision, ("j", 2)) is None


async def test_undo_edit_keeps_restored_figure_visible_without_reopening_history(
    sessions, tmp_path
):
    async with sessions() as db:
        job, _ = await seed(db, tmp_path)
        await apply_review_action(
            job=job, db=db, action="edit", change_id=1, text="New description"
        )
        await apply_review_action(job=job, db=db, action="undo", change_id=3)
        current = await list_figure_changes(db=db, job_id="j")
        restored = next(
            row for row in current if json.loads(row.metadata_json)["figure_index"] == 0
        )
        assert restored.id == 4 and restored.review_status == "kept"
        assert json.loads(restored.after_json)["edited_text"] is None
        assert (await db.get(AppliedChange, 1)).review_status == "undone"
        assert (await db.get(AppliedChange, 3)).review_status == "undone"


async def test_published_revision_snapshot_is_immutable_on_replay(sessions, tmp_path):
    async with sessions() as db:
        job, _ = await seed(db, tmp_path)
        revision = await prepare_review(db, job, {"type": "edit", "change_id": 1})
        await start_review(db, job, revision)
        job.status = "complete"
        await finish_review(db, job, revision)
        published = revision.published_json
        job.error = "unrelated later local change"
        await finish_review(db, job, revision)
        assert revision.published_json == published


async def test_stale_publication_cannot_replace_a_newer_revision(sessions, tmp_path):
    async with sessions() as db:
        job, _ = await seed(db, tmp_path)
        first = await prepare_review(db, job, {"type": "edit", "change_id": 1})
        await start_review(db, job, first)
        db.add(
            ReviewRevision(
                job_id="j",
                revision=2,
                command_id="newer",
                command_json="{}",
                before_json="{}",
                phase="running",
            )
        )
        await db.commit()
        job.status = "complete"
        with pytest.raises(RuntimeError):
            await finish_review(db, job, first)
        await db.rollback()
        assert (await db.get(ReviewRevision, ("j", 1))).published_json is None


@pytest.mark.parametrize(
    "legacy_status,text",
    [
        ("accepted", "Stored caption"),
        ("approved", "[figure]"),
        ("pending_review", ""),
        ("rejected", "decorative"),
        ("old_unknown", "Legacy text"),
    ],
)
async def test_undo_restores_legacy_figure_without_new_approval_validation(
    sessions,
    tmp_path,
    legacy_status,
    text,
):
    async with sessions() as db:
        job, _ = await seed(db, tmp_path)
        change = await db.get(AppliedChange, 1)
        change.undo_payload_json = json.dumps(
            {
                "kind": "alt_text_entry",
                "figure_index": 0,
                "status": legacy_status,
                "generated_text": text,
                "edited_text": None,
            }
        )
        await db.commit()
        await apply_review_action(job=job, db=db, action="undo", change_id=1)
        await db.commit()
        row = await db.get(AltTextEntry, 1)
        row.image_path = "/tmp/unchanged-semantics.png"
        await db.commit()  # Ordinary updates must not revalidate a legacy status.
        await db.refresh(row)
        assert row.status == legacy_status and row.generated_text == text


async def test_compensation_restores_legacy_values_for_every_status_model(sessions, tmp_path):
    async with sessions() as db:
        job, _ = await seed(db, tmp_path)
        db.add(ReviewTask(job_id="j", task_type="alt_text", title="Legacy issue", detail=""))
        await db.commit()
        revision = await prepare_review(db, job, {"type": "edit", "change_id": 1})
        state = json.loads(revision.before_json)
        for table, field in (
            ("jobs", "status"),
            ("job_steps", "status"),
            ("alt_texts", "status"),
            ("applied_changes", "review_status"),
            ("review_tasks", "status"),
        ):
            state[table][0][field] = f"legacy_{table}"
        revision.before_json = json.dumps(state)
        await start_review(db, job, revision)
        await db.commit()
        await fail_review(db, "j", 1, "failed attempt")
        for model, field, identity in (
            (Job, "status", "j"),
            (JobStep, "status", 1),
            (AltTextEntry, "status", 1),
            (AppliedChange, "review_status", 1),
            (ReviewTask, "status", 1),
        ):
            restored = await db.get(model, identity)
            assert getattr(restored, field) == f"legacy_{model.__tablename__}"
            if model is AppliedChange:
                restored.detail = "History retained"
            elif model is ReviewTask:
                restored.detail = "Acknowledgement retained"
            elif model is JobStep:
                restored.error = "Historical context"
            elif model is AltTextEntry:
                restored.image_path = "/tmp/legacy.png"
            else:
                restored.error = "Historical context"
        await db.commit()  # before_update ignores statuses that were not changed.


_ORM_EDGES = {
    Job: (
        "status",
        {
            "queued": {"processing", "failed"},
            "processing": {"complete", "manual_remediation", "failed"},
            "complete": {"processing", "failed"},
            "manual_remediation": {"processing", "failed"},
            "failed": set(),
        },
    ),
    JobStep: (
        "status",
        {
            "pending": {"running", "complete", "skipped", "failed"},
            "running": {"complete", "skipped", "failed"},
            "complete": {"pending", "failed"},
            "skipped": {"pending", "failed"},
            "failed": {"pending"},
        },
    ),
    AltTextEntry: (
        "status",
        {
            state: {"approved", "pending_review", "rejected"}
            for state in ("approved", "pending_review", "rejected")
        },
    ),
    AppliedChange: (
        "review_status",
        {"pending_review": {"kept", "undone"}, "kept": {"undone"}, "undone": set()},
    ),
    ReviewTask: ("status", {"pending_review": {"resolved"}, "resolved": set()}),
}


@pytest.mark.parametrize(
    "model,field,current,target,allowed",
    [
        (model, field, current, target, current == target or target in transitions[current])
        for model, (field, transitions) in _ORM_EDGES.items()
        for current, target in itertools.product(transitions, repeat=2)
    ],
)
async def test_actual_orm_update_hooks_allow_runner_edges_and_reject_illegal_edges(
    sessions,
    tmp_path,
    model,
    field,
    current,
    target,
    allowed,
):
    async with sessions() as db:
        await seed(db, tmp_path)
        db.add(ReviewTask(job_id="j", task_type="alt_text", title="Issue", detail=""))
        await db.commit()
        key = "j" if model is Job else 1
        table = model.__table__
        await db.execute(table.update().where(table.c.id == key).values(**{field: current}))
        await db.commit()
        db.expire_all()
        row = await db.get(model, key)
        setattr(row, field, target)
        if allowed:
            await db.flush()
        else:
            with pytest.raises(ValueError):
                await db.flush()


async def test_failure_after_committed_skipped_step_does_not_mask_original_error(
    sessions,
    tmp_path,
    monkeypatch,
):
    from app.pipeline import orchestrator
    from app.pipeline.classify import ClassificationResult

    async def classify(_path):
        return ClassificationResult(
            type="digital", confidence=1.0, pages_with_text=1, total_pages=1
        )

    class FailingProgress:
        def emit_progress(self, _job_id, **event):
            if event.get("step") == "ocr" and event.get("status") == "skipped":
                raise RuntimeError("progress failed after skip")

    monkeypatch.setattr(orchestrator, "classify_pdf", classify)
    monkeypatch.setattr(orchestrator, "create_job_dir", lambda _job_id: tmp_path)
    monkeypatch.setattr(orchestrator, "read_author_structure", lambda _path: {})
    async with sessions() as db:
        db.add(
            Job(
                id="j",
                filename="input.pdf",
                original_filename="input.pdf",
                owner_session_hash="runner",
                input_path=str(tmp_path / "input.pdf"),
            )
        )
        await db.commit()
    await orchestrator.run_pipeline(
        "j", sessions, SimpleNamespace(ocr_language="eng"), FailingProgress()
    )
    async with sessions() as db:
        job = await db.get(Job, "j")
        ocr = await db.scalar(
            select(JobStep).where(JobStep.job_id == "j", JobStep.step_name == "ocr")
        )
        assert job.status == ocr.status == "failed"
        assert ocr.error == "progress failed after skip"


async def test_pruning_running_revision_keeps_compensation_and_candidate_only(sessions, tmp_path):
    from app.services.review_revisions import preserve_output, prune_preserved_outputs

    async with sessions() as db:
        job, output = await seed(db, tmp_path)
        revision = await prepare_review(db, job, {"type": "edit", "change_id": 1})
        await start_review(db, job, revision)
        await db.commit()
        baseline = Path(json.loads(revision.before_json)["jobs"][0]["output_path"])
        output.write_bytes(b"new candidate")
        for _ in range(3):
            preserve_output(output)  # Interrupted/older publication leftovers.
        job.output_path = preserve_output(output)
        await db.commit()
        await prune_preserved_outputs(db, job)
        copies = list((tmp_path / "published").glob("*/*.pdf"))
        assert set(copies) == {baseline, Path(job.output_path)}
        await fail_review(db, "j", 1, "failed new candidate")
        restored = await db.get(Job, "j")
        assert Path(restored.output_path).read_bytes() == b"old published bytes"
        assert list((tmp_path / "published").glob("*/*.pdf")) == [baseline]


async def test_stale_legacy_undo_cannot_overwrite_a_new_figure_decision(sessions, tmp_path):
    from app.services.figure_state import restore_stored_figure

    async with sessions() as db:
        await seed(db, tmp_path)
    async with sessions() as winner, sessions() as stale:
        old = await stale.get(AltTextEntry, 1)
        current = await winner.get(AltTextEntry, 1)
        set_figure_status(current, "rejected")
        await winner.commit()
        with pytest.raises(HTTPException) as exc:
            await restore_stored_figure(
                stale, old, {"status": "accepted", "generated_text": "Old caption"}
            )
        assert exc.value.status_code == 409
        await stale.rollback()
        assert (await stale.get(AltTextEntry, 1)).status == "rejected"
