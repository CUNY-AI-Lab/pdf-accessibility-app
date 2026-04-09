# Backend

The backend is a FastAPI application that owns:

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
    api/         REST endpoints for jobs, documents, and review
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
- [app/services/intelligence_gemini_semantics.py](app/services/intelligence_gemini_semantics.py)
- [app/services/document_intelligence_models.py](app/services/document_intelligence_models.py)

Wrappers over the shared engine:
- [app/services/intelligence_gemini_pages.py](app/services/intelligence_gemini_pages.py)
- [app/services/intelligence_gemini_tables.py](app/services/intelligence_gemini_tables.py)
- [app/services/intelligence_gemini_forms.py](app/services/intelligence_gemini_forms.py)
- [app/services/intelligence_gemini_figures.py](app/services/intelligence_gemini_figures.py)
- [app/services/intelligence_gemini_toc.py](app/services/intelligence_gemini_toc.py)

## LLM transport

The backend now uses Gemini directly for PDF-native understanding and the Gemini Developer API chat-completions endpoint for the remaining structured JSON lanes.

Important behaviors:
- `json_schema` structured output requests
- retry and `Retry-After` handling
- concurrency limits
- prompt caching breakpoints
- real usage/cost tracking from provider responses

Main files:
- [app/services/llm_client.py](app/services/llm_client.py)
- [app/services/intelligence_llm_utils.py](app/services/intelligence_llm_utils.py)

The same backend settings drive both the real app and the benchmark scripts. If `DOCLING_SERVE_URL` is set, the structure step uses that server; otherwise it falls back to local Docling. Semantic adjudication uses Gemini directly for PDF-native lanes and the configured Gemini chat-completions endpoint for the remaining JSON-only lanes.

For Gemini-first deployments, prefer:
- `GEMINI_API_KEY=<key>`
- `LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai`
- `LLM_API_KEY=` blank, unless you intentionally want a separate credential for the chat-completions path
- `LLM_MODEL=google/gemini-3-flash-preview`
- `GEMINI_MODEL=gemini-3-flash-preview`
- `USE_DIRECT_GEMINI_PDF=true`

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

Run the API:

```bash
cd backend
uv run uvicorn app.main:app --reload --port 8001
```

The backend scopes jobs to an anonymous browser session using an HTTP-only cookie.
There is no login flow, but job APIs only return documents created by the current
browser session. Uploaded files and job state expire after `JOB_TTL_HOURS`
(`12` by default).
This protects jobs inside the app's API surface; semantic LLM calls still go to
the configured provider.

For HTTPS deployments, set `ANONYMOUS_SESSION_COOKIE_SECURE=true` so the cookie
is only sent over secure transport.

## Tests

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
