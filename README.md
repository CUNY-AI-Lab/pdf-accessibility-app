# PDF Accessibility App

An automated PDF remediation tool from the [CUNY AI Lab](https://ailab.gc.cuny.edu/) that converts uploaded PDFs into accessible, PDF/UA-1 compliant documents.

Upload a PDF and the app classifies it, runs OCR if needed, extracts structure, generates alt text, writes PDF/UA tags, and validates the result against veraPDF. Documents that can't be fully remediated automatically are flagged for manual review.

## Features

- **Automatic classification** of digital, mixed, and scanned PDFs
- **OCR with language auto-detection** via OCRmyPDF and Tesseract
- **Structure extraction** via [Docling](https://github.com/docling-project/docling), with optional remote `docling-serve` for GPU acceleration
- **LLM-generated alt text** for figures, with decorative-image detection
- **LLM-assisted semantic tagging** for tables, forms, reading order, and grounded text
- **Deterministic PDF/UA tag writing** via [pikepdf](https://github.com/pikepdf/pikepdf)
- **Compliance gating** with [veraPDF](https://verapdf.org/) PDF/UA-1 validation
- **Fidelity checks** for text drift, reading order, table coverage, and form labels
- **Anonymous sessions** — no login required, jobs scoped to an HTTP-only cookie

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy (async SQLite) |
| Frontend | React, TypeScript, Vite, Tailwind CSS 4, TanStack Query |
| PDF Processing | pikepdf, OCRmyPDF, Ghostscript, Poppler, QPDF |
| Structure Extraction | Docling (local or `docling-serve`) |
| Semantic Analysis | Gemini Developer API |
| Validation | veraPDF |

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- [Bun](https://bun.sh/)
- [Ghostscript](https://www.ghostscript.com/)
- [OCRmyPDF](https://ocrmypdf.readthedocs.io/)
- [Tesseract](https://github.com/tesseract-ocr/tesseract)
- [Poppler](https://poppler.freedesktop.org/) (`pdftoppm`)
- [veraPDF](https://verapdf.org/) and a Java runtime
- A Gemini Developer API key

On macOS, install system dependencies via Homebrew. On Debian/Ubuntu, install `ghostscript`, `poppler-utils`, `tesseract-ocr`, and a Java runtime.

## Quick Start

```bash
# Clone and configure
git clone <repo-url> pdf-accessibility-app
cd pdf-accessibility-app
cp .env.example .env
# Edit .env and set GEMINI_API_KEY

# Install dependencies
cd backend && uv sync
cd ../frontend && bun install
```

Run the two services in separate terminals:

```bash
# Backend
cd backend
uv run uvicorn app.main:app --reload --port 8001

# Frontend
cd frontend
bun dev
```

Open http://localhost:5173. The frontend proxies `/api` and `/health` to the backend on port 8001.

## Docker

A single-container image bundles all system dependencies and serves the built frontend from FastAPI.

```bash
cp .env.example .env
# Set GEMINI_API_KEY

docker compose up -d --build
```

Open http://localhost:8080. Set `APP_PORT` in `.env` to use a different port.

To run without Compose:

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

The image preloads Docling models so there are no first-run downloads. For subpath deployments (e.g., behind a reverse proxy), set `VITE_APP_BASE_PATH=/pdf-accessibility/` before building.

Tesseract language packs included: English, Spanish, French, German, Chinese (Simplified and Traditional), Russian, Arabic, Korean, Bengali, Polish, Hebrew, Yiddish, Haitian Creole, Hindi, Italian, Portuguese, and Japanese. Add others by extending the Dockerfile.

## Configuration

Configure the app via `.env`. Key variables:

| Variable | Description | Default |
|---|---|---|
| `GEMINI_API_KEY` | Google Gemini API key (required) | — |
| `LLM_BASE_URL` | Chat-completions base URL | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `LLM_API_KEY` | Optional chat-completions credential (falls back to `GEMINI_API_KEY`) | — |
| `LLM_MODEL` | Chat-completions model identifier | `google/gemini-3-flash-preview` |
| `GEMINI_MODEL` | Native Gemini model for direct PDF lanes | `gemini-3-flash-preview` |
| `GEMINI_DIRECT_THINKING_LEVEL` | Thinking level for direct PDF semantic lanes | `low` |
| `GEMINI_DIRECT_ALT_TEXT_THINKING_LEVEL` | Thinking level for figure semantics and alt text | `medium` |
| `ALT_TEXT_MAX_CONCURRENCY` | Max concurrent alt-text requests per PDF | `8` |
| `ALT_TEXT_GLOBAL_MAX_CONCURRENCY` | Process-wide cap for alt-text requests | `12` |
| `DOCLING_SERVE_URL` | Remote `docling-serve` URL (falls back to local Docling when unset) | — |
| `DOCLING_SERVE_TOKEN` | Bearer token for a protected `docling-serve` proxy | — |
| `OCR_LANGUAGE` | Fallback Tesseract language code | `eng` |
| `JOB_TTL_HOURS` | Hours before jobs expire | `12` |
| `VERAPDF_PATH` | Path to the veraPDF binary | `verapdf` |
| `GHOSTSCRIPT_PATH` | Path to the Ghostscript binary | `gs` |

### Structure extraction with `docling-serve`

Structure extraction is the slowest pipeline step. Running a persistent `docling-serve` process eliminates cold starts and enables GPU acceleration. Start it with:

```bash
DOCLING_DEVICE=mps docling-serve run --host 0.0.0.0 --port 5001
```

Then set `DOCLING_SERVE_URL=http://localhost:5001` in `.env`. Without `DOCLING_SERVE_URL`, the app falls back to local Docling on CPU.

### OCR language detection

The app auto-detects document language during classification and selects the matching Tesseract pack for OCR. Digital and mixed PDFs use [lingua-py](https://github.com/pemistahl/lingua-py) on the extracted text. Scanned PDFs probe OCR on page 1 with every installed language pack, then run lingua-py on the result. If no pack is installed for the detected language, the app falls back to `OCR_LANGUAGE`.

On macOS, `brew install tesseract-lang` installs all packs. On Debian/Ubuntu, install individual packs with `apt install tesseract-ocr-<lang>`.

## Pipeline

Each upload runs through seven steps:

1. **Classify** — Determine whether the PDF is digital, mixed, or scanned
2. **OCR** — Add a searchable text layer to scanned pages
3. **Structure** — Extract document structure via Docling, with LLM-assisted TOC enhancement
4. **Alt Text** — Generate alt text for figures and reclassify misidentified elements
5. **Tag** — Resolve ambiguous semantics (tables, forms, reading order) via LLM, then write PDF/UA tags deterministically
6. **Validate** — Check PDF/UA-1 compliance with veraPDF
7. **Fidelity** — Verify output faithfulness (text drift, reading order, table coverage, form labels)

## Project Structure

```
backend/
  app/
    api/              FastAPI route handlers
    pipeline/         Classify, OCR, structure, tag, validate, fidelity
    services/         Semantic adjudication, storage, LLM client
    models.py         SQLAlchemy ORM models
    config.py         App settings
  tests/

frontend/
  src/
    pages/            Upload, Dashboard, JobDetail, Review
    components/
    api/              TanStack Query hooks
    types/

data/                 Runtime storage (git-ignored)
```

## Development

```bash
# Backend tests
cd backend
PYTHONPATH=. uv run pytest tests -q

# Frontend lint and build
cd frontend
bun run lint
bun run build
```

Verify the effective runtime (LLM provider, Docling target, installed binaries) with:

```bash
cd backend
PYTHONPATH=. uv run python scripts/runtime_diagnostics.py
```

## Documentation

- [Architecture](docs/architecture.md)
- [Benchmarks](docs/benchmarks.md)
- [Accessibility Coverage](ACCESSIBILITY_COVERAGE.md)
- [Accessibility Coverage Matrix](docs/a11y_coverage_matrix.md)
- [PDF/UA Rule Coverage](docs/pdfua_rule_coverage_matrix.md)
- [Backend README](backend/README.md)
- [Frontend README](frontend/README.md)
