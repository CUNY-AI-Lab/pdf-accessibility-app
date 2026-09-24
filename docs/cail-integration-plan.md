# CAIL integration and improvement plan

Status: approved 2026-09-23, revised the same day. This is the plan of record
for moving PDF Accessibility behind Doorway and the CAIL Gateway and for the
improvement work that follows. Update it as phases land.

## Goal: version 2

A new version of PDF Accessibility, built as a Cloudflare Worker, that beats
both the current app (v1) and Adobe Acrobat's tagging on the same documents.

- **Shape:** a Worker at `/pdf-accessibility` behind Doorway owns the interface,
  sign-in, jobs (Workflows), storage (R2), and model calls (the CAIL Gateway,
  via the 24-hour `cail:gateway` leg). The PDF tools that cannot run in a
  Worker (OCR, pikepdf, veraPDF, Docling or its replacement) run in one compute
  service the Worker calls. v1 keeps serving until v2 wins.
- **Beats v1 and Adobe when,** on the evaluation suite in
  [evaluation.md](evaluation.md), scored on what a screen reader hears. The
  documents that matter most are printed books and articles, scanned or born
  digital; handwriting is a side case.
  - Text and reading order: v2 scores above v1 and Adobe on printed old books
    and born-digital multi-column pages, with 95% confidence intervals that do
    not overlap.
  - Page furniture: running heads, footers, and page numbers become artifacts
    (the benchmark's `headers_footers` absent tests).
  - Tables: header and data cells, and their neighbors, survive into the tags
    (the benchmark's table tests, read from the tag tree).
  - Structure (strip-and-restore corpus): v2 matches or beats Adobe on veraPDF
    PDF/UA-1 failures, headings, lists, figures and alt text, title, and
    language, and never regresses against v1 in any category.
  - No recognized text is dropped from the structure tree.
- **Targets** are set once the v1, v1-fixed, and Adobe baselines are measured.

## Decisions

- **Audience:** CUNY-only. Doorway signs people in and Admission membership
  gates access. Anonymous use ends at cutover.
- **Models:** open-weight vision models through the CAIL Gateway. The direct
  Gemini path is removed after cutover, not kept as a fallback.
- **Spend:** each person's own Gateway budget. Doorway's `cail:gateway` leg
  lasts 24 hours (Phase 1), so a job can keep calling the Gateway after the
  upload request ends. Gateway checks membership and quota on every call.
- **Hosting:** a new Cloudflare Worker version of the app owns the interface,
  sign-in, jobs, and storage. The Python pipeline (Docling, OCR,
  pikepdf, veraPDF) runs outside Cloudflare, on actual-dell or on the Lab's
  AWS account, because Cloudflare Containers cost too much for this workload.
  The Worker sends the pipeline host the job's Gateway token with each unit of
  work. Which host is still to be decided.
- **Improvements:** reliability, remediation quality, human-readable reports,
  and code health.

## Phases

Each phase ships on its own and leaves a working product.

### 1. Long-lived Gateway leg (cail-tools-admission)

Doorway's `cail:gateway` leg lasts 24 hours, capped by the session and the
Admission membership; every other leg stays at five minutes
(cail-tools-admission#246). Delegation grants (#245) are parked: every current
long-running consumer needs only the Gateway, which re-checks every call.

### 2. Gateway model lane (this repo)

1. **Evaluation first.** Done for the deterministic pipeline: see
   [evaluation.md](evaluation.md) (olmOCR-Bench subsets, a printed-book set,
   and a structure round-trip on gold tagged PDFs, all scored on what a
   screen reader hears). Still to add: the app's LLM steps (alt text,
   headings, tables, reading order) with a Gemini baseline, and veraPDF and
   alt-text checks on the gold set.
2. **Gateway client.** One model lane: the chat-completions client at
   `LLM_BASE_URL` (the Gateway) with an open-weight vision `LLM_MODEL`, given
   rendered page images, never PDF files.
   - Every AI step goes through it. Title, front matter, and table of contents
     today run only on direct Gemini with PDF input, so a Gateway setup would
     skip them; they get the same image-based request as the other steps.
   - The local-semantic lane, a second OpenAI-compatible client for local
     models, is the same thing pointed elsewhere; it merges into this lane.
   - Answers are read from `content` or, for reasoning models,
     `reasoning_content`; structured output falls back from `json_schema` to
     `json_object` to plain JSON, as today.
   - The "gemini" model-name validator goes. Direct Gemini with PDF input stays
     only as the baseline for the bake-off (step 4), selected by
     `USE_DIRECT_GEMINI_PDF`, and is removed after it with `GEMINI_API_KEY`.
3. **Per-job credential.** The Worker keeps the job's Gateway token and sends
   it with each unit of work; the pipeline never stores it. Revocation and
   `quota_exceeded` fail the job with a clear message.
4. **Bake-off.** Run the corpus through candidate Gateway vision models
   (qwen3-vl, kimi-k2.6, others in the live catalog) against the Gemini
   baseline. Choose the model on the measured results.

### 3. Worker version behind Doorway

- A Worker serves the app at `/pdf-accessibility` as a protected Doorway
  product with audience `cail:pdf-accessibility` and a `cail:gateway` leg.
- Jobs, files, and results move to Cloudflare storage; the pipeline host
  receives work and returns results over an authenticated channel.
- Retire the NML deployment, the two no-script bypass Worker Routes, and
  their release reconciliation.
- Admission needs no product registration; add the app to the admin desk's
  fleet status.
- Production cutover needs Stephen's go-ahead after the PRs are green.

### 4. Reliability

- An overall job deadline, kept inside the 24-hour Gateway leg.
- A Docling timeout that scales with page count instead of one fixed 300s.
- Timeouts that actually cancel paid model calls (no `wait_for` over
  `to_thread` leaving the call running).
- Clean up `debug/` with the rest of the 12-hour retention sweep.
- Correct the README's Docling host.

### 5. Human-readable reports

A downloadable HTML report (printable to PDF) per document: what was fixed,
what needs manual work, validation results, and generated alt text for review.

### 6. Code health

- Mark placeholder alt text as unapproved in the database, not only in the
  tagger.
- Run ruff in `scripts/check.sh`; drop the unused `tenacity` dependency.
- Split `pipeline/orchestrator.py` and `pipeline/tagger.py` along pipeline
  stage boundaries, in steps that each keep the suite green.

### 7. Remediation quality

Use the Phase 2 corpus to measure and improve the known gaps: complex tables,
multi-column reading order, uncaptioned charts, math, and inline language
changes. Each change reports its before/after on the corpus.

Done so far (branch `agent/cail-integration-plan`), each measured in
[evaluation.md](evaluation.md):

- Every OCR line reaches the structure tree once, in order (printed books
  30.5% → 91.7%).
- Text positions come from pdfminer's measurement of each text operator, not
  estimates, and a text operator spanning several table cells is split so
  each cell is tagged.
- Heading levels come from Docling's hierarchy stage and survive tagging.
- Language detection runs (lingua was never installed).
- ICC profiles missing /N no longer break text extraction.
- Optional OCR through the Gateway (`OCR_ENGINE=gateway`); it helps
  handwriting, not printed books, so Tesseract stays the default.

Next, from the evaluation and from peer tools (opendataloader-pdf, olmOCR,
the ASU/AWS remediation pipeline):

- Running heads and footers detected across pages (recto/verso, page-number
  sequences), not only per page.
- Picture classification (already requested from Docling, never read) as
  input to alt text and decorative-figure decisions.
- The scanned-document language probe: 18 Tesseract languages under a 30 s
  limit; use script detection first.

## Open decisions

- Pipeline host: actual-dell or the Lab's AWS account.
