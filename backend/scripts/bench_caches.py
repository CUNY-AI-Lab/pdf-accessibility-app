"""Caches for benchmark runs of the pipeline, installed in the bench process only.

- A docling-serve response cache: a page whose PDF bytes, request options
  and Docling version were converted before is answered from disk.
- A cassette for model calls: ``record`` calls the model and keeps each
  response, ``replay`` answers a request it has from the cassette and calls
  the model for one it lacks, ``strict`` answers only from the cassette and
  fails a request it lacks without calling anything.
- A ledger of what one page cost: cache use, model requests, tokens and cost
  as ``LlmUsageRecorder`` counts them.

Both caches replace a function of the app in this process (``pipeline_bench.py``
installs them); the app and the runner never import this module.
"""

from __future__ import annotations

import gzip
import hashlib
import inspect
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from app.pipeline import structure
from app.services import llm_client

CASSETTE_MODES = ("record", "replay", "strict")


@dataclass
class PageLedger:
    """What one page's pipeline run used beyond its own process."""

    docling: list[dict] = field(default_factory=list)
    cassette: dict[str, int] = field(
        default_factory=lambda: {"replayed": 0, "recorded": 0, "missing": 0}
    )
    usage: llm_client.LlmUsageRecorder = field(default_factory=llm_client.LlmUsageRecorder)

    def as_dict(self) -> dict:
        return {"docling": self.docling, "cassette": self.cassette, "llm_usage": asdict(self.usage)}


ledger = PageLedger()
# docling-serve request options a bench run adds to the app's own
# (``install_docling_options``); part of the docling cache key when set.
extra_docling_options: dict[str, str] = {}


def new_page() -> PageLedger:
    global ledger
    ledger = PageLedger()
    return ledger


def digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def write_atomically(path: Path, data: bytes) -> None:
    """Several bench processes share a cache; a reader sees a whole file or none."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(handle, "wb") as file:
        file.write(data)
    os.replace(temporary, path)


def docling_cache_key(pdf_bytes: bytes, options: dict, versions: dict) -> str:
    return digest(
        json.dumps(
            {
                "pdf": digest(pdf_bytes),
                "options": options,
                "docling": versions.get("docling"),
                "docling-serve": versions.get("docling-serve"),
            },
            sort_keys=True,
        )
    )


def install_docling_cache(directory: Path) -> None:
    """Answer docling-serve conversions from ``directory`` when the same PDF
    bytes went through the same request with the same Docling version. The
    request's options are written in ``_convert_via_docling_serve``, so its
    source is part of the key."""
    convert = structure._convert_via_docling_serve
    code = digest(inspect.getsource(convert))
    versions: dict[str, dict] = {}

    async def cached_convert(
        pdf_path: Path,
        job_dir: Path,
        base_url: str,
        timeout: int,
        ocr_engine: str = "rapidocr",
        token: str = "",
        include_figure_images: bool = True,
    ):
        if base_url not in versions:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            response = httpx.get(f"{base_url.rstrip('/')}/version", headers=headers, timeout=30)
            response.raise_for_status()
            versions[base_url] = response.json()
        options = {
            "ocr_engine": ocr_engine,
            "include_figure_images": include_figure_images,
            "code": code,
        }
        if extra_docling_options:
            options["extra"] = dict(sorted(extra_docling_options.items()))
        key = docling_cache_key(pdf_path.read_bytes(), options, versions[base_url])
        path = directory / f"{key}.json.gz"
        started = time.monotonic()
        if path.exists():
            doc_dict = json.loads(gzip.decompress(path.read_bytes()))
            figures = (
                structure._save_docling_figures(doc_dict, job_dir / "figures")
                if include_figure_images
                else []
            )
            ledger.docling.append(
                {"cache": "hit", "key": key, "seconds": round(time.monotonic() - started, 2)}
            )
            return doc_dict, figures
        doc_dict, figures = await convert(
            pdf_path, job_dir, base_url, timeout, ocr_engine, token, include_figure_images
        )
        write_atomically(path, gzip.compress(json.dumps(doc_dict).encode(), compresslevel=6))
        ledger.docling.append(
            {"cache": "miss", "key": key, "seconds": round(time.monotonic() - started, 2)}
        )
        return doc_dict, figures

    structure._convert_via_docling_serve = cached_convert


def install_docling_options(options: dict[str, str]) -> None:
    """Add ``options`` to every conversion request the app sends docling-serve
    (such as ``table_cell_matching=false``), overriding the app's value for
    the same field."""
    if not options:
        return
    extra_docling_options.update(options)
    request = structure._docling_serve_request

    async def request_with_options(client, method, url, **kwargs):
        if method.upper() == "POST" and "/v1/convert/" in url and "data" in kwargs:
            kwargs["data"] = {**kwargs["data"], **extra_docling_options}
        return await request(client, method, url, **kwargs)

    structure._docling_serve_request = request_with_options


class CassetteMiss(RuntimeError):
    """A model request the cassette has no answer for, in strict mode."""


def cassette_key(model: str, messages: list[dict], kwargs: dict) -> str:
    request = {"model": model, "messages": messages, **kwargs}
    return digest(json.dumps(request, sort_keys=True, default=str))


def count_usage(payload: dict) -> None:
    """Count a response's usage on the page ledger, as the app's recorder reads it."""
    token = llm_client._ACTIVE_USAGE_RECORDER.set(ledger.usage)
    try:
        llm_client._record_response_usage(payload)
    finally:
        llm_client._ACTIVE_USAGE_RECORDER.reset(token)


def install_llm_cassette(directory: Path | None, mode: str = "replay") -> None:
    """Count every model response on the page ledger and, with a cassette
    ``directory``, record or replay ``LlmClient._do_chat_completion`` in this
    process."""
    if mode not in CASSETTE_MODES:
        raise ValueError(f"cassette mode must be one of {CASSETTE_MODES}")
    call = llm_client.LlmClient._do_chat_completion

    async def taped_call(self, messages: list[dict], **kwargs) -> dict:
        if directory is None:
            payload = await call(self, messages, **kwargs)
            count_usage(payload)
            return payload
        key = cassette_key(self.model, messages, kwargs)
        path = directory / f"{key}.json"
        if mode != "record" and path.exists():
            payload = json.loads(path.read_text())["response"]
            # The job's own recorder counts a replayed response as it would a live one.
            llm_client._record_response_usage(payload)
            ledger.cassette["replayed"] += 1
        elif mode == "strict":
            ledger.cassette["missing"] += 1
            raise CassetteMiss(f"no recorded response for request {key[:12]} ({self.model})")
        else:
            payload = await call(self, messages, **kwargs)
            recorded = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            entry = {"model": self.model, "recorded": recorded, "response": payload}
            write_atomically(path, json.dumps(entry).encode())
            ledger.cassette["recorded"] += 1
        count_usage(payload)
        return payload

    llm_client.LlmClient._do_chat_completion = taped_call
