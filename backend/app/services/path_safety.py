"""Shared path-safety utilities for API route handlers."""

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from app.config import get_settings


def validate_path_within_allowed_roots(candidate: Path) -> Path:
    """Resolve *candidate* and verify it falls within the configured data directories.

    Returns the resolved ``Path`` on success, or raises ``HTTPException(403)``
    if the path would escape the allowed roots (upload_dir, processing_dir,
    output_dir).
    """
    settings = get_settings()
    resolved = candidate.resolve()
    allowed_roots = (
        settings.upload_dir.resolve(),
        settings.processing_dir.resolve(),
        settings.output_dir.resolve(),
    )
    if not any(
        resolved == root or str(resolved).startswith(str(root) + "/")
        for root in allowed_roots
    ):
        raise HTTPException(status_code=403, detail="Access denied")
    return resolved


def safe_filename(original_filename: str) -> str:
    """Strip directory components from a user-supplied filename."""
    normalized = str(original_filename or "").replace("\\", "/")
    name = Path(normalized).name.strip()
    if name in {"", ".", ".."}:
        return "document.pdf"
    return name


def attachment_content_disposition(filename: str) -> str:
    """Build an ASCII-safe attachment Content-Disposition header value."""
    safe_name = safe_filename(filename)
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_name).strip("._-")
    fallback = re.sub(r"_+(\.[A-Za-z0-9]+)$", r"\1", fallback)
    if not fallback:
        fallback = "download"
    encoded = quote(safe_name, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
