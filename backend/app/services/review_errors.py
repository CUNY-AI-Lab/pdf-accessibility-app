"""Refusals shared by review state, revisions, and runner actions."""


class ReviewRefusal(Exception):
    """A saved review action can no longer be applied to the current document."""

    def __init__(
        self, *, code: int, detail: str, service_failure: dict[str, object] | None = None,
    ):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        # {"error": class name, "status": HTTP status or None} when no model
        # answered (llm_client.model_service_failure); a later retry may work.
        self.service_failure = service_failure
