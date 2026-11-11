"""Refusals shared by review state, revisions, and runner actions."""


class ReviewRefusal(Exception):
    """A saved review action can no longer be applied to the current document."""

    def __init__(self, *, code: int, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail
