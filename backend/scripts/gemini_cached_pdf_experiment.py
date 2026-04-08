from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.gemini_direct_experiment import (
    GeminiCachedPromptResult,
    find_gemini_api_key,
    make_pdf_subset_io,
    parse_page_spec,
)


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "to_dict"):
        try:
            return _json_safe(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump())
        except Exception:
            pass

    result: dict[str, object] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:
            continue
        if callable(attr):
            continue
        result[name] = _json_safe(attr)
    return result


def _usage_to_dict(value) -> dict[str, object]:
    safe_value = _json_safe(value)
    if isinstance(safe_value, dict):
        return safe_value
    return {"value": safe_value}


def _response_text(response) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            value = getattr(part, "text", None)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _result_dict(result: GeminiCachedPromptResult) -> dict[str, object]:
    return {
        "prompt": result.prompt,
        "text": result.text,
        "usage_metadata": result.usage_metadata,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Upload a PDF or PDF page subset to the direct Gemini API, create an explicit cache, "
            "and run repeated prompts against that cached document context."
        )
    )
    parser.add_argument("--pdf", required=True, help="Absolute path to the source PDF")
    parser.add_argument(
        "--pages",
        default="",
        help="Optional page selection like '1-3,5'. Omit for the whole PDF.",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-flash",
        help="Direct Gemini model name to use for the cache and follow-up prompts.",
    )
    parser.add_argument(
        "--ttl",
        default="3600s",
        help="Cache TTL, for example '600s' or '3600s'.",
    )
    parser.add_argument(
        "--env-file",
        default="",
        help="Optional env file to scan for GEMINI_API_KEY or GOOGLE_API_KEY.",
    )
    parser.add_argument(
        "--prompt",
        action="append",
        default=[],
        help="Follow-up prompt to ask against the cached PDF. Repeat for multiple prompts.",
    )
    args = parser.parse_args()

    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # pragma: no cover - import guard for local experiment use
        raise SystemExit(
            "google-genai is not installed. Run this script with:\n"
            "  uv run --with google-genai python scripts/gemini_cached_pdf_experiment.py ...\n"
            f"Import error: {exc}"
        ) from exc

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    page_numbers = parse_page_spec(args.pages)
    env_file = Path(args.env_file).expanduser().resolve() if args.env_file else None
    api_key = find_gemini_api_key(env_file=env_file)
    prompts = args.prompt or [
        "Summarize the important structure and navigation on these pages.",
        "List the elements that most affect accessibility on these pages.",
    ]

    client = genai.Client(api_key=api_key)
    subset_stream = make_pdf_subset_io(pdf_path, page_numbers=page_numbers)

    uploaded_file = client.files.upload(
        file=subset_stream,
        config={"mime_type": "application/pdf"},
    )
    cache = client.caches.create(
        model=args.model,
        config=types.CreateCachedContentConfig(
            contents=[uploaded_file],
            system_instruction=(
                "You are evaluating PDF accessibility and document semantics. "
                "Stay grounded in the provided PDF pages."
            ),
            ttl=args.ttl,
        ),
    )

    try:
        results: list[GeminiCachedPromptResult] = []
        for prompt in prompts:
            response = client.models.generate_content(
                model=args.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    cached_content=cache.name,
                ),
            )
            results.append(
                GeminiCachedPromptResult(
                    prompt=prompt,
                    text=_response_text(response),
                    usage_metadata=_usage_to_dict(getattr(response, "usage_metadata", None)),
                )
            )

        payload = {
            "pdf": str(pdf_path),
            "pages": page_numbers or "all",
            "model": args.model,
            "cache_name": cache.name,
            "file_name": getattr(uploaded_file, "name", ""),
            "file_uri": getattr(uploaded_file, "uri", ""),
            "results": [_result_dict(result) for result in results],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=True))
    finally:
        try:
            client.caches.delete(name=cache.name)
        except Exception:
            pass
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
