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
1. Classify (scanned vs digital)
2. OCR (OCRmyPDF)
3. Structure (Docling)
4. Alt Text (Vision LLM via OpenAI-compatible API)
5. Tag (pikepdf PDF/UA)
6. Validate (veraPDF)
