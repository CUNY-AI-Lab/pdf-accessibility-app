# PDF Accessibility App

## Project Structure
- `backend/` — FastAPI (Python 3.12, managed with `uv`)
- `frontend/` — React + Vite + TypeScript (managed with `bun`)
- `data/` — Runtime storage (git-ignored): uploads, processing, output, SQLite DB

## Development

### Backend
```bash
cd backend
uv run uvicorn app.main:app --reload --port 8001
```

### Frontend
```bash
cd frontend
bun dev
```

Frontend proxies `/api` and `/health` to `http://localhost:8001` via Vite config.

## Conventions
- Python: ruff for linting/formatting, async everywhere, type hints required
- TypeScript: strict mode, TanStack Query for server state, Tailwind CSS 4
- Database: SQLite via SQLAlchemy async + aiosqlite
- API: RESTful, all under `/api/` prefix, Pydantic schemas for validation

## Key Files
- `backend/app/main.py` — FastAPI app entry point
- `backend/app/pipeline/` — 6-step PDF accessibility pipeline
- `backend/app/api/` — API route handlers
- `backend/app/models.py` — SQLAlchemy ORM models (Job, JobStep, AltTextEntry)
- `frontend/src/pages/` — React pages (Upload, Dashboard, JobDetail, Review)
- `frontend/src/api/jobs.ts` — TanStack Query hooks for API calls

## Pipeline Steps
1. Classify (scanned vs digital + language detection)
2. OCR (OCRmyPDF, auto-detected language)
3. Structure (Docling — docling-serve or local fallback)
4. Alt Text (Vision LLM via OpenAI-compatible API)
5. Tag (pikepdf PDF/UA)
6. Validate (veraPDF)

## Fidelity Check
Post-remediation quality gate in `backend/app/pipeline/fidelity.py`. Runs after
tagging/validation to detect content issues the validator can't catch.

Key checks: text drift, reading order, table coverage, form labels, font fidelity.

**Text drift** uses classification-aware comparison:
- **Digital PDFs**: strict symmetric similarity via `SequenceMatcher` (threshold 0.82)
- **Mixed/Scanned PDFs**: asymmetric metrics via `rapidfuzz` — measures *containment*
  (is original text preserved in output?) and *preservation* (were original characters
  corrupted?) rather than penalizing OCR-added text. This avoids false positives when
  OCR legitimately expands the text layer.

Blocking fidelity tasks surface on the review page via `FidelityIssueCard` with
task-type-specific metadata (similarity scores, page numbers, table counts, etc.)
and a "Mark as Resolved" button. Cards show the LLM's plain-language analysis
("What we found") and suggested fix. Resolving all blocking tasks upgrades the
job from `manual_remediation` → `complete`. Task types are registered in
`backend/app/services/review_surface.py`.

## OCR Language Detection
Auto-detects document language during classification. Shared utilities in
`backend/app/pipeline/language.py` (BCP-47 ↔ Tesseract code mappings).

- **Digital/mixed PDFs**: text extraction via pdfminer → lingua-py detection
- **Scanned PDFs**: probe OCR on page 1 with all installed Tesseract packs →
  lingua-py on the OCR'd text
- Per-job `ocr_language` stored on the Job model; falls back to `OCR_LANGUAGE`
  env var (default: `eng`)
- Docker image includes 18 language packs (CUNY-relevant subset)

## docling-serve (Structure Extraction)
Persistent Docling HTTP server on the Mac Studio, reachable via Tailscale
node sharing. Zero cold start, ~1.3s for a 7-page PDF with RapidOCR.

- Server: `DOCLING_DEVICE=mps docling-serve run --host 0.0.0.0 --port 5001`
- Tailscale IP: `100.108.110.78` (shared from personal tailnet to CUNY tailnet)
- Production config: `DOCLING_SERVE_URL=http://100.108.110.78:5001` in `.env`
- Local dev: use `DOCLING_SERVE_URL=http://localhost:5001` when running on Mac Studio
- OCR engine: RapidOCR (set via `DOCLING_SERVE_OCR_ENGINE`)
- API: async — POST `/v1/convert/file/async`, poll `/v1/status/poll/{id}`, GET `/v1/result/{id}`
- Falls back to local Docling if `DOCLING_SERVE_URL` is not set
