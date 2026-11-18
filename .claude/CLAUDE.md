# PDF Accessibility App

Read `AGENTS.md` first: it has the working rules (the source gate, the runner
contract, the Gateway relay, the PDFium lock). `docs/cail-integration-plan.md`
is the product plan of record; `docs/IMPROVEMENT_PLAN.md` tracks engineering
work and `docs/DECISIONS.md` records decisions.

## Project Structure
- `worker/` — Cloudflare Worker (v2): serves the app at `/pdf-accessibility`,
  the API, sign-in through Doorway, jobs in the `JobBoard` Durable Object,
  files in R2, and the Gateway relay. Managed with `bun`; tests run in workerd.
- `backend/` — the Python 3.12 pipeline, run as `python -m app.runner`. It
  claims work from the Worker and has no HTTP server. Managed with `uv`.
- `frontend/` — React + Vite + TypeScript, served by the Worker. Managed with `bun`.
- `deploy/actual-dell/` — how the runner and docling-serve run in production.

## Development
- Source gate (run before pushing): `scripts/check.sh`.
- Backend tests: `cd backend && PYTHONPATH=. uv run pytest tests -q`.
- Worker and frontend: `bun run check` in `worker/` and `frontend/`.
- The Worker and runner together on one PDF: `worker/dev/e2e.mjs`
  (see `worker/README.md`).

## Conventions
- Python: ruff for linting, pyright for types, async where the code is async.
- TypeScript: strict mode, TanStack Query for server state, Tailwind CSS 4.
- Runner state: per-task SQLite via SQLAlchemy async + aiosqlite.
- The runner contract lives in both `worker/src/contract.ts` and
  `backend/app/runner/`; change both in one change.

## Key Files
- `backend/app/runner/` — task claims, leases, pipeline execution, view publication
- `backend/app/pipeline/` — 7-step PDF accessibility pipeline
- `backend/app/models.py` — SQLAlchemy ORM models
- `worker/src/board.ts` — the JobBoard Durable Object (jobs, leases, retention)
- `worker/src/user-api.ts`, `worker/src/runner-api.ts` — the person's API and the runner's API
- `frontend/src/pages/` — React pages (Upload, Dashboard, JobDetail, Review)
- `frontend/src/api/jobs.ts` — TanStack Query hooks for API calls

## Pipeline Steps
1. Classify (scanned vs digital + language detection)
2. OCR (OCRmyPDF, auto-detected language)
3. Structure (Docling — docling-serve or local fallback)
4. Alt Text (Vision LLM via OpenAI-compatible API)
5. Tag (pikepdf PDF/UA)
6. Validate (veraPDF)
7. Fidelity (see below)

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
Persistent Docling HTTP server. Production uses the CUDA docling-serve 1.35.0
on actual-dell (`deploy/actual-dell/docling.compose.yml`); the v2 runner on the
same host reaches it at `http://127.0.0.1:5001`. Tailscale Serve also publishes
it on port 8444 to the tailnet, which `worker/dev/e2e.mjs` uses.

- Production config: `deploy/actual-dell/compose.yml` sets the runner's
  `DOCLING_SERVE_URL`
- Local dev: `DOCLING_DEVICE=mps docling-serve run --host 0.0.0.0 --port 5001`,
  then `DOCLING_SERVE_URL=http://localhost:5001`
- OCR engine: RapidOCR (set via `DOCLING_SERVE_OCR_ENGINE`)
- API: async — POST `/v1/convert/file/async`, poll `/v1/status/poll/{id}`, GET `/v1/result/{id}`
- Falls back to local Docling if `DOCLING_SERVE_URL` is not set
