"""What the Worker serves for a job, rendered by v1's own response code.

The Worker applies review statuses changed since the last task (keep and
resolve) on top of these views; it does not re-implement them.
"""

import asyncio
import json
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.documents import visible_review_tasks
from app.api.jobs import job_to_response
from app.models import AltTextEntry, AppliedChange, Job, ReviewTask
from app.schemas import ValidationReportResponse
from app.services.applied_changes import change_to_response_payload, list_figure_changes
from app.services.html_report import render_batch_parts, render_html_report
from app.services.pdf_preview import render_page_png_bytes

logger = logging.getLogger(__name__)

OUTPUT_READY_STATUSES = frozenset({"complete", "manual_remediation"})
PREVIEW_PAGES_PER_TASK = 3
PREVIEW_TARGET_KEYS = (
    "poor_links",
    "broken_links",
    "table_review_targets",
    "targets",
    "field_review_targets",
)


@dataclass
class RenderedFile:
    name: str
    body: bytes | Path
    content_type: str


@dataclass
class RenderedViews:
    job: dict[str, Any]
    files: list[RenderedFile] = field(default_factory=list)
    figures: list[int] = field(default_factory=list)
    previews: list[int] = field(default_factory=list)


def preview_pages_for_task(metadata: dict[str, Any]) -> list[int]:
    """The pages the review screen previews for one task.

    Mirrors ``previewPagesForTask`` in frontend/src/pages/reviewHelpers.ts.
    """
    pages: set[int] = set()
    listed = metadata.get("pages_to_check")
    if isinstance(listed, list):
        for item in listed:
            try:
                page = float(item)
            except (TypeError, ValueError):
                continue
            if page > 0 and page.is_integer():
                pages.add(int(page))
    for key in PREVIEW_TARGET_KEYS:
        entries = metadata.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            page = entry.get("page") if isinstance(entry, dict) else None
            if isinstance(page, int) and not isinstance(page, bool) and page > 0:
                pages.add(page)
    return sorted(pages)[:PREVIEW_PAGES_PER_TASK]


def _json_file(name: str, value: Any) -> RenderedFile:
    return RenderedFile(name, json.dumps(value).encode(), "application/json")


async def render_views(db: AsyncSession, job_id: str) -> RenderedViews:
    job = (
        await db.execute(select(Job).options(selectinload(Job.steps)).where(Job.id == job_id))
    ).scalar_one()
    rendered = RenderedViews(job=job_to_response(job).model_dump(mode="json"))
    if job.status not in OUTPUT_READY_STATUSES:
        return rendered

    alt_texts = list(
        (
            await db.execute(
                select(AltTextEntry)
                .where(AltTextEntry.job_id == job_id)
                .order_by(AltTextEntry.figure_index)
            )
        ).scalars()
    )
    review_tasks = list(
        (
            await db.execute(
                select(ReviewTask)
                .where(ReviewTask.job_id == job_id)
                .order_by(ReviewTask.blocking.desc(), ReviewTask.created_at.asc())
            )
        ).scalars()
    )
    reviewable_changes = list(
        (
            await db.execute(
                select(AppliedChange)
                .where(AppliedChange.job_id == job_id, AppliedChange.reviewable.is_(True))
                .order_by(AppliedChange.created_at.asc())
            )
        ).scalars()
    )
    figure_changes = await list_figure_changes(db=db, job_id=job_id)
    visible_tasks = [task.model_dump(mode="json") for task in visible_review_tasks(review_tasks)]

    rendered.files += [
        _json_file("views/review-tasks.json", visible_tasks),
        _json_file(
            "views/applied-changes.json",
            [change_to_response_payload(change) for change in reviewable_changes],
        ),
        _json_file(
            "views/figure-changes.json",
            [change_to_response_payload(change) for change in figure_changes],
        ),
    ]

    if job.validation_json:
        validation = json.loads(job.validation_json)
        rendered.files += [
            _json_file(
                "views/report.json",
                ValidationReportResponse.model_validate(validation).model_dump(mode="json"),
            ),
            RenderedFile(
                "report.html",
                render_html_report(job, validation, alt_texts, review_tasks).encode(),
                "text/html; charset=utf-8",
            ),
            _json_file(
                "views/batch-parts.json",
                render_batch_parts(job, validation, alt_texts, review_tasks),
            ),
        ]

    if job.output_path and Path(job.output_path).exists():
        rendered.files.append(RenderedFile("output.pdf", Path(job.output_path), "application/pdf"))

    for entry in alt_texts:
        image = Path(entry.image_path) if entry.image_path else None
        if image is None or not image.exists():
            continue
        content_type = mimetypes.guess_type(image.name)[0] or "image/png"
        rendered.files.append(RenderedFile(f"figures/{entry.figure_index}", image, content_type))
        rendered.figures.append(entry.figure_index)

    preview_source = Path(job.output_path or job.input_path)
    pages = sorted(
        {page for task in visible_tasks for page in preview_pages_for_task(task["metadata"])}
    )
    for page in pages:
        if job.page_count and page > job.page_count:
            continue
        try:
            png = await asyncio.to_thread(render_page_png_bytes, preview_source, page)
        except Exception:
            logger.exception("Could not render page %s of job %s for review", page, job_id)
            continue
        rendered.files.append(RenderedFile(f"previews/{page}", png, "image/png"))
        rendered.previews.append(page)
    return rendered
