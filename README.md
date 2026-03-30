# PDF Accessibility App

An automated PDF remediation tool from the [CUNY AI Lab](https://ailab.gc.cuny.edu/) that converts uploaded PDFs into accessible, PDF/UA-1 compliant documents.

## Overview

Upload a PDF and the app automatically remediates it through a multi-step pipeline: classification, OCR, structure extraction, semantic analysis, accessible tagging, and validation. Output is gated by [veraPDF](https://verapdf.org/) compliance checks and fidelity analysis to ensure quality. Documents that can't be fully remediated are flagged for manual review.

### Pipeline

1. **Classify** — Determine whether the PDF is digital, mixed, or scanned
2. **OCR** — Add searchable text to scanned pages ([OCRmyPDF](https://ocrmypdf.readthedocs.io/)) with automatic language detection
3. **Structure** — Extract document structure via [Docling](https://github.com/docling-project/docling), with LLM-assisted TOC enhancement
4. **Alt Text** — Generate alt text for figures and reclassify misidentified elements using a vision LLM
5. **Tag** — Resolve ambiguous semantics (tables, forms, reading order, grounded text) via LLM, then write PDF/UA structure tags deterministically with [pikepdf](https://github.com/pikepdf/pikepdf)
6. **Validate** — Check PDF/UA-1 compliance with [veraPDF](https://verapdf.org/)
7. **Fidelity** — Verify output faithfulness (text drift, reading order, table coverage, form labels)

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy (async SQLite) |
| Frontend | React, TypeScript, Vite, Tailwind CSS 4, TanStack Query |
| PDF Processing | pikepdf, OCRmyPDF, Ghostscript, Poppler, QPDF |
| Structure Extraction | Docling (local or docling-serve) |
| Semantic Analysis | Vision LLM via OpenAI-compatible API (e.g., OpenRouter) |
| OCR | OCRmyPDF, Tesseract |
| Validation | veraPDF |

## Prerequisites

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/)
- **[Bun](https://bun.sh/)**
- **[Ghostscript](https://www.ghostscript.com/)**
- **[OCRmyPDF](https://ocrmypdf.readthedocs.io/)**
- **[Tesseract](https://github.com/tesseract-ocr/tesseract)** (used by OCRmyPDF and for local crop OCR)
- **[veraPDF](https://verapdf.org/)** (requires Java runtime)
- **[Poppler](https://poppler.freedesktop.org/)** (`pdftoppm`)

On macOS: install via Homebrew. On Ubuntu/Debian: `ghostscript`, `poppler-utils`, `tesseract-ocr`, plus a Java runtime for veraPDF.

## Getting Started

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum, set LLM_API_KEY
```

Key environment variables:

| Variable | Description | Default |
|---|---|---|
| `LLM_BASE_URL` | OpenAI-compatible API base URL | `https://openrouter.ai/api/v1` |
| `LLM_API_KEY` | API key for the LLM provider | — |
| `LLM_MODEL` | Model identifier | `google/gemini-3-flash-preview` |
| `OCR_LANGUAGE` | Default Tesseract language code | `eng` |
| `JOB_TTL_HOURS` | Hours before jobs expire | `12` |
| `VERAPDF_PATH` | Path to veraPDF binary | `verapdf` |
| `GHOSTSCRIPT_PATH` | Path to Ghostscript binary | `gs` |

### 2. Install dependencies

```bash
cd backend && uv sync
cd ../frontend && bun install
```

### 3. Run locally

```bash
# Terminal 1 — backend
cd backend
uv run uvicorn app.main:app --reload --port 8001

# Terminal 2 — frontend
cd frontend
bun dev
```

- Frontend: http://localhost:5173
- Backend API: http://localhost:8001

The frontend proxies `/api` and `/health` to the backend via Vite config.

## Docker

A single-container deployment bundles all dependencies (Ghostscript, OCRmyPDF, Tesseract, Poppler, QPDF, Java, veraPDF) with the built frontend served by FastAPI.

```bash
cp .env.example .env
# Edit .env with your LLM_API_KEY

docker compose up -d --build
```

Open http://localhost:8080. Health check at `/health`.

If port 8080 is in use, set `APP_PORT` in `.env`.

You can also run the image directly without Compose:

```bash
docker build -t pdf-accessibility-app .
docker run -d \
  --name pdf-accessibility-app \
  --env-file .env \
  -p 8080:8001 \
  -v pdf_accessibility_data:/app/data \
  -v pdf_accessibility_cache:/home/app/.cache \
  pdf-accessibility-app
```

Notes:
- The image preloads Docling models so there are no first-run downloads.
- For subpath deployments, set `VITE_APP_BASE_PATH` before building (e.g., `/pdf-accessibility/`).
- Tesseract language packs included: English, Spanish, French, German, Chinese (Simplified + Traditional), Russian, Arabic, Korean, Bengali, Polish, Hebrew, Yiddish, Haitian Creole, Hindi, Italian, Portuguese, Japanese. Add others by extending the Dockerfile.

## Project Structure

```
backend/
  app/
    api/              # FastAPI route handlers
    pipeline/         # classify, ocr, structure, tag, validate, fidelity
    services/         # semantic adjudication, storage, LLM client
    models.py         # SQLAlchemy ORM models
    config.py         # App settings
  tests/              # Backend test suite

frontend/
  src/
    pages/            # Upload, Dashboard, JobDetail, Review
    components/       # UI components
    api/              # TanStack Query hooks
    types/            # Shared TypeScript types

data/                 # Runtime storage (git-ignored)
```

## Testing

```bash
# Backend
cd backend
PYTHONPATH=. uv run pytest tests -q

# Frontend
cd frontend
bun run lint
bun run build
```

## OCR Language Support

The app auto-detects the document language during classification and selects the appropriate Tesseract language pack for OCR. For digital/mixed PDFs, it extracts existing text and identifies the language with [lingua-py](https://github.com/pemistahl/lingua-py). For scanned PDFs, it runs a quick probe OCR on page 1 with all installed language packs, then identifies the language from the result.

Language priority: **auto-detection > `OCR_LANGUAGE` env var default**.

For local development, install Tesseract language packs via your package manager. On macOS, `brew install tesseract-lang` installs all languages. On Debian/Ubuntu, install individual packs (e.g., `apt install tesseract-ocr-spa`). If a language pack is missing, probe OCR falls back gracefully to the `OCR_LANGUAGE` default.

## Session Model

The app uses anonymous browser sessions — no login required. Each browser gets an HTTP-only session cookie, and all jobs are scoped to that session. Jobs expire after `JOB_TTL_HOURS` (default: 12 hours).

## Documentation

- [Architecture](docs/architecture.md)
- [Benchmarks](docs/benchmarks.md)
- [Accessibility Coverage](ACCESSIBILITY_COVERAGE.md)
- [Accessibility Coverage Matrix](docs/a11y_coverage_matrix.md)
- [PDF/UA Rule Coverage](docs/pdfua_rule_coverage_matrix.md)
- [Backend README](backend/README.md)
- [Frontend README](frontend/README.md)
