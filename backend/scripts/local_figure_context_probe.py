from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.services.intelligence_gemini_figures import (
    FIGURE_BATCH_PROMPT,
    FIGURE_BATCH_SCHEMA,
)
from app.services.intelligence_llm_utils import context_json_part
from app.services.llm_client import LlmClient
from app.services.local_semantic import _extract_json_from_message
from app.services.pdf_preview import (
    render_bbox_preview_png_data_url,
    render_page_jpeg_data_url,
)

TEXT_TYPES = {"heading", "paragraph", "note", "list_item"}
VARIANT_NAMES = ("baseline", "nearby_text", "target_preview", "combined")


def _load_structure_json(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("select structure_json from jobs limit 1").fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise RuntimeError(f"No structure_json found in {db_path}")
    return json.loads(row[0])


def _load_alt_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "select figure_index, status, image_path, generated_text from alt_texts order by figure_index"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "figure_index": int(figure_index),
            "status": str(status),
            "image_path": str(image_path),
            "generated_text": generated_text,
        }
        for figure_index, status, image_path, generated_text in rows
    ]


def _bbox_area(bbox: dict[str, Any] | None) -> float:
    if not isinstance(bbox, dict):
        return 0.0
    try:
        width = max(0.0, float(bbox.get("r", 0.0)) - float(bbox.get("l", 0.0)))
        height = max(0.0, float(bbox.get("t", 0.0)) - float(bbox.get("b", 0.0)))
    except Exception:
        return 0.0
    return width * height


def _figure_page_context(page_figures: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    areas = {int(fig["figure_index"]): _bbox_area(fig.get("bbox")) for fig in page_figures}
    page_max_area = max(areas.values(), default=0.0)
    context: dict[int, dict[str, Any]] = {}
    for fig in page_figures:
        figure_index = int(fig["figure_index"])
        area = areas.get(figure_index, 0.0)
        larger_siblings = [
            sibling
            for sibling in page_figures
            if int(sibling["figure_index"]) != figure_index
            and areas.get(int(sibling["figure_index"]), 0.0) > area * 8.0
        ]
        caption = str(fig.get("caption") or "").strip()
        likely_child_ui = bool(
            not caption
            and area > 0.0
            and page_max_area > 0.0
            and area / page_max_area <= 0.02
            and larger_siblings
        )
        context[figure_index] = {
            "bbox_area": round(area, 2),
            "relative_area": round(area / page_max_area, 4) if page_max_area > 0.0 else 0.0,
            "likely_child_ui_figure": likely_child_ui,
            "larger_sibling_indexes": [int(sibling["figure_index"]) for sibling in larger_siblings[:4]],
        }
    return context


def _group_figures(structure_json: dict[str, Any], alt_rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    image_paths = {row["figure_index"]: row["image_path"] for row in alt_rows}
    figures = [
        el
        for el in structure_json.get("elements", [])
        if isinstance(el, dict) and el.get("type") == "figure" and isinstance(el.get("figure_index"), int)
    ]
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for fig in figures:
        figure_index = int(fig["figure_index"])
        page = int(fig.get("page") or 1)
        grouped[page].append(
            {
                "figure_index": figure_index,
                "page": page,
                "caption": str(fig.get("caption") or "").strip(),
                "bbox": fig.get("bbox"),
                "image_path": image_paths.get(figure_index, ""),
            }
        )
    return {page: sorted(items, key=lambda item: item["figure_index"]) for page, items in grouped.items()}


def _nearby_text_by_figure(structure_json: dict[str, Any]) -> dict[int, dict[str, Any]]:
    elements = structure_json.get("elements", [])
    result: dict[int, dict[str, Any]] = {}
    for idx, element in enumerate(elements):
        if not isinstance(element, dict) or element.get("type") != "figure":
            continue
        figure_index = element.get("figure_index")
        if not isinstance(figure_index, int):
            continue
        page = int(element.get("page") or 1)
        previous_blocks: list[dict[str, str]] = []
        next_blocks: list[dict[str, str]] = []
        nearest_heading = ""
        for prev_idx in range(idx - 1, -1, -1):
            prev = elements[prev_idx]
            if not isinstance(prev, dict) or int(prev.get("page") or 1) != page:
                continue
            prev_type = str(prev.get("type") or "")
            prev_text = " ".join(str(prev.get("text") or prev.get("caption") or "").split()).strip()
            if not prev_text or prev_type not in TEXT_TYPES:
                continue
            if not nearest_heading and prev_type == "heading":
                nearest_heading = prev_text
            if len(previous_blocks) < 2:
                previous_blocks.append({"type": prev_type, "text": prev_text})
            if nearest_heading and len(previous_blocks) >= 2:
                break
        previous_blocks.reverse()
        for next_idx in range(idx + 1, len(elements)):
            nxt = elements[next_idx]
            if not isinstance(nxt, dict) or int(nxt.get("page") or 1) != page:
                continue
            nxt_type = str(nxt.get("type") or "")
            nxt_text = " ".join(str(nxt.get("text") or nxt.get("caption") or "").split()).strip()
            if not nxt_text or nxt_type not in TEXT_TYPES:
                continue
            if len(next_blocks) < 2:
                next_blocks.append({"type": nxt_type, "text": nxt_text})
            if len(next_blocks) >= 2:
                break
        result[figure_index] = {
            "nearest_heading": nearest_heading,
            "preceding_text_blocks": previous_blocks,
            "following_text_blocks": next_blocks,
        }
    return result


def _candidate_payload(
    *,
    figure: dict[str, Any],
    page_context: dict[int, dict[str, Any]],
    nearby_text: dict[int, dict[str, Any]],
    include_nearby_text: bool,
) -> dict[str, Any]:
    figure_index = int(figure["figure_index"])
    payload = {
        "figure_index": figure_index,
        "caption": str(figure.get("caption") or "").strip(),
        "page": int(figure.get("page") or 1),
        "bbox": figure.get("bbox"),
        **page_context.get(figure_index, {}),
    }
    if include_nearby_text:
        payload.update(nearby_text.get(figure_index, {}))
    return payload


def _variant_content(
    *,
    variant: str,
    pdf_path: Path,
    job_filename: str,
    document_title: str,
    page: int,
    figures: list[dict[str, Any]],
    page_context: dict[int, dict[str, Any]],
    nearby_text: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    include_nearby_text = variant in {"nearby_text", "combined"}
    include_target_preview = variant in {"target_preview", "combined"}

    payload_candidates = [
        _candidate_payload(
            figure=figure,
            page_context=page_context,
            nearby_text=nearby_text,
            include_nearby_text=include_nearby_text,
        )
        for figure in figures
    ]
    context_payload: dict[str, Any] = {
        "job_filename": job_filename,
        "document_title": document_title,
        "page": page,
        "variant": variant,
        "candidates": payload_candidates,
    }
    content: list[dict[str, Any]] = [
        {"type": "text", "text": FIGURE_BATCH_PROMPT},
        {
            "type": "image_url",
            "image_url": {"url": render_page_jpeg_data_url(pdf_path, page)},
        },
        context_json_part(context_payload),
    ]
    for figure in figures:
        figure_index = int(figure["figure_index"])
        if include_target_preview and isinstance(figure.get("bbox"), dict):
            content.extend(
                [
                    {"type": "text", "text": f"Figure candidate {figure_index} target preview:"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": render_bbox_preview_png_data_url(
                                pdf_path,
                                page,
                                figure["bbox"],
                                highlight=True,
                            )
                        },
                    },
                ]
            )
        image_path = Path(str(figure.get("image_path") or "")).expanduser()
        if image_path.exists():
            content.extend(
                [
                    {"type": "text", "text": f"Figure candidate {figure_index} crop:"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(image_path.read_bytes()).decode("ascii")
                        },
                    },
                ]
            )
    return content


async def _request_variant(
    *,
    model_name: str,
    content: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    client = LlmClient(
        base_url="http://127.0.0.1:1234/v1",
        api_key="local",
        model=model_name,
        timeout=300,
        max_retries=1,
        max_concurrency=1,
    )
    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are evaluating PDF accessibility and figure semantics. "
                    "Stay grounded in the provided page evidence and return JSON only."
                ),
            },
            {"role": "user", "content": content},
        ]
        request_kwargs: dict[str, Any] = {
            "messages": messages,
            "temperature": 0,
        }
        response = None
        try:
            response = await client.chat_completion(
                **request_kwargs,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "figure_batch_intelligence",
                        "strict": True,
                        "schema": FIGURE_BATCH_SCHEMA,
                    },
                },
            )
        except Exception:
            response = None
        if response is None:
            try:
                response = await client.chat_completion(
                    **request_kwargs,
                    response_format={"type": "json_object"},
                )
            except Exception:
                response = await client.chat_completion(**request_kwargs)
        message = response["choices"][0]["message"]
        return _extract_json_from_message(message), response
    finally:
        await client.close()


def _usage_dict(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


async def run_probe(
    *,
    benchmark_db: Path,
    pdf_path: Path,
    reference_db: Path | None,
    models: list[str],
) -> dict[str, Any]:
    structure_json = _load_structure_json(benchmark_db)
    alt_rows = _load_alt_rows(benchmark_db)
    grouped = _group_figures(structure_json, alt_rows)
    nearby_text = _nearby_text_by_figure(structure_json)
    reference = _load_alt_rows(reference_db) if reference_db else []
    reference_by_index = {int(row["figure_index"]): row for row in reference}

    results: list[dict[str, Any]] = []
    document_title = str(structure_json.get("title") or "").strip()
    job_filename = pdf_path.name

    for model_name in models:
        for variant in VARIANT_NAMES:
            model_result: dict[str, Any] = {
                "model": model_name,
                "variant": variant,
                "pages": [],
            }
            for page in sorted(grouped):
                page_figures = grouped[page]
                page_context = _figure_page_context(page_figures)
                content = _variant_content(
                    variant=variant,
                    pdf_path=pdf_path,
                    job_filename=job_filename,
                    document_title=document_title,
                    page=page,
                    figures=page_figures,
                    page_context=page_context,
                    nearby_text=nearby_text,
                )
                parsed, response = await _request_variant(model_name=model_name, content=content)
                page_result = {
                    "page": page,
                    "usage": _usage_dict(response),
                    "decisions": parsed.get("decisions") or [],
                }
                model_result["pages"].append(page_result)
            decision_map: dict[int, dict[str, Any]] = {}
            for page_result in model_result["pages"]:
                for decision in page_result["decisions"]:
                    if not isinstance(decision, dict) or not isinstance(decision.get("figure_index"), int):
                        continue
                    decision_map[int(decision["figure_index"])] = decision
            summary_rows: list[dict[str, Any]] = []
            for figure_index in sorted({*decision_map.keys(), *reference_by_index.keys()}):
                summary_rows.append(
                    {
                        "figure_index": figure_index,
                        "decision": decision_map.get(figure_index, {}),
                        "reference": reference_by_index.get(figure_index, {}),
                        "nearby_text": nearby_text.get(figure_index, {}),
                    }
                )
            model_result["summary_rows"] = summary_rows
            model_result["usage"] = {
                "prompt_tokens": sum(page["usage"]["prompt_tokens"] for page in model_result["pages"]),
                "completion_tokens": sum(page["usage"]["completion_tokens"] for page in model_result["pages"]),
                "total_tokens": sum(page["usage"]["total_tokens"] for page in model_result["pages"]),
            }
            results.append(model_result)
    return {
        "benchmark_db": str(benchmark_db),
        "pdf_path": str(pdf_path),
        "reference_db": str(reference_db) if reference_db else None,
        "models": models,
        "variants": list(VARIANT_NAMES),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Probe local figure-context variants against saved benchmark artifacts. "
            "Reconstructs the local figure batch packets and compares model decisions."
        )
    )
    parser.add_argument("--benchmark-db", required=True, help="Path to the local-model workflow_benchmark.sqlite3")
    parser.add_argument("--pdf", required=True, help="Path to the benchmark PDF used for page previews")
    parser.add_argument(
        "--reference-db",
        default="",
        help="Optional reference workflow_benchmark.sqlite3 to compare against (for example the Gemini run)",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Local model identifier to test. Repeat for multiple models.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional file path to write the JSON results.",
    )
    args = parser.parse_args()

    models = args.model or [
        "qwen3-vl-8b-local",
        "qwen/qwen3-vl-30b",
        "qwen3-vl-32b-thinking-local",
    ]
    payload = asyncio.run(
        run_probe(
            benchmark_db=Path(args.benchmark_db).expanduser().resolve(),
            pdf_path=Path(args.pdf).expanduser().resolve(),
            reference_db=Path(args.reference_db).expanduser().resolve() if args.reference_db else None,
            models=models,
        )
    )
    rendered = json.dumps(payload, indent=2, ensure_ascii=True)
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
