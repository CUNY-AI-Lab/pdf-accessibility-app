"""Progress reporting without task scheduling or subscriber ownership."""

from typing import Any, Protocol


class ProgressReporter(Protocol):
    def emit_progress(self, job_id: str, **event_data: Any) -> None: ...


class NullProgress:
    """Benchmarks run pipelines inline and read results from their database."""

    def emit_progress(self, job_id: str, **event_data: Any) -> None:
        pass
