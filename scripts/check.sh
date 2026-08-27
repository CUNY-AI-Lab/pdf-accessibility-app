#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

(
  cd "$repo_root/backend"
  env \
    GEMINI_API_KEY= \
    GOOGLE_API_KEY= \
    LLM_API_KEY=check-only \
    LLM_BASE_URL=http://127.0.0.1:8000/v1 \
    LLM_MODEL=gemini-check \
    USE_DIRECT_GEMINI_PDF=false \
    DOCLING_SERVE_URL= \
    DOCLING_SERVE_TOKEN= \
    PYTHONPATH=. \
    uv run --frozen pytest tests -q
)

(
  cd "$repo_root/frontend"
  bun run lint
  bun run build
)
