from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class BBoxModel:
    l: float
    t: float
    r: float
    b: float

    def to_dict(self) -> dict[str, float]:
        return {
            "l": float(self.l),
            "t": float(self.t),
            "r": float(self.r),
            "b": float(self.b),
        }


@dataclass(slots=True)
class BlockModel:
    review_id: str
    page: int
    order: int
    role: str
    text: str = ""
    native_text_candidate: str = ""
    ocr_text_candidate: str | None = None
    llm_text_candidate: str | None = None
    resolved_text: str | None = None
    resolution_source: str | None = None
    resolution_reason: str | None = None
    level: int | None = None
    bbox: BBoxModel | None = None
    semantic_text_hint: str | None = None
    semantic_issue_type: str | None = None
    semantic_resolved_kind: str | None = None
    semantic_blocking: bool = False
    provenance: str = "legacy_structure"
    confidence: float = 0.5
    source_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.bbox is not None:
            data["bbox"] = self.bbox.to_dict()
        return data


@dataclass(slots=True)
class TableCellModel:
    row: int
    col: int
    text: str = ""
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    column_header: bool = False
    row_header: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TableModel:
    table_review_id: str
    page: int
    order: int
    bbox: BBoxModel | None = None
    num_rows: int = 0
    num_cols: int = 0
    text_excerpt: str = ""
    header_rows: list[int] = field(default_factory=list)
    row_header_columns: list[int] = field(default_factory=list)
    cells: list[TableCellModel] = field(default_factory=list)
    provenance: str = "legacy_structure"
    confidence: float = 0.5
    source_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.bbox is not None:
            data["bbox"] = self.bbox.to_dict()
        return data


@dataclass(slots=True)
class FieldModel:
    field_review_id: str
    page: int
    order: int
    field_type: str
    field_name: str = ""
    accessible_name: str = ""
    value_text: str = ""
    resolved_accessible_name: str | None = None
    resolution_source: str | None = None
    resolution_reason: str | None = None
    bbox: BBoxModel | None = None
    provenance: str = "pdf_widgets"
    confidence: float = 0.5
    label_quality: str = "missing"
    source_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.bbox is not None:
            data["bbox"] = self.bbox.to_dict()
        return data


@dataclass(slots=True)
class PageModel:
    page_number: int
    blocks: list[BlockModel] = field(default_factory=list)
    tables: list[TableModel] = field(default_factory=list)
    fields: list[FieldModel] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "blocks": [block.to_dict() for block in self.blocks],
            "tables": [table.to_dict() for table in self.tables],
            "fields": [field.to_dict() for field in self.fields],
        }


@dataclass(slots=True)
class DocumentModel:
    title: str = ""
    pages: list[PageModel] = field(default_factory=list)
    provenance: str = "legacy_structure"

    def page(self, page_number: int) -> PageModel | None:
        for page in self.pages:
            if page.page_number == page_number:
                return page
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "provenance": self.provenance,
            "pages": [page.to_dict() for page in self.pages],
        }
