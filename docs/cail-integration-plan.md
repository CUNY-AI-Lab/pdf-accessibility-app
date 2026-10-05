# CAIL integration and improvement plan

Status: approved 2026-09-23, revised the same day and on 2026-10-04 (pipeline
host, where the Gateway token lives, Phase 2 ships to v1). This is the plan of record
for moving PDF Accessibility behind Doorway and the CAIL Gateway and for the
improvement work that follows. Update it as phases land.

## Goal: version 2

A new version of PDF Accessibility, built as a Cloudflare Worker, that beats
both the current app (v1) and Adobe Acrobat's tagging on the same documents.

- **Shape:** a Worker at `/pdf-accessibility` behind Doorway owns the interface,
  sign-in, jobs (Workflows), storage (R2), and model calls (the CAIL Gateway,
  via the 24-hour `cail:gateway` leg). The PDF tools that cannot run in a
  Worker (OCR, pikepdf, veraPDF, Docling or its replacement) run on a pipeline
  host that takes work from the Worker. v1 keeps serving until v2 wins.
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
  Gemini path was removed after the bake-off, not kept as a fallback.
- **Spend:** each person's own Gateway budget. Doorway's `cail:gateway` leg
  lasts 24 hours (Phase 1), so a job can keep calling the Gateway after the
  upload request ends. Gateway checks membership and quota on every call.
- **Hosting:** a new Cloudflare Worker version of the app owns the interface,
  sign-in, jobs, and storage. The Python pipeline (Docling, OCR, pikepdf,
  veraPDF) runs outside Cloudflare, because Cloudflare Containers cost too
  much for this workload, on actual-dell (decided 2026-10-04). actual-dell
  already runs production's docling-serve, has a GPU, and adds no spend; the
  Lab's AWS account would first need its invoice and procurement settled. The
  cost of one machine is that queued jobs wait through its reboots.
- **The Gateway token stays on Cloudflare** (decided 2026-10-04). Doorway keeps
  identity legs in Worker-to-Worker headers, and actual-dell has a shared
  administrator account. The pipeline host makes only outbound calls: it
  takes work from the Worker and sends each model request to a Worker
  endpoint with a credential good for that one job, and the Worker forwards
  it to the Gateway with the person's token. Moving the pipeline to another
  host is a redeploy, not a redesign.
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

1. **Evaluation first.** Done: see [evaluation.md](evaluation.md)
   (olmOCR-Bench subsets, a printed-book set, and a structure round-trip on
   gold tagged PDFs, all scored on what a screen reader hears; title,
   language, and alt text on the gold set scored by a judge model). Still to
   add: veraPDF results on the gold set.
2. **Gateway client.** One model lane: the chat-completions client at
   `LLM_BASE_URL` (the Gateway) with an open-weight vision `LLM_MODEL`, given
   rendered page images, never PDF files.
   Done. Every AI step goes through it, including title, front matter, and
   table of contents, which used to run only on direct Gemini with PDF input.
   The local-semantic lane merged into it. Answers are read from `content` or,
   for reasoning models, `reasoning_content`; structured output falls back
   from `json_schema` to `json_object` to plain JSON. The direct Gemini path,
   its model-name validator, and `GEMINI_API_KEY` are gone.
3. **Per-job credential.** The pipeline gets its model endpoint and
   credential per job instead of from `LLM_API_KEY`, and never stores them.
   In v2 these are the Worker's model-relay endpoint and the job's
   credential (see Decisions). A refused credential stops the job's model
   calls and fails the job with a clear message instead of being absorbed by
   the AI steps' fallbacks: the Gateway's 401 `invalid_credential`, 403
   `insufficient_scope` (membership ended), and 429 `quota_exceeded` (the
   person's budget or the Lab's ceiling; `x-should-retry: false`, so not
   retried).
4. **Bake-off.** Done 2026-09-24; results in [evaluation.md](evaluation.md).
   `qwen3-vl-235b-a22b-instruct` is the default: on the gold set its titles
   and alt text judge as well as Gemini 3 Flash Preview's and 3.8 Flash's,
   and it writes alt text for fewer figures (110 of 163 against 124), mostly
   because it treats screenshots of tables as tables. No model changes the
   structure or table scores. Mistral Large 3 (three images per request at
   most), Kimi K2.5, and Gemma 3 27B (timeouts) were dropped early.
5. **Ship to v1.** Merge the branch and deploy v1 on NML with the Lab's own
   Gateway key in place of the direct Gemini key, so v1 users get the
   remediation gains before v2 exists. First upgrade actual-dell's
   docling-serve from 1.12.0 (Docling 2.72) to 1.35.0 (Docling 2.130), the
   version every measurement in [evaluation.md](evaluation.md) used: the
   older server ignores the heading-hierarchy option, so every heading comes
   back as level 1. Step 3 follows.
6. **Rebuild the evaluation data.** The corpus in `backend/data/eval/` was
   git-ignored and was lost with its worktree in late September 2026. Done
   2026-10-04: everything but Adobe's outputs was recovered from the build
   session's transcripts, and the corpus is now defined in
   [backend/eval/](../backend/eval/README.md) (sources with checksums or a
   pinned dataset revision, and our own tests) and rebuilt byte for byte by
   `scripts/fetch_eval_corpus.py`. Files that cannot be re-downloaded
   reliably live in the Lab's private copy,
   CUNY-AI-Lab/pdf-accessibility-eval-data. Adobe is re-run within the free
   tier, about 45 pages a month (Auto-Tag costs ten transactions a page).
7. **A CUNY corpus.** Add documents CUNY people actually remediate, from CUNY
   Academic Works: well-tagged course materials (syllabi, OER textbooks,
   slides, assignments) as a structure round-trip, each also as an
   image-only scan scored against the same tags; then pages of untagged
   articles and dissertations with per-page tests.

### 3. Worker version behind Doorway

- A Worker serves the app at `/pdf-accessibility` as a protected Doorway
  product with audience `cail:pdf-accessibility` and a `cail:gateway` leg.
- Jobs, files, and results move to Cloudflare storage. The pipeline host on
  actual-dell takes work from the Worker, relays model calls through it, and
  returns results, all over outbound HTTPS with its own host credential (no
  inbound port or Tunnel).
- A `cail:gateway` leg lasts at most 24 hours but is also capped by the
  session's absolute deadline and the Admission membership, so the Worker
  reads the token's actual expiry and does not start a job it cannot finish.
- Retire the NML deployment, the two no-script bypass Worker Routes, and
  their release reconciliation.
- Admission needs no product registration; add the app to the admin desk's
  fleet status.
- Production cutover needs Stephen's go-ahead after the PRs are green.

### 4. Reliability

- An overall job deadline, kept inside the Gateway token's actual expiry.
- A Docling timeout that scales with page count instead of one fixed 300s.
- Timeouts that actually cancel paid model calls (no `wait_for` over
  `to_thread` leaving the call running).
- Clean up `debug/` with the rest of the 12-hour retention sweep.
- Keep internal hostnames and paths out of the public `/health/ready` body.

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

- Every OCR line reaches the structure tree once, in order. (An earlier
  measurement credited this with printed books 30.5% → 91.7%. That baseline
  was wrong: the scorer's reader ignored marked-content `/ActualText`.
  Measured correctly, production scores 92.1% and the branch 92.5%.)
- Tables: 10.1% → 64.8% of olmOCR-Bench table tests; multi-column reading
  order: 41.1% → 58.0%.
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

None.
