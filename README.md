# PDF Accessibility Remediation Tool

Automated PDF/UA compliance remediation developed at the CUNY AI Lab. Upload a PDF and the tool analyzes its structure, generates alt text for images, adds accessibility tags, and validates the result against PDF/UA-1 (ISO 14289-1).

## What it does

The pipeline runs six steps on each uploaded PDF:

1. **Classify** — Detects whether the document is scanned, digital, or mixed
2. **OCR** — Adds a searchable text layer to scanned pages (via OCRmyPDF)
3. **Structure** — Extracts document structure using Docling (headings, tables, lists, figures, links)
4. **Alt Text** — Generates image descriptions using a vision LLM; marks decorative images as artifacts
5. **Tag** — Writes PDF/UA structure tags, bookmarks, metadata, and link annotations (via pikepdf)
6. **Validate** — Checks compliance against PDF/UA-1 using veraPDF

After processing, the tool produces a tagged PDF, a compliance report, and (when needed) a set of review tasks for issues that require human judgment.

For a detailed breakdown of which accessibility requirements are fully addressed, partially addressed, and not covered, see [ACCESSIBILITY_COVERAGE.md](ACCESSIBILITY_COVERAGE.md).

## Architecture

```
frontend/          React + Vite + TypeScript (Tailwind CSS 4, TanStack Query)
backend/           FastAPI + SQLAlchemy async (Python 3.12)
data/              Runtime storage: uploads, processing, output, SQLite DB (git-ignored)
```

The frontend proxies `/api` and `/health` to the backend via Vite dev server config.

## Prerequisites

| Dependency | Purpose | Install |
|---|---|---|
| Python 3.12+ | Backend runtime | — |
| [uv](https://docs.astral.sh/uv/) | Python package manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Bun](https://bun.sh/) | Frontend runtime & package manager | `curl -fsSL https://bun.sh/install \| bash` |
| [OCRmyPDF](https://ocrmypdf.readthedocs.io/) | OCR for scanned PDFs | `brew install ocrmypdf` / `apt install ocrmypdf` |
| [Ghostscript](https://www.ghostscript.com/) | Font embedding | `brew install ghostscript` / `apt install ghostscript` |
| [veraPDF](https://verapdf.org/) | PDF/UA validation | [Install guide](https://docs.verapdf.org/install/) |
| [Poppler](https://poppler.freedesktop.org/) | Page preview rendering (`pdftoppm`) | `brew install poppler` / `apt install poppler-utils` |

## Setup

```bash
# Clone and enter the project
git clone <repo-url> && cd pdf-accessibility-app

# Backend dependencies
cd backend && uv sync && cd ..

# Frontend dependencies
cd frontend && bun install && cd ..

# Configure environment
cp .env.example .env   # then edit .env with your LLM API key
```

### Environment variables

Create a `.env` file in the project root:

```env
# LLM — any OpenAI-compatible API (OpenRouter, local Ollama, etc.)
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=your-api-key-here
LLM_MODEL=google/gemini-3-flash-preview

# Optional: OCR language (default: eng)
OCR_LANGUAGE=eng

# Optional: veraPDF path if not on PATH
VERAPDF_PATH=verapdf

# Optional: job TTL in hours (default: 24)
JOB_TTL_HOURS=24

# Optional: max upload size in bytes (default: 500 MB)
MAX_UPLOAD_SIZE_BYTES=524288000
```

The tool calls this endpoint for alt text generation and font-mapping assistance. Any OpenAI-compatible API works (OpenRouter, a local Ollama instance, etc.). Set `LLM_STRICT_VALIDATION=false` for local endpoints that don't require an API key.

## Development

Start both servers:

```bash
# Terminal 1 — Backend (port 8001)
cd backend
uv run uvicorn app.main:app --reload --port 8001

# Terminal 2 — Frontend (port 5173)
cd frontend
bun dev
```

Open [http://localhost:5173](http://localhost:5173).

### Running tests

```bash
# Backend
cd backend && uv run pytest tests/ -q

# Frontend type check
cd frontend && bun run tsc --noEmit

# Production build
cd frontend && bun run build
```

## Project structure

```
backend/
  app/
    api/             Route handlers (jobs, documents, review)
    pipeline/        6-step processing pipeline
      orchestrator.py   Coordinates all steps
      ocr.py            OCRmyPDF integration
      structure.py      Docling structure extraction
      alt_text.py       Vision LLM alt text generation
      tagger.py         pikepdf PDF/UA tag writing
      validator.py      veraPDF compliance checking
    services/        Business logic (LLM client, file storage, job management)
    models.py        SQLAlchemy ORM (Job, JobStep, AltTextEntry, ReviewTask)
    config.py        Pydantic settings from .env
  tests/             pytest suite

frontend/
  src/
    pages/           Upload, Dashboard, JobDetail, Review
    components/      UI components (OutcomeHero, RemediationSummary, ValidationReport, ...)
    hooks/           Custom hooks (useJobProgress SSE, useToast)
    api/             TanStack Query hooks for API calls
    utils/           Shared utilities (typeGuards, format)
```

## How results appear

Job results use progressive disclosure so non-expert users aren't overwhelmed:

1. **Outcome Hero** — A single card answering "is my PDF accessible?" with a download button
2. **What We Did** — Plain-language summary of remediation actions taken
3. **Technical Details** — Collapsible section with the full validation report, metadata, and violation details

When issues require human judgment, the tool generates review tasks categorized by type (font fidelity, alt text accuracy, table semantics, reading order, etc.) with blocking/advisory severity levels.

## Conventions

- **Python**: ruff for linting/formatting, async everywhere, type hints required
- **TypeScript**: strict mode, TanStack Query for server state, Tailwind CSS 4
- **Database**: SQLite via SQLAlchemy async + aiosqlite
- **API**: RESTful under `/api/`, Pydantic schemas for request/response validation

## License

Internal tool — CUNY AI Lab.
