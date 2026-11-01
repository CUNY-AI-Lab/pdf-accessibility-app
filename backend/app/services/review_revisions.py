"""Review revisions and bounded compensation copies.

From       To
prepared   running, failed
running    published, failed
published  terminal (replay is a no-op)
failed     terminal (retry creates a new revision)

Keep the latest published PDF and the current/in-flight PDF. Older snapshots
retain decisions, edits and output digests, but retire their file pointers.
Commit retired pointers before deleting files; the next prune retries orphans.
Historical compensation uses conditional Core SQL, preserving legacy values
without passing them through new-write ORM validators.
"""

import hashlib
import json
import shutil
import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import DateTime, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.models import AltTextEntry, AppliedChange, Job, JobStep, ReviewRevision, ReviewTask
from app.services.applied_changes import parse_json_dict
from app.services.job_transitions import guard_transition, set_job_status
from app.services.review_errors import ReviewRefusal


class RevisionPhase(StrEnum):
    PREPARED = "prepared"
    RUNNING = "running"
    PUBLISHED = "published"
    FAILED = "failed"


REVISION_TRANSITIONS = {
    RevisionPhase.PREPARED: {RevisionPhase.RUNNING, RevisionPhase.FAILED},
    RevisionPhase.RUNNING: {RevisionPhase.PUBLISHED, RevisionPhase.FAILED},
    RevisionPhase.PUBLISHED: set(),
    RevisionPhase.FAILED: set(),
}
MODELS = (Job, JobStep, AltTextEntry, AppliedChange, ReviewTask)


def advance_revision(revision, target):
    guard_transition(revision.phase, target, REVISION_TRANSITIONS, entity="review revision")
    revision.phase = target


def preserve_output(path: Path) -> str:
    """Protect current bytes until publication/compensation retires the copy."""
    if not path.is_file():
        return str(path)
    if path.parent.parent.name == "published":
        return str(path)
    folder = path.parent / "published" / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    shutil.copyfile(path, target)
    return str(target)


def _preserved_path(raw, root: Path) -> Path | None:
    if not isinstance(raw, str):
        return None
    path = Path(raw).resolve()
    if path.parent.parent == root:
        try:
            if uuid.UUID(path.parent.name).hex == path.parent.name:
                return path
        except ValueError:
            pass
    return None


def _snapshot_paths(raw: str | None, root: Path) -> set[Path]:
    state = parse_json_dict(raw)
    return {
        path
        for row in state.get("jobs", [])
        if isinstance(row, dict)
        if (path := _preserved_path(row.get("output_path"), root)) is not None
    }


async def prune_preserved_outputs(db, job) -> None:
    """Retain at most the latest published copy and a current/in-flight copy.

    Historical snapshots lose retired output_path pointers, retaining SHA-256
    evidence instead. No status, command, edit or review history is deleted.
    Only UUID directories in this job's published directory are considered.
    """
    if not job.output_path:
        return
    current = Path(job.output_path).resolve()
    root = (
        current.parent.parent
        if current.parent.parent.name == "published"
        else current.parent / "published"
    )
    rows = list(
        (
            await db.scalars(
                select(ReviewRevision)
                .where(
                    ReviewRevision.job_id == job.id,
                )
                .order_by(ReviewRevision.revision)
            )
        ).all()
    )
    keep = {current}
    published = next((row for row in reversed(rows) if row.phase == RevisionPhase.PUBLISHED), None)
    if published:
        keep.update(_snapshot_paths(published.published_json, root))
    # Only the latest revision can still compensate (_reserve_revision_transition).
    if rows and rows[-1].phase == RevisionPhase.RUNNING:
        keep.update(_snapshot_paths(rows[-1].before_json, root))
        keep.update(_snapshot_paths(rows[-1].edits_json, root))
    retired = {
        path.resolve()
        for path in root.glob("*/*")
        if path.is_file()
        and path.suffix.lower() == ".pdf"
        and _preserved_path(str(path), root) is not None
        and path.resolve() not in keep
    }
    digests = {}
    for path in retired:
        with path.open("rb") as stream:
            digests[path] = hashlib.file_digest(stream, "sha256").hexdigest()
    for row in rows:
        for field in ("before_json", "edits_json", "published_json"):
            raw = getattr(row, field)
            state = parse_json_dict(raw)
            changed = False
            for saved_job in state.get("jobs", []):
                if not isinstance(saved_job, dict):
                    continue
                path = _preserved_path(saved_job.get("output_path"), root)
                if path is not None and path not in keep:
                    saved_job["output_path"] = None
                    if path in digests:
                        saved_job["retired_output_sha256"] = digests[path]
                    changed = True
            if changed:
                setattr(row, field, json.dumps(state))
    # A crash after commit can leave extra files, but never a dangling live pointer.
    await db.commit()
    for path in retired:
        path.unlink(missing_ok=True)
        if not any(path.parent.iterdir()):
            path.parent.rmdir()


async def snapshot(db, job_id: str) -> str:
    result = {}
    for model in MODELS:
        key = model.__table__.c["id" if model is Job else "job_id"]
        rows = (await db.execute(select(model).where(key == job_id))).scalars().all()
        result[model.__tablename__] = [
            {col.name: getattr(row, col.name) for col in model.__table__.columns} for row in rows
        ]
    return json.dumps(result, default=lambda value: value.isoformat())


async def prepare_review(db, job, action: dict, *, replay: bool = False) -> ReviewRevision:
    if job.status not in {"complete", "manual_remediation"}:
        raise ReviewRefusal(code=409, detail="Review needs a published output")
    command = json.dumps(action, sort_keys=True)
    command_id = hashlib.sha256(command.encode()).hexdigest()
    prior = await db.scalar(
        select(ReviewRevision)
        .where(
            ReviewRevision.job_id == job.id,
            ReviewRevision.command_json == command,
        )
        .order_by(ReviewRevision.revision.desc())
        .limit(1)
    )
    if prior is not None and prior.phase == RevisionPhase.PUBLISHED and replay:
        return prior
    if prior is not None and prior.phase != RevisionPhase.FAILED:
        raise ReviewRefusal(code=409, detail="Review command was already recorded")
    # First command captures the deployed runner's output before any writer can overwrite it.
    if job.output_path:
        job.output_path = preserve_output(Path(job.output_path))
    revision = (
        int(
            await db.scalar(
                select(func.max(ReviewRevision.revision)).where(
                    ReviewRevision.job_id == job.id,
                )
            )
            or 0
        )
        + 1
    )
    row = ReviewRevision(
        job_id=job.id,
        revision=revision,
        command_id=f"{command_id}:retry:{revision}" if prior is not None else command_id,
        command_json=command,
        phase=RevisionPhase.PREPARED,
        before_json=await snapshot(db, job.id),
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ReviewRefusal(code=409, detail="Review revision changed; retry") from exc
    return row


async def start_review(db, job, revision) -> None:
    revision.edits_json = await snapshot(db, job.id)
    previous = job.status
    # Guard before the conditional write; suppress autoflush of the new status.
    with db.no_autoflush:
        set_job_status(job, "processing")
        result = await db.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == previous)
            .values(status="processing")
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        raise ReviewRefusal(code=409, detail="Review job changed; retry")
    advance_revision(revision, RevisionPhase.RUNNING)


async def _reserve_revision_transition(db, row, phase) -> None:
    """The conditional write takes SQLite's writer lock before snapshot/restore."""
    await db.flush()
    previous = row.phase
    latest = (
        select(func.max(ReviewRevision.revision))
        .where(
            ReviewRevision.job_id == row.job_id,
        )
        .scalar_subquery()
    )
    with db.no_autoflush:
        advance_revision(row, phase)
        result = await db.execute(
            update(ReviewRevision)
            .where(
                ReviewRevision.job_id == row.job_id,
                ReviewRevision.revision == row.revision,
                ReviewRevision.phase == previous,
                ReviewRevision.revision == latest,
            )
            .values(phase=phase)
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        raise RuntimeError("Review revision changed; cannot publish or restore")


async def finish_review(db, job, revision) -> None:
    if revision.phase == RevisionPhase.PUBLISHED:
        await prune_preserved_outputs(db, job)
        return
    if job.status not in {"complete", "manual_remediation"}:
        raise RuntimeError(job.error or "Review did not produce a published result")
    await _reserve_revision_transition(db, revision, RevisionPhase.PUBLISHED)
    revision.published_json = await snapshot(db, job.id)
    await db.commit()
    await prune_preserved_outputs(db, job)


async def fail_review(db, job_id: str, revision_number: int, error: str) -> None:
    """Compensate all edits/steps/evidence, conditional on the owned revision."""
    await db.rollback()
    row = await db.get(ReviewRevision, (job_id, revision_number))
    if row is None or row.phase != RevisionPhase.RUNNING:
        raise RuntimeError("Review transaction no longer owns this revision")
    await _reserve_revision_transition(db, row, RevisionPhase.FAILED)
    state = json.loads(row.before_json)
    for model in reversed(MODELS[1:]):
        await db.execute(delete(model).where(model.job_id == job_id))
    for model in MODELS:
        for values in state[model.__tablename__]:
            for col in model.__table__.columns:
                if isinstance(col.type, DateTime) and values.get(col.name):
                    values[col.name] = datetime.fromisoformat(values[col.name])
            if model is Job:
                await db.execute(update(Job).where(Job.id == job_id).values(**values))
            else:
                # Restoration replays the exact stored snapshot, including legacy values.
                await db.execute(insert(model).values(**values))
    row.error = error
    await db.commit()
    await prune_preserved_outputs(db, await db.get(Job, job_id))
    db.expire_all()


async def reconcile_review_tasks(db, job_id: str, tasks: list[dict]) -> None:
    """Reuse unchanged issue identities/acknowledgements; journal retains old evidence."""
    old = list((await db.scalars(select(ReviewTask).where(ReviewTask.job_id == job_id))).all())
    retained = set()
    for task in tasks:
        values = {
            "task_type": str(task.get("task_type") or "review_task"),
            "title": str(task.get("title") or "Follow-up review required"),
            "detail": str(task.get("detail") or ""),
            "severity": str(task.get("severity") or "medium"),
            "blocking": bool(task.get("blocking", True)),
            "source": str(task.get("source") or "fidelity"),
            "metadata_json": json.dumps(task.get("metadata") or {}, sort_keys=True),
        }
        match = next(
            (
                row
                for row in old
                if row.id not in retained
                and all(
                    (
                        parse_json_dict(row.metadata_json) == json.loads(value)
                        if key == "metadata_json"
                        else getattr(row, key) == value
                    )
                    for key, value in values.items()
                )
            ),
            None,
        )
        if match:
            retained.add(match.id)
        else:
            db.add(ReviewTask(job_id=job_id, status="pending_review", **values))
    # Insert before deleting so SQLite cannot recycle an old visible task ID.
    await db.flush()
    for row in old:
        if row.id not in retained:
            await db.delete(row)
