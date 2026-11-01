#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

(
  cd "$repo_root/backend"
  uv run --frozen ruff check .
  PYRIGHT_PYTHON_GLOBAL_NODE=1 uv run --frozen pyright
  env \
    LLM_API_KEY=check-only \
    LLM_BASE_URL=http://127.0.0.1:8000/v1 \
    LLM_MODEL=check-model \
    DOCLING_SERVE_URL= \
    DOCLING_SERVE_TOKEN= \
    PYTHONPATH=. \
    uv run --frozen pytest tests -q
)

(
  cd "$repo_root/frontend"
  bun run check
)

# The v2 Worker: lint, types, its build (with the app at /pdf-accessibility/),
# and its tests in workerd, which need Node 22.18 or later.
(
  cd "$repo_root/worker"
  bun run check
)
