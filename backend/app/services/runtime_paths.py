from __future__ import annotations

import os
import shutil
from pathlib import Path

COMMON_BINARY_DIRS = (
    "/usr/local/bin",
    "/usr/local/sbin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
)


def _configured_binary_dirs() -> list[str]:
    from app.config import get_settings

    configured = str(get_settings().binary_search_dirs or "").strip()
    if not configured:
        return []
    dirs: list[str] = []
    for entry in configured.split(","):
        value = entry.strip()
        if value:
            dirs.append(value)
    return dirs


def _path_entries() -> list[str]:
    path_entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    for directory in _configured_binary_dirs():
        if directory not in path_entries:
            path_entries.append(directory)
    for directory in COMMON_BINARY_DIRS:
        if directory not in path_entries:
            path_entries.append(directory)
    return path_entries


def _resolve_explicit_binary(explicit: str | None) -> str | None:
    value = str(explicit or "").strip()
    if not value:
        return None

    candidate = Path(value).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate.resolve())

    resolved = shutil.which(value, path=os.pathsep.join(_path_entries()))
    if resolved:
        return resolved
    return None


def resolve_binary(binary: str, *, explicit: str | None = None) -> str | None:
    explicit_path = _resolve_explicit_binary(explicit)
    if explicit_path:
        return explicit_path

    resolved = shutil.which(binary, path=os.pathsep.join(_path_entries()))
    if resolved:
        return resolved

    for directory in _path_entries():
        candidate = Path(directory) / binary
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def enriched_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(_path_entries())
    return env
