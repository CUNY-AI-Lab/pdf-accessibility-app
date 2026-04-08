from __future__ import annotations

import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from app.services.intelligence_llm_utils import pdf_file_bytes


class GeminiExperimentConfigError(RuntimeError):
    pass


def parse_page_spec(page_spec: str | None) -> list[int] | None:
    if page_spec is None:
        return None
    text = page_spec.strip()
    if not text:
        return None

    pages: set[int] = set()
    for chunk in text.split(","):
        part = chunk.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text.strip())
            end = int(end_text.strip())
            if start <= 0 or end <= 0:
                raise ValueError("Page numbers must be positive")
            if end < start:
                raise ValueError("Page ranges must be ascending")
            pages.update(range(start, end + 1))
            continue
        page = int(part)
        if page <= 0:
            raise ValueError("Page numbers must be positive")
        pages.add(page)
    return sorted(pages)


def find_gemini_api_key(*, env_file: Path | None = None) -> str:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value

    if env_file is not None and env_file.exists():
        for line in env_file.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            for prefix in ("GEMINI_API_KEY=", "GOOGLE_API_KEY="):
                if stripped.startswith(prefix):
                    value = stripped.split("=", 1)[1].strip().strip("'\"")
                    if value:
                        return value

    raise GeminiExperimentConfigError(
        "No Gemini API key found. Set GEMINI_API_KEY or GOOGLE_API_KEY, "
        "or pass an env file containing one."
    )


def make_pdf_subset_io(pdf_path: Path, *, page_numbers: list[int] | None) -> BytesIO:
    pdf_bytes = pdf_file_bytes(pdf_path, page_numbers)
    stream = BytesIO(pdf_bytes)
    stream.name = pdf_path.name
    stream.seek(0)
    return stream


@dataclass(slots=True)
class GeminiCachedPromptResult:
    prompt: str
    text: str
    usage_metadata: dict[str, object]
