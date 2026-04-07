from __future__ import annotations

import re
import subprocess
from typing import Any
from urllib.parse import urlparse

from app.config import get_settings

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
GPU_LIBRARY_MARKERS = (
    "agxmetal",
    "metalperformanceshaders",
    "/com.apple.metal/",
    "mpscore.framework",
    "mpsndarray.framework",
    "mlx.metallib",
)


def _host_and_port(url: str) -> tuple[str | None, int | None]:
    parsed = urlparse((url or "").strip())
    host = parsed.hostname
    port = parsed.port
    if port is None and parsed.scheme == "http":
        port = 80
    elif port is None and parsed.scheme == "https":
        port = 443
    return host, port


def _extract_env_value(command_line: str, env_name: str) -> str | None:
    pattern = re.compile(rf"(?:^|\s){re.escape(env_name)}=([^\s]+)")
    match = pattern.search(command_line or "")
    if not match:
        return None
    return match.group(1)


def _parse_listener_pid(lsof_output: str) -> int | None:
    for line in (lsof_output or "").splitlines():
        if not line.startswith("p"):
            continue
        try:
            return int(line[1:])
        except ValueError:
            return None
    return None


def _command_output(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _gpu_libraries_loaded(lsof_output: str) -> bool:
    normalized = (lsof_output or "").lower()
    return any(marker in normalized for marker in GPU_LIBRARY_MARKERS)


def _inspect_local_docling_serve(port: int) -> dict[str, Any]:
    listener_output = _command_output(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"]
    )
    pid = _parse_listener_pid(listener_output)
    if pid is None:
        return {
            "listener_found": False,
            "process_pid": None,
            "device": None,
            "gpu_libraries_loaded": False,
        }

    ps_output = _command_output(["ps", "eww", "-p", str(pid)])
    process_lsof_output = _command_output(["lsof", "-p", str(pid)])
    return {
        "listener_found": True,
        "process_pid": pid,
        "device": _extract_env_value(ps_output, "DOCLING_DEVICE"),
        "gpu_libraries_loaded": _gpu_libraries_loaded(process_lsof_output),
    }


def collect_runtime_diagnostics(settings: Any | None = None) -> dict[str, Any]:
    settings = settings or get_settings()

    llm_host, _ = _host_and_port(settings.llm_base_url)
    llm_is_local = (llm_host or "").lower() in LOCAL_HOSTS

    docling_host, docling_port = _host_and_port(settings.docling_serve_url)
    docling_is_local = bool(docling_host) and docling_host.lower() in LOCAL_HOSTS

    docling: dict[str, Any] = {
        "configured": bool(settings.docling_serve_url),
        "serve_url": settings.docling_serve_url or None,
        "host": docling_host,
        "port": docling_port,
        "local": docling_is_local,
        "ocr_engine": settings.docling_serve_ocr_engine,
        "listener_found": False,
        "process_pid": None,
        "device": None,
        "gpu_libraries_loaded": False,
    }

    if docling_is_local and docling_port is not None:
        docling.update(_inspect_local_docling_serve(docling_port))

    structure_runtime = "local docling"
    if docling["configured"]:
        if docling["local"]:
            device = docling.get("device") or "unknown"
            structure_runtime = f"docling-serve ({device})"
        else:
            structure_runtime = "docling-serve (remote)"

    return {
        "llm": {
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "uses_remote_api_endpoint": not llm_is_local,
        },
        "docling": docling,
        "pipeline": {
            "structure_runtime": structure_runtime,
            "tagging_runtime": "cpu",
            "notes": [
                "The structure step follows DOCLING_SERVE_URL when configured.",
                "The tagging/writer step is local pikepdf/docling-parse work and remains CPU-bound.",
            ],
        },
    }
