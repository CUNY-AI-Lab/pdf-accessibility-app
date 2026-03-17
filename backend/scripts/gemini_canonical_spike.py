#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import pikepdf

from app.config import get_settings
from app.pipeline.structure import extract_structure
from app.services.intelligence_llm_utils import request_llm_json
from app.services.llm_client import make_llm_client, track_llm_usage
from app.services.pdf_preview import render_page_jpeg_data_url

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = ROOT_DIR / "data" / "benchmarks" / "deep_dive_representative_inputs_20260311"
DEFAULT_DOCS = [
    "skew.pdf",
    "Code4Lib BePress Article.pdf",
    "multipage.pdf",
    "filer-user-guide-january-2025.pdf",
]

OUTPUT_ROOT = ROOT_DIR / "data" / "experiments"

CANONICAL_BATCH_PROMPT = """You are reconstructing a PDF into a canonical accessible document model.

Goal:
- Read the provided page images and return the meaningful content in linear reading order.
- Produce a representation that would be sufficient to rebuild an accessible PDF or HTML document.

Important constraints:
- Ignore interactive PDF mechanics. We do not care about AcroForm fidelity for this experiment.
- Ignore decorative page furniture, repeated headers/footers, crop marks, and layout-only artifacts unless they carry meaning.
- Preserve headings, paragraphs, lists, tables, formulas, notes, code blocks, and meaningful figures/charts.
- Prefer faithful readable content over visual exactness.
- Do not invent text or structure that is not supported by the page images.
- When chart or figure meaning is clear, provide concise alt_text. When it is not clear, leave alt_text empty and use lower confidence.
- For tables, preserve the visible content in simple markdown when feasible. If the full table is not legible, keep a short textual summary in text and set lower confidence.
- For figure blocks, use text for visible caption/context and alt_text for the accessible description.
- Every returned page_number must match one of the requested pages exactly.
- Return blocks in reading order for each page.

Block kinds:
- heading
- paragraph
- list_item
- table
- figure
- formula
- code
- note
"""

CANONICAL_BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document_title": {"type": "string"},
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "page_number": {"type": "integer", "minimum": 1},
                    "blocks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": [
                                        "heading",
                                        "paragraph",
                                        "list_item",
                                        "table",
                                        "figure",
                                        "formula",
                                        "code",
                                        "note",
                                    ],
                                },
                                "text": {"type": "string"},
                                "level": {"type": "integer", "minimum": 1, "maximum": 6},
                                "alt_text": {"type": "string"},
                                "table_markdown": {"type": "string"},
                                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                            },
                            "required": ["kind", "text", "confidence"],
                        },
                    },
                },
                "required": ["page_number", "blocks"],
            },
        },
    },
    "required": ["document_title", "pages"],
}


def _slugify(name: str) -> str:
    cleaned = [
        char.lower() if char.isalnum() else "-"
        for char in name.strip()
    ]
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "document"


def _page_count(pdf_path: Path) -> int:
    with pikepdf.open(str(pdf_path)) as pdf:
        return len(pdf.pages)


def _render_page_parts(pdf_path: Path, page_numbers: list[int]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for page_number in page_numbers:
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": render_page_jpeg_data_url(pdf_path, page_number)},
            }
        )
    return parts


def _request_payload(filename: str, page_numbers: list[int]) -> dict[str, Any]:
    return {
        "source_filename": filename,
        "requested_pages": page_numbers,
        "output_contract": {
            "document_title": "best-effort title for the document or current section",
            "pages": [
                {
                    "page_number": 1,
                    "blocks": [
                        {
                            "kind": "heading|paragraph|list_item|table|figure|formula|code|note",
                            "text": "main readable content for the block",
                            "level": "heading level only when kind=heading",
                            "alt_text": "figure/chart description only when kind=figure",
                            "table_markdown": "simple markdown table only when kind=table",
                            "confidence": "high|medium|low",
                        }
                    ],
                }
            ],
        },
    }


def _normalize_block(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip()
    if kind not in {"heading", "paragraph", "list_item", "table", "figure", "formula", "code", "note"}:
        return None
    text = " ".join(str(raw.get("text") or "").split())
    alt_text = " ".join(str(raw.get("alt_text") or "").split())
    table_markdown = str(raw.get("table_markdown") or "").strip()
    confidence = str(raw.get("confidence") or "low").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    normalized: dict[str, Any] = {
        "kind": kind,
        "text": text,
        "confidence": confidence,
    }
    if kind == "heading":
        try:
            level = int(raw.get("level"))
        except (TypeError, ValueError):
            level = None
        if level is not None and 1 <= level <= 6:
            normalized["level"] = level
    if alt_text:
        normalized["alt_text"] = alt_text
    if table_markdown:
        normalized["table_markdown"] = table_markdown
    return normalized


def _normalize_batch(raw: dict[str, Any], expected_pages: list[int]) -> dict[str, Any]:
    expected = set(expected_pages)
    pages: list[dict[str, Any]] = []
    for item in raw.get("pages", []):
        if not isinstance(item, dict):
            continue
        try:
            page_number = int(item.get("page_number"))
        except (TypeError, ValueError):
            continue
        if page_number not in expected:
            continue
        blocks = []
        for block in item.get("blocks", []):
            normalized = _normalize_block(block)
            if normalized is not None:
                blocks.append(normalized)
        pages.append({"page_number": page_number, "blocks": blocks})
    pages.sort(key=lambda item: item["page_number"])
    return {
        "document_title": " ".join(str(raw.get("document_title") or "").split()),
        "pages": pages,
    }


def _block_counts_from_gemini(document: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for page in document.get("pages", []):
        if not isinstance(page, dict):
            continue
        for block in page.get("blocks", []):
            if not isinstance(block, dict):
                continue
            counts[str(block.get("kind") or "unknown")] += 1
    return counts


def _text_chars_from_gemini(document: dict[str, Any]) -> int:
    total = 0
    for page in document.get("pages", []):
        if not isinstance(page, dict):
            continue
        for block in page.get("blocks", []):
            if not isinstance(block, dict):
                continue
            total += len(str(block.get("text") or ""))
            total += len(str(block.get("alt_text") or ""))
            total += len(str(block.get("table_markdown") or ""))
    return total


def _structure_slice_counts(document_json: dict[str, Any], *, max_pages: int) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    text_chars = 0
    element_count = 0
    title = " ".join(str(document_json.get("title") or "").split())
    for element in document_json.get("elements", []):
        if not isinstance(element, dict):
            continue
        page = element.get("page")
        if not isinstance(page, int) or page >= max_pages:
            continue
        role = str(element.get("type") or "unknown").strip() or "unknown"
        counts[role] += 1
        text_chars += len(str(element.get("text") or ""))
        element_count += 1
    return {
        "title": title,
        "elements": element_count,
        "role_counts": dict(sorted(counts.items())),
        "text_chars": text_chars,
    }


def _excerpt_lines(document: dict[str, Any], *, limit: int = 10) -> list[str]:
    lines: list[str] = []
    for page in document.get("pages", []):
        if not isinstance(page, dict):
            continue
        page_number = int(page.get("page_number") or 0)
        for block in page.get("blocks", []):
            if not isinstance(block, dict):
                continue
            kind = str(block.get("kind") or "unknown")
            text = str(block.get("text") or "").strip()
            alt = str(block.get("alt_text") or "").strip()
            if text:
                lines.append(f"p.{page_number} {kind}: {text[:160]}")
            elif alt:
                lines.append(f"p.{page_number} {kind}: {alt[:160]}")
            else:
                lines.append(f"p.{page_number} {kind}: [empty]")
            if len(lines) >= limit:
                return lines
    return lines


def _markdown_from_gemini(document: dict[str, Any]) -> str:
    lines: list[str] = []
    title = str(document.get("document_title") or "").strip()
    if title:
        lines.append(f"# {title}")
        lines.append("")

    for page in document.get("pages", []):
        if not isinstance(page, dict):
            continue
        page_number = int(page.get("page_number") or 0)
        lines.append(f"## Page {page_number}")
        lines.append("")
        for block in page.get("blocks", []):
            if not isinstance(block, dict):
                continue
            kind = str(block.get("kind") or "")
            text = str(block.get("text") or "").strip()
            alt_text = str(block.get("alt_text") or "").strip()
            table_markdown = str(block.get("table_markdown") or "").strip()
            level = block.get("level")
            if kind == "heading":
                heading_level = min(max(int(level or 2), 1), 6)
                lines.append(f"{'#' * heading_level} {text}")
            elif kind == "list_item":
                lines.append(f"- {text}")
            elif kind == "figure":
                if text:
                    lines.append(f"Figure context: {text}")
                if alt_text:
                    lines.append(f"Alt text: {alt_text}")
            elif kind == "table":
                if text:
                    lines.append(text)
                if table_markdown:
                    lines.append("")
                    lines.append(table_markdown)
            elif kind == "formula":
                lines.append(f"Formula: {text}")
            elif kind == "code":
                lines.append("```text")
                lines.append(text)
                lines.append("```")
            else:
                lines.append(text)
            lines.append("")
    return "\n".join(lines).strip() + "\n"


async def _gemini_document_for_pdf(
    pdf_path: Path,
    *,
    batch_size: int,
    max_pages: int,
) -> dict[str, Any]:
    settings = get_settings()
    llm_client = make_llm_client(settings)
    try:
        page_count = _page_count(pdf_path)
        page_numbers = list(range(1, min(page_count, max_pages) + 1))
        title_candidates: list[str] = []
        pages_by_number: dict[int, list[dict[str, Any]]] = {}

        async def _request_batch(batch_pages: list[int]) -> list[dict[str, Any]]:
            content = [
                {"type": "text", "text": CANONICAL_BATCH_PROMPT},
                *_render_page_parts(pdf_path, batch_pages),
                {
                    "type": "text",
                    "text": json.dumps(
                        _request_payload(pdf_path.name, batch_pages),
                        indent=2,
                        ensure_ascii=True,
                    ),
                },
            ]
            try:
                raw = await request_llm_json(
                    llm_client=llm_client,
                    content=content,
                    schema_name="canonical_document_batch",
                    response_schema=CANONICAL_BATCH_SCHEMA,
                    cache_breakpoint_index=len(batch_pages) if batch_pages else 0,
                )
                return [_normalize_batch(raw, batch_pages)]
            except Exception:
                if len(batch_pages) <= 1:
                    raise
                normalized_batches: list[dict[str, Any]] = []
                for page_number in batch_pages:
                    normalized_batches.extend(await _request_batch([page_number]))
                return normalized_batches

        with track_llm_usage() as usage:
            started = perf_counter()
            for index in range(0, len(page_numbers), batch_size):
                batch_pages = page_numbers[index:index + batch_size]
                normalized_batches = await _request_batch(batch_pages)
                for normalized in normalized_batches:
                    title = str(normalized.get("document_title") or "").strip()
                    if title:
                        title_candidates.append(title)
                    for page in normalized.get("pages", []):
                        if not isinstance(page, dict):
                            continue
                        page_number = int(page.get("page_number") or 0)
                        pages_by_number[page_number] = list(page.get("blocks") or [])
            elapsed = perf_counter() - started

        title = max(title_candidates, key=len) if title_candidates else ""
        pages = [
            {"page_number": page_number, "blocks": pages_by_number.get(page_number, [])}
            for page_number in sorted(pages_by_number)
        ]
        return {
            "document_title": title,
            "pages": pages,
            "meta": {
                "page_count_total": page_count,
                "pages_processed": len(page_numbers),
                "pages_capped": page_count > max_pages,
                "batch_size": batch_size,
                "llm_requests": usage.request_count,
                "llm_prompt_tokens": usage.prompt_tokens,
                "llm_completion_tokens": usage.completion_tokens,
                "llm_total_tokens": usage.total_tokens,
                "llm_cost_usd": round(usage.cost_usd, 6),
                "runtime_seconds": round(elapsed, 3),
            },
        }
    finally:
        await llm_client.close()


async def _current_structure_for_pdf(
    pdf_path: Path,
    *,
    run_dir: Path,
    max_pages: int,
) -> dict[str, Any]:
    structure_dir = run_dir / f"{_slugify(pdf_path.stem)}_structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    result = await extract_structure(pdf_path, structure_dir)
    return _structure_slice_counts(result.document_json, max_pages=max_pages)


async def _run_one(
    pdf_path: Path,
    *,
    run_dir: Path,
    batch_size: int,
    max_pages: int,
) -> dict[str, Any]:
    gemini = await _gemini_document_for_pdf(pdf_path, batch_size=batch_size, max_pages=max_pages)
    current = await _current_structure_for_pdf(pdf_path, run_dir=run_dir, max_pages=max_pages)

    gemini_counts = dict(sorted(_block_counts_from_gemini(gemini).items()))
    gemini_text_chars = _text_chars_from_gemini(gemini)
    excerpt = _excerpt_lines(gemini)

    stem = _slugify(pdf_path.stem)
    (run_dir / f"{stem}_gemini.json").write_text(
        json.dumps(gemini, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    (run_dir / f"{stem}_gemini.md").write_text(
        _markdown_from_gemini(gemini),
        encoding="utf-8",
    )

    return {
        "filename": pdf_path.name,
        "pdf_path": str(pdf_path),
        "pages_processed": int(gemini["meta"]["pages_processed"]),
        "pages_capped": bool(gemini["meta"]["pages_capped"]),
        "gemini_title": str(gemini.get("document_title") or ""),
        "current_title": str(current.get("title") or ""),
        "gemini_counts": gemini_counts,
        "current_counts": current.get("role_counts", {}),
        "gemini_text_chars": gemini_text_chars,
        "current_text_chars": int(current.get("text_chars") or 0),
        "llm_requests": int(gemini["meta"]["llm_requests"]),
        "llm_cost_usd": float(gemini["meta"]["llm_cost_usd"]),
        "runtime_seconds": float(gemini["meta"]["runtime_seconds"]),
        "excerpt": excerpt,
    }


def _write_report(run_dir: Path, results: list[dict[str, Any]], *, batch_size: int, max_pages: int) -> None:
    report_path = run_dir / "report.md"
    successful = [item for item in results if "error" not in item]
    failed = [item for item in results if "error" in item]
    total_cost = sum(float(item["llm_cost_usd"]) for item in successful)
    runtimes = [float(item["runtime_seconds"]) for item in successful]
    with report_path.open("w", encoding="utf-8") as handle:
        handle.write("# Gemini Canonical Spike\n\n")
        handle.write(f"Generated: {datetime.now().isoformat()}\n\n")
        handle.write("## Setup\n\n")
        handle.write(f"- Documents: {len(results)}\n")
        handle.write(f"- Successful: {len(successful)}\n")
        handle.write(f"- Failed: {len(failed)}\n")
        handle.write(f"- Max pages per document: {max_pages}\n")
        handle.write(f"- Page batch size: {batch_size}\n")
        handle.write(f"- Total Gemini cost: ${total_cost:.6f}\n")
        if runtimes:
            handle.write(f"- Average runtime per document: {statistics.mean(runtimes):.2f}s\n")
        if failed:
            handle.write("\n## Failures\n\n")
            for item in failed:
                handle.write(f"- `{item['filename']}`: {item['error']}\n")
        handle.write("\n## Documents\n\n")

        for item in successful:
            handle.write(f"### {item['filename']}\n\n")
            handle.write(f"- Pages processed: {item['pages_processed']}")
            if item["pages_capped"]:
                handle.write(" (capped)")
            handle.write("\n")
            handle.write(f"- Gemini title: `{item['gemini_title'] or 'n/a'}`\n")
            handle.write(f"- Current structure title: `{item['current_title'] or 'n/a'}`\n")
            handle.write(f"- Gemini runtime: `{item['runtime_seconds']:.2f}s`\n")
            handle.write(f"- Gemini cost: `${item['llm_cost_usd']:.6f}` across `{item['llm_requests']}` request(s)\n")
            handle.write(f"- Gemini blocks: `{json.dumps(item['gemini_counts'], sort_keys=True)}`\n")
            handle.write(f"- Current structure elements: `{json.dumps(item['current_counts'], sort_keys=True)}`\n")
            handle.write(f"- Gemini text chars: `{item['gemini_text_chars']}`\n")
            handle.write(f"- Current structure text chars: `{item['current_text_chars']}`\n")
            handle.write("\nExcerpt:\n")
            for line in item["excerpt"]:
                handle.write(f"- {line}\n")
            handle.write("\nArtifacts:\n")
            stem = _slugify(Path(item["filename"]).stem)
            handle.write(f"- `{stem}_gemini.json`\n")
            handle.write(f"- `{stem}_gemini.md`\n\n")


async def _main_async(args: argparse.Namespace) -> None:
    pdf_paths: list[Path] = []
    for raw in args.pdfs:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = DEFAULT_INPUT_DIR / raw
        if not candidate.exists():
            raise FileNotFoundError(f"PDF not found: {candidate}")
        pdf_paths.append(candidate)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTPUT_ROOT / f"gemini_canonical_spike_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for pdf_path in pdf_paths:
        print(f"Running Gemini canonical spike on {pdf_path.name}")
        try:
            result = await _run_one(
                pdf_path,
                run_dir=run_dir,
                batch_size=args.batch_size,
                max_pages=args.max_pages,
            )
        except Exception as exc:
            print(f"  failed: {exc}")
            results.append(
                {
                    "filename": pdf_path.name,
                    "pdf_path": str(pdf_path),
                    "error": str(exc),
                }
            )
            continue
        results.append(result)
        print(
            f"  cost=${result['llm_cost_usd']:.6f} runtime={result['runtime_seconds']:.2f}s "
            f"gemini_blocks={result['gemini_counts']}"
        )

    (run_dir / "summary.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    _write_report(run_dir, results, batch_size=args.batch_size, max_pages=args.max_pages)
    print(f"Wrote results to {run_dir}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a Gemini-first canonical document spike on representative PDFs.",
    )
    parser.add_argument(
        "pdfs",
        nargs="*",
        default=DEFAULT_DOCS,
        help="PDF filenames from the representative input dir, or explicit paths.",
    )
    parser.add_argument("--max-pages", type=int, default=6, help="Maximum pages per document.")
    parser.add_argument("--batch-size", type=int, default=2, help="Pages per Gemini request.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
