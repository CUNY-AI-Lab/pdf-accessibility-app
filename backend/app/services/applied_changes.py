from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppliedChange, Job


def parse_json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def change_to_response_payload(change: AppliedChange) -> dict[str, Any]:
    return {
        "id": change.id,
        "change_type": change.change_type,
        "title": change.title,
        "detail": change.detail,
        "importance": change.importance,
        "review_status": change.review_status,
        "reviewable": bool(change.reviewable),
        "metadata": parse_json_dict(change.metadata_json),
        "before": parse_json_dict(change.before_json),
        "after": parse_json_dict(change.after_json),
    }


async def list_pending_reviewable_changes(
    *,
    db: AsyncSession,
    job_id: str,
) -> list[AppliedChange]:
    result = await db.execute(
        select(AppliedChange)
        .where(
            AppliedChange.job_id == job_id,
            AppliedChange.review_status == "pending_review",
            AppliedChange.reviewable.is_(True),
        )
        .order_by(AppliedChange.created_at.asc())
    )
    return list(result.scalars().all())


async def list_figure_changes(
    *,
    db: AsyncSession,
    job_id: str,
) -> list[AppliedChange]:
    """Return the latest applied change per figure, including auto-kept ones."""
    all_result = await db.execute(
        select(AppliedChange)
        .where(
            AppliedChange.job_id == job_id,
            AppliedChange.change_type == "figure_semantics",
        )
        .order_by(AppliedChange.created_at.desc())
    )
    all_changes = list(all_result.scalars().all())
    # Keep only the most recent change per figure_index
    seen: set[int] = set()
    latest: list[AppliedChange] = []
    for change in all_changes:
        metadata = parse_json_dict(change.metadata_json)
        fig_idx = metadata.get("figure_index")
        if fig_idx is not None and fig_idx not in seen:
            seen.add(fig_idx)
            latest.append(change)
    latest.sort(key=lambda c: c.created_at)
    return latest


async def add_applied_change(
    *,
    db: AsyncSession,
    job: Job,
    change_type: str,
    title: str,
    detail: str,
    importance: str = "medium",
    reviewable: bool = True,
    metadata: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    undo_payload: dict[str, Any] | None = None,
) -> AppliedChange:
    change = AppliedChange(
        job_id=job.id,
        change_type=change_type,
        title=title,
        detail=detail,
        importance=importance,
        review_status="pending_review" if reviewable else "kept",
        reviewable=reviewable,
        metadata_json=json.dumps(metadata or {}),
        before_json=json.dumps(before or {}),
        after_json=json.dumps(after or {}),
        undo_payload_json=json.dumps(undo_payload or {}),
    )
    db.add(change)
    await db.flush()
    return change
