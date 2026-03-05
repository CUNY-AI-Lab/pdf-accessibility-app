from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class JobStepResponse(BaseModel):
    step_name: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


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
    steps: list[JobStepResponse] = []


class JobCreateResponse(BaseModel):
    jobs: list[JobResponse]


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int


class AltTextResponse(BaseModel):
    id: int
    figure_index: int
    image_url: str
    generated_text: str | None = None
    edited_text: str | None = None
    status: str


class AltTextUpdateRequest(BaseModel):
    edited_text: str | None = None
    status: Literal["pending_review", "approved", "rejected"] | None = None


class ValidationViolation(BaseModel):
    rule_id: str
    description: str
    severity: str
    location: str | None = None
    count: int = 1


class ValidationReportResponse(BaseModel):
    compliant: bool
    violations: list[ValidationViolation] = []
    summary: dict[str, int] = {}


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
