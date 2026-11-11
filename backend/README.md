# Backend

The backend is a Python pipeline imported in-process by the v2 runner and
evaluation scripts. It owns:

- the remediation pipeline
- semantic adjudication and grounding
- PDF writing
- validation and fidelity checks
- user-visible review surface selection
- benchmark generation

## Key directories

```text
backend/
  app/
    runner/      Worker task claims, per-task databases, and view publication
    pipeline/    classify, ocr, structure, tag, validate, fidelity
    services/    LLM client, semantic units, previews, form helpers, storage
    models.py    SQLAlchemy models
    config.py    settings
  scripts/       corpus benchmarks and docs generators
  tests/         pytest suite
```

## Main pipeline files

- [app/pipeline/orchestrator.py](app/pipeline/orchestrator.py)
- [app/pipeline/tagger.py](app/pipeline/tagger.py)
- [app/pipeline/validator.py](app/pipeline/validator.py)
- [app/pipeline/fidelity.py](app/pipeline/fidelity.py)

## Semantic layer

The semantic layer is generic-first, not element-type specific.

Shared pieces:
- [app/services/semantic_units.py](app/services/semantic_units.py)
- [app/services/intelligence_semantics.py](app/services/intelligence_semantics.py)
- [app/services/document_intelligence_models.py](app/services/document_intelligence_models.py)

Wrappers over the shared engine:
- [app/services/intelligence_pages.py](app/services/intelligence_pages.py)
- [app/services/intelligence_tables.py](app/services/intelligence_tables.py)
- [app/services/intelligence_forms.py](app/services/intelligence_forms.py)
- [app/services/intelligence_figures.py](app/services/intelligence_figures.py)
- [app/services/intelligence_toc.py](app/services/intelligence_toc.py)

## LLM transport

Every AI step sends rendered page images to one chat-completions lane: the
CAIL Gateway with an open-weight vision `LLM_MODEL`
(default `qwen3-vl-235b-a22b-instruct`). The runner uses the Worker relay and
a task lease; it never holds a Gateway key. Standalone benchmark scripts use
`LLM_BASE_URL` and a personal key in `LLM_API_KEY`.

Important behaviors:
- `json_schema` structured output, falling back to `json_object` for models
  without the structured-output capability, and to plain JSON after that
- answers read from `content`, or `reasoning_content` for reasoning models
- retries connection failures and 429/500/502/503/504 responses, never a
  timed-out call
- concurrency limits
- real usage/cost tracking from provider responses

Main files:
- [app/services/llm_client.py](app/services/llm_client.py)
- [app/services/intelligence_llm_utils.py](app/services/intelligence_llm_utils.py)

The same backend settings drive both the real app and the benchmark scripts. If `DOCLING_SERVE_URL` is set, the structure step uses that server; otherwise it falls back to local Docling.

Alt-text concurrency is page-level and bounded per PDF by `ALT_TEXT_MAX_CONCURRENCY`.
The global alt-text cap, `ALT_TEXT_GLOBAL_MAX_CONCURRENCY`, prevents batch uploads from multiplying per-PDF concurrency into unbounded provider work.

On Apple Silicon, the recommended local setup is `docling-serve` with `DOCLING_DEVICE=mps`. That accelerates structure extraction, but the tagging/writer step in [app/pipeline/tagger.py](app/pipeline/tagger.py) remains CPU-bound.

Runtime check:

```bash
cd backend
PYTHONPATH=. uv run python scripts/runtime_diagnostics.py
```

## External binaries

The backend depends on system binaries for OCR, previews, and validation:
- `ghostscript`
- `pdftoppm` (Poppler)
- `tesseract`
- `verapdf`

Use explicit paths in deployment instead of relying on Homebrew-style locations:

```env
VERAPDF_PATH=verapdf
GHOSTSCRIPT_PATH=gs
TESSERACT_PATH=tesseract
PDFTOPPM_PATH=pdftoppm
BINARY_SEARCH_DIRS=/usr/bin,/usr/local/bin
```

Resolution order is:
1. explicit `*_PATH`
2. normal `PATH`
3. `BINARY_SEARCH_DIRS`
4. local development fallbacks

## Development

Use the Worker/runner harness in [../worker/README.md](../worker/README.md)
(`worker/dev/e2e.mjs`) for the v2 app locally. To run a configured runner:

```bash
cd backend
uv run --frozen python -m app.runner
```

It needs `RUNNER_WORKER_URL` and `RUNNER_TOKEN`, claims work over outbound
HTTP, and renders the views the Worker serves. The Worker owns sign-in, API
routes, job scheduling, storage, and retention. Each runner task opens its own
SQLite database; `app.database.prepare_schema` is shared schema preparation.

Use the pinned uv shim for dependency changes, ignoring workstation-wide uv
configuration. For example:

```bash
cd backend
UV_NO_CONFIG=1 uvx uv@0.13.4 sync --locked --extra dev
```

## Tests

The repository source gate runs Ruff, Pyright, and the backend tests before
the frontend and Worker checks:

```bash
./scripts/check.sh
```

Run the focused backend type check from `backend/` after syncing the dev extra:

```bash
PYRIGHT_PYTHON_GLOBAL_NODE=1 .venv/bin/pyright
```

Run the backend tests alone:

```bash
cd backend
PYTHONPATH=. uv run pytest tests -q
```

## Benchmarks

Representative corpus:

```bash
cd backend
PYTHONPATH=. uv run python scripts/corpus_benchmark.py --exclude-wac
```

Use the `assistive-core` profile when you want the full workflow semantics while temporarily skipping only the figure alt-text branch:

```bash
cd backend
PYTHONPATH=. uv run python scripts/corpus_benchmark.py --profile assistive-core --exclude-wac
```

Round-trip strip step for gold accessible PDFs:

```bash
cd backend
PYTHONPATH=. uv run python scripts/strip_accessibility.py \
  --input /path/to/gold-accessible.pdf \
  --output data/benchmarks/roundtrip/mydoc_stripped.pdf
```

Round-trip comparison against the gold file:

```bash
cd backend
PYTHONPATH=. uv run python scripts/roundtrip_compare.py \
  --gold /path/to/gold-accessible.pdf \
  --candidate /path/to/remediated-output.pdf \
  --manifest /path/to/mydoc.roundtrip.json
```

Round-trip corpus benchmark:

```bash
cd backend
PYTHONPATH=. uv run python scripts/roundtrip_corpus_benchmark.py
```

The round-trip corpus runner defaults to the `assistive-core` profile. That profile runs the full workflow except for the figure alt-text branch, so validation, fidelity, review surfaces, grounded text, tables, widget cleanup, and form labeling all remain in the loop. Use the full workflow only when you specifically want to include figure/alt-text behavior:

```bash
cd backend
PYTHONPATH=. uv run python scripts/roundtrip_corpus_benchmark.py --workflow-profile full
```

Adobe Accessibility Checker is available as a local benchmark-only check. It uploads the candidate PDF to Adobe and consumes one Adobe PDF Services transaction, so it is intentionally not wired into the app runtime:

```bash
cd backend
uv run --with pdfservices-sdk python scripts/adobe_accessibility_check.py \
  /path/to/candidate.pdf \
  --credentials /path/to/PDFServicesAPI-Credentials.zip \
  --output-dir data/adobe-accessibility-checks \
  --confirm-spend
```

The script keeps a local monthly usage ledger under `~/.cache/pdf-accessibility-app/` and defaults to a conservative local cap of 100 transactions per month.

The round-trip comparison reports form field presence and field-type recovery separately from exact accessible-name replay. Use manifest assertions to encode the assistive requirement you actually care about: field existence, control type, required label terms, and disambiguating context.

PDF/UA coverage matrix:

```bash
cd backend
PYTHONPATH=. uv run python scripts/generate_pdfua_rule_coverage.py
```

## Current evidence

- exact curated corpus: [../backend/data/benchmarks/corpus_20260308_202258/corpus_report.md](../backend/data/benchmarks/corpus_20260308_202258/corpus_report.md)
- representative non-huge corpus: [../backend/data/benchmarks/corpus_20260311_121723/corpus_report.md](../backend/data/benchmarks/corpus_20260311_121723/corpus_report.md)
- official form set: [../backend/data/benchmarks/corpus_20260309_123540/corpus_report.md](../backend/data/benchmarks/corpus_20260309_123540/corpus_report.md)
