"""Persisted job and step contracts; stored strings remain readable.

Job from             To
queued               processing, failed
processing           complete, manual_remediation, failed
complete/manual_remediation  processing, failed
failed               terminal

Step from            To
pending              running, complete, skipped, failed
running              complete, skipped, failed
complete             pending, failed
skipped              pending, failed
failed               pending

Known self-replays are allowed. Pending->complete supports preserved author
PDFs. Reruns explicitly reset terminal steps to pending. Starting clears old
completion/payload/error; duplicate starts retain their original start time.
"""

import json
from datetime import UTC, datetime
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    MANUAL_REMEDIATION = "manual_remediation"
    FAILED = "failed"


JOB_TRANSITIONS = {
    JobStatus.QUEUED: {JobStatus.PROCESSING, JobStatus.FAILED},
    JobStatus.PROCESSING: {JobStatus.COMPLETE, JobStatus.MANUAL_REMEDIATION, JobStatus.FAILED},
    JobStatus.COMPLETE: {JobStatus.PROCESSING, JobStatus.FAILED},
    JobStatus.MANUAL_REMEDIATION: {JobStatus.PROCESSING, JobStatus.FAILED},
    JobStatus.FAILED: set(),
}


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    SKIPPED = "skipped"
    FAILED = "failed"


STEP_TRANSITIONS = {
    # Preserved author PDFs are evaluated without starting the tagging steps.
    StepStatus.PENDING: {
        StepStatus.RUNNING,
        StepStatus.COMPLETE,
        StepStatus.SKIPPED,
        StepStatus.FAILED,
    },
    StepStatus.RUNNING: {StepStatus.COMPLETE, StepStatus.SKIPPED, StepStatus.FAILED},
    StepStatus.COMPLETE: {StepStatus.PENDING, StepStatus.FAILED},
    StepStatus.SKIPPED: {StepStatus.PENDING, StepStatus.FAILED},
    StepStatus.FAILED: {StepStatus.PENDING},
}


def guard_transition(current, target, transitions, *, entity: str) -> None:
    # Unknown legacy states can be read, but cannot silently become a known state.
    if (
        target not in transitions
        or current not in transitions
        or (target != current and target not in transitions[current])
    ):
        raise ValueError(f"Illegal {entity} transition: {current!r} -> {target!r}")


def set_job_status(job, status: str) -> None:
    guard_transition(job.status, status, JOB_TRANSITIONS, entity="job")
    job.status = status


def set_step_status(step, status: str, *, result=None, error=None) -> None:
    previous = step.status or StepStatus.PENDING
    guard_transition(previous, status, STEP_TRANSITIONS, entity="step")
    now = datetime.now(UTC)
    if status == StepStatus.PENDING:
        step.started_at = step.completed_at = None
    elif status == StepStatus.RUNNING:
        if previous != StepStatus.RUNNING:
            step.started_at = now
        step.completed_at = None
    elif previous != status:
        step.completed_at = now
    step.status = status
    step.result_json = json.dumps(result) if result is not None else None
    step.error = error
