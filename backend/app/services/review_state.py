"""Review disposition is independent of release evidence.

Change from          To
pending_review       kept, undone
kept                 undone
undone               terminal

Task from            To
pending_review       resolved (acknowledged, never release approval)
resolved             terminal

Sync permits equivalent self-replay; other duplicate commands conflict.
Superseded edits retain the deployed wire status undone plus metadata.
All live writes use compare-and-set; snapshot restoration is historical SQL.
"""

from enum import StrEnum

from sqlalchemy import update
from sqlalchemy.orm.attributes import set_committed_value

from app.services.job_transitions import guard_transition
from app.services.review_errors import ReviewRefusal


class ChangeStatus(StrEnum):
    PENDING = "pending_review"
    KEPT = "kept"
    UNDONE = "undone"  # Also the wire-compatible disposition for a superseded edit.


CHANGE_TRANSITIONS = {
    ChangeStatus.PENDING: {ChangeStatus.KEPT, ChangeStatus.UNDONE},
    ChangeStatus.KEPT: {ChangeStatus.UNDONE},
    ChangeStatus.UNDONE: set(),
}


class ReviewStatus(StrEnum):
    PENDING = "pending_review"
    RESOLVED = "resolved"  # Acknowledged, never release approval.


REVIEW_TRANSITIONS = {
    ReviewStatus.PENDING: {ReviewStatus.RESOLVED},
    ReviewStatus.RESOLVED: set(),
}


async def transition_review(db, row, target: str, *, replay=False) -> None:
    """Compare-and-set so a stale edit/keep/resolve cannot overwrite a winner."""
    field = "review_status" if hasattr(row, "review_status") else "status"
    table = CHANGE_TRANSITIONS if field == "review_status" else REVIEW_TRANSITIONS
    current = getattr(row, field)
    try:
        guard_transition(current, target, table, entity="review")
    except ValueError as exc:
        raise ReviewRefusal(code=409, detail=str(exc)) from exc
    if current == target and not replay:
        raise ReviewRefusal(code=409, detail="Review action was already applied")
    model = type(row)
    result = await db.execute(
        update(model)
        .where(model.id == row.id, model.job_id == row.job_id, getattr(model, field) == current)
        .values(**{field: target})
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ReviewRefusal(code=409, detail="Review revision changed; refresh and retry")
    set_committed_value(row, field, target)
