from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SemanticUnitType = Literal["text_block", "table", "form_field", "figure", "toc_group"]


@dataclass(slots=True)
class SemanticUnit:
    unit_id: str
    unit_type: SemanticUnitType
    page: int
    accessibility_goal: str
    bbox: dict[str, float] | None = None
    native_text_candidate: str = ""
    ocr_text_candidate: str | None = None
    nearby_context: list[dict[str, Any]] = field(default_factory=list)
    structure_context: list[dict[str, Any]] = field(default_factory=list)
    current_semantics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_prompt_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not self.bbox:
            data.pop("bbox", None)
        return data


@dataclass(slots=True)
class SemanticDecision:
    unit_id: str
    unit_type: SemanticUnitType
    summary: str
    confidence: str
    confidence_score: float
    suggested_action: str
    reason: str
    chosen_source: str | None = None
    resolved_text: str | None = None
    issue_type: str | None = None
    should_block_accessibility: bool = False
    header_rows: list[int] = field(default_factory=list)
    row_header_columns: list[int] = field(default_factory=list)
    accessible_label: str | None = None
    alt_text: str | None = None
    resolved_kind: str | None = None
    is_decorative: bool = False
    is_toc: bool = False
    entry_indexes: list[int] = field(default_factory=list)
    entry_types: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
