from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class JobStepResponse(BaseModel):
    step_name: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


class JobResponse(BaseModel):
    id: str
    filename: str
    original_filename: str
    status: str
    classification: str | None = None
    page_count: int | None = None
    file_size_bytes: int | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    steps: list[JobStepResponse] = Field(default_factory=list)


class JobCreateResponse(BaseModel):
    jobs: list[JobResponse]


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int


class ReviewTaskResponse(BaseModel):
    id: int
    task_type: str
    title: str
    detail: str
    severity: Literal["high", "medium", "low"]
    blocking: bool
    status: str
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AppliedChangeResponse(BaseModel):
    id: int
    change_type: str
    title: str
    detail: str
    importance: Literal["high", "medium", "low"]
    review_status: Literal["pending_review", "kept", "undone"]
    reviewable: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)


class ReviewFeedbackRequest(BaseModel):
    feedback: str | None = None


class AppliedChangeActionResponse(BaseModel):
    status: str
    message: str
    job_status: str


class ValidationViolation(BaseModel):
    rule_id: str
    description: str
    severity: str
    location: str | None = None
    count: int = 1
    category: str | None = None
    fix_hint: str | None = None
    remediation_status: str | None = None


class ValidationChange(BaseModel):
    rule_id: str
    description: str
    severity: str
    location: str | None = None
    category: str | None = None
    fix_hint: str | None = None
    baseline_count: int = 0
    post_count: int = 0
    remediation_status: str


class ValidationReportResponse(BaseModel):
    compliant: bool
    profile: str | None = None
    standard: str | None = None
    validator: str | None = None
    generated_at: str | None = None
    baseline: dict[str, Any] = Field(default_factory=dict)
    violations: list[ValidationViolation] = Field(default_factory=list)
    changes: list[ValidationChange] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    remediation: dict[str, Any] = Field(default_factory=dict)
    fidelity: dict[str, Any] = Field(default_factory=dict)
    tagging: dict[str, Any] = Field(default_factory=dict)
    claims: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
