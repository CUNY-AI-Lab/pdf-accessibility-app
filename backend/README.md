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

The backend currently uses OpenRouter with Gemini models and structured outputs.

Important behaviors:
- `json_schema` structured output requests
- `provider.require_parameters=true`
- retry and `Retry-After` handling
- concurrency limits
- prompt caching breakpoints
- real usage/cost tracking from provider responses

Main files:
- [app/services/llm_client.py](app/services/llm_client.py)
- [app/services/intelligence_llm_utils.py](app/services/intelligence_llm_utils.py)

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

PDF/UA coverage matrix:

```bash
cd backend
PYTHONPATH=. uv run python scripts/generate_pdfua_rule_coverage.py
```

## Current evidence

- exact curated corpus: [../backend/data/benchmarks/corpus_20260308_202258/corpus_report.md](../backend/data/benchmarks/corpus_20260308_202258/corpus_report.md)
- representative non-huge corpus: [../backend/data/benchmarks/corpus_20260311_121723/corpus_report.md](../backend/data/benchmarks/corpus_20260311_121723/corpus_report.md)
- official form set: [../backend/data/benchmarks/corpus_20260309_123540/corpus_report.md](../backend/data/benchmarks/corpus_20260309_123540/corpus_report.md)
