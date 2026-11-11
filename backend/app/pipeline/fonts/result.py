"""Serializable state for font remediation planning and lane results."""

from dataclasses import dataclass, field, fields


@dataclass
class FontRemediation:
    attempted: bool = False
    eligible: bool = False
    applied: bool = False
    strategy: str = "safe_first_non_destructive"
    document_features: dict[str, int | bool] = field(default_factory=dict)
    unicode_gate: dict[str, object] | None = None
    preflight_diagnostics: dict[str, object] | None = None
    postflight_diagnostics: dict[str, object] | None = None
    first_pass_errors: int = 0
    first_pass_warnings: int = 0
    second_pass_errors: int | None = None
    second_pass_warnings: int | None = None
    lanes_planned: list[str] = field(default_factory=list)
    lanes_skipped: list[str] = field(default_factory=list)
    lane_results: list[dict[str, object]] = field(default_factory=list)
    selected_lane: str | None = None
    selected_lanes: list[str] = field(default_factory=list)
    error: str | None = None
    ocr_message: str = ""
    ocr_skipped: bool = False
    selected_preprocessed_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Keep the twenty stored keys and the existing conditional path key."""
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "selected_preprocessed_path"
            or self.selected_preprocessed_path is not None
        }
