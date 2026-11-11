import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def generate_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("idx_jobs_status", "status"),
        Index("idx_jobs_created_at", "created_at"),
        Index("idx_jobs_owner_session_hash", "owner_session_hash"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_uuid)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    original_filename: Mapped[str] = mapped_column(String, nullable=False)
    owner_session_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    classification: Mapped[str | None] = mapped_column(String, nullable=True)
    ocr_language: Mapped[str | None] = mapped_column(String, nullable=True)
    input_path: Mapped[str] = mapped_column(String, nullable=False)
    output_path: Mapped[str | None] = mapped_column(String, nullable=True)
    structure_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    fidelity_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    steps: Mapped[list["JobStep"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    alt_texts: Mapped[list["AltTextEntry"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    review_tasks: Mapped[list["ReviewTask"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    applied_changes: Mapped[list["AppliedChange"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class JobStep(Base):
    __tablename__ = "job_steps"
    __table_args__ = (
        Index("idx_job_steps_job_id", "job_id"),
        UniqueConstraint("job_id", "step_name", name="uq_job_steps_job_step"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    step_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped["Job"] = relationship(back_populates="steps")


class AltTextEntry(Base):
    __tablename__ = "alt_texts"
    __table_args__ = (
        Index("idx_alt_texts_job_id", "job_id"),
        UniqueConstraint("job_id", "figure_index", name="uq_alt_texts_job_figure"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    figure_index: Mapped[int] = mapped_column(Integer, nullable=False)
    image_path: Mapped[str] = mapped_column(String, nullable=False)
    generated_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending_review")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    job: Mapped["Job"] = relationship(back_populates="alt_texts")


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        Index("idx_review_tasks_job_id", "job_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False, default="medium")
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending_review")
    source: Mapped[str] = mapped_column(String, nullable=False, default="fidelity")
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    job: Mapped["Job"] = relationship(back_populates="review_tasks")


class AppliedChange(Base):
    __tablename__ = "applied_changes"
    __table_args__ = (
        Index("idx_applied_changes_job_id", "job_id"),
        Index("idx_applied_changes_review_status", "review_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    change_type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[str] = mapped_column(String, nullable=False, default="medium")
    review_status: Mapped[str] = mapped_column(String, nullable=False, default="pending_review")
    reviewable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    undo_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), onupdate=utcnow
    )

    job: Mapped["Job"] = relationship(back_populates="applied_changes")


class ReviewRevision(Base):
    """Local command/output journal, archived with the runner's SQLite state.

    New tables are additive: runner 7764be7 ignores this journal, and the
    deployed Worker continues receiving exactly its existing view contract.
    """

    __tablename__ = "review_revisions"
    __table_args__ = (UniqueConstraint("job_id", "command_id"),)

    job_id: Mapped[str] = mapped_column(String, ForeignKey("jobs.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    command_id: Mapped[str] = mapped_column(String, nullable=False)
    phase: Mapped[str] = mapped_column(String, nullable=False)
    command_json: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[str] = mapped_column(Text, nullable=False)
    edits_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ORM boundaries cover every in-process writer without parsing legacy rows on read.
# Conditional SQL writers (review CAS and snapshot compensation) guard explicitly.
def _guard_persisted_status(_mapper, _connection, target):
    from sqlalchemy import inspect

    from app.services.figure_state import FIGURE_TRANSITIONS, figure_status, set_figure_status
    from app.services.job_transitions import JOB_TRANSITIONS, STEP_TRANSITIONS, guard_transition
    from app.services.review_revisions import REVISION_TRANSITIONS
    from app.services.review_state import CHANGE_TRANSITIONS, REVIEW_TRANSITIONS

    contracts = {
        Job: ("status", JOB_TRANSITIONS),
        JobStep: ("status", STEP_TRANSITIONS),
        AltTextEntry: ("status", FIGURE_TRANSITIONS),
        AppliedChange: ("review_status", CHANGE_TRANSITIONS),
        ReviewTask: ("status", REVIEW_TRANSITIONS),
        ReviewRevision: ("phase", REVISION_TRANSITIONS),
    }
    field, transitions = contracts[type(target)]
    history = inspect(target).attrs[field].history
    if not history.has_changes():
        return
    current = history.deleted[0] if history.deleted else getattr(target, field)
    value = getattr(target, field)
    if value is None:
        return
    if isinstance(target, AltTextEntry):
        current, value = figure_status(current), figure_status(value)
        set_figure_status(target, value)
    guard_transition(current, value, transitions, entity=target.__tablename__)


for _model in (Job, JobStep, AltTextEntry, AppliedChange, ReviewTask, ReviewRevision):
    event.listen(_model, "before_update", _guard_persisted_status)
    event.listen(_model, "before_insert", _guard_persisted_status)
