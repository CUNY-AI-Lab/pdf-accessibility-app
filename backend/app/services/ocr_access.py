"""A job-local refusal receipt shared by OCR page processes and their parent.

Access transitions use ModelAccess.refuse in llm_client::
    available -> refused (401/403 or nonretryable 429)
    refused -> refused (cannot clear within the job)

Empty status files share refusal across page processes; the parent latches it
before accepting OCR output. A new invocation gets a private receipt directory.
"""

import os
from pathlib import Path

from app.services.llm_client import ModelAccess, ModelAccessRefused


class OcrJobAccess(ModelAccess):
    def __init__(self, directory: Path | None):
        super().__init__()
        self.directory = directory

    @classmethod
    def from_environment(cls) -> "OcrJobAccess":
        directory = os.environ.get("CAIL_OCR_ACCESS_DIR")
        return cls(Path(directory) if directory else None)

    @property
    def refusal(self) -> ModelAccessRefused | None:
        if self.directory is not None:
            for status in (401, 403, 429):
                if (self.directory / str(status)).exists():
                    return super().refuse(ModelAccessRefused(status, str(status), ""))
        return super().refusal

    def refuse(self, reason: ModelAccessRefused) -> ModelAccessRefused:
        reason = super().refuse(reason)
        if self.directory is not None:
            # An empty file is an atomic receipt: readers cannot see partial JSON.
            # Only a status is stored, never headers, credentials, or page text.
            (self.directory / str(reason.status_code)).touch(exist_ok=True)
        return reason

    def check(self) -> None:
        if refusal := self.refusal:
            raise refusal
