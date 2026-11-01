"""Figure semantics, distinct from human acknowledgement.

State                Meaning / next states
approved             usable description; any known semantic decision
pending_review       needs description; any known semantic decision
rejected             decorative; any known semantic decision
reclassified         transient proposal; covered replacement or pending_review

Persistent reclassified writes are forbidden. Approved new writes require
usable text. The legacy accepted alias maps to approved for new decisions;
undo replays stored semantics exactly using conditional SQL, including legacy
statuses or descriptions. Unknown stored values never fail an ordinary read.
"""

from enum import StrEnum

from app.services.alt_text_cache import usable_description
from app.services.job_transitions import guard_transition


class FigureStatus(StrEnum):
    DESCRIPTION = "approved"
    NEEDS_DESCRIPTION = "pending_review"
    DECORATIVE = "rejected"
    RECLASSIFIED = "reclassified"  # Transient: replacement must cover the figure.


FIGURE_TRANSITIONS = {state: set(FigureStatus) for state in FigureStatus}


def figure_status(raw: str) -> str:
    # Old reports used accepted. Unknown stored values remain readable.
    return FigureStatus.DESCRIPTION if raw == "accepted" else raw


def set_figure_status(entry, status: str, *, persistent: bool = True) -> None:
    guard_transition(figure_status(entry.status), status, FIGURE_TRANSITIONS, entity="figure")
    if persistent and status == FigureStatus.RECLASSIFIED:
        raise ValueError("A reclassified figure needs verified replacement before persistence")
    if status == FigureStatus.DESCRIPTION and not usable_description(
        getattr(entry, "edited_text", None)
        if getattr(entry, "edited_text", None) is not None
        else entry.generated_text
    ):
        raise ValueError("An approved figure needs a usable description")
    entry.status = status


def resolve_figure_for_storage(result, covered_indexes: list[int]) -> bool:
    """Omit only verified replacements; retain every uncovered figure."""
    if result.status == FigureStatus.RECLASSIFIED:
        if result.figure_index in covered_indexes:
            return False
        set_figure_status(result, FigureStatus.NEEDS_DESCRIPTION, persistent=False)
    return True


async def restore_stored_figure(db, entry, state: dict) -> None:
    """Undo replays trusted stored semantics, even legacy statuses/descriptions.

    This is restoration, not a new semantic approval. Conditional SQL checks
    the current row; set_committed_value prevents ORM revalidation of history.
    """
    from fastapi import HTTPException
    from sqlalchemy import update
    from sqlalchemy.orm.attributes import set_committed_value

    values = {
        "generated_text": state.get("generated_text"),
        "edited_text": state.get("edited_text"),
        "status": str(state.get("status") or "pending_review"),
    }
    model = type(entry)
    with db.no_autoflush:
        result = await db.execute(
            update(model)
            .where(
                model.id == entry.id,
                model.job_id == entry.job_id,
                model.status == entry.status,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="Figure changed; cannot restore stale history")
    for field, value in values.items():
        set_committed_value(entry, field, value)
