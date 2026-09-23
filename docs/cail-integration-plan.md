# CAIL integration and improvement plan

Status: approved 2026-09-23. This is the plan of record for moving PDF
Accessibility behind Doorway and the CAIL Gateway and for the improvement work
that follows. Update it as phases land.

## Decisions

- **Audience:** CUNY-only. Doorway signs people in and Admission membership
  gates access. Anonymous use ends at cutover.
- **Models:** open-weight vision models through the CAIL Gateway. The direct
  Gemini path is removed after cutover, not kept as a fallback.
- **Spend:** each person's own Gateway budget. Doorway issues a job-length
  `cail:gateway` token for this app's routes (Phase 1), so a background job
  can keep calling the Gateway after the upload request ends. Gateway still
  checks membership and quota on every call, so revocation takes effect
  mid-job.
- **Hosting:** unchanged. The app stays on the NML server; Docling stays on
  actual-dell. Reliability work happens in place.
- **Improvements:** reliability, remediation quality, human-readable reports,
  and code health.

## Phases

Each phase ships on its own and leaves a working product.

### 1. Doorway: job-length Gateway token (cail-tools-admission)

- A route in Doorway's closed Gateway route list can declare that its
  `cail:gateway` token lives for a job, not a request. Only the Gateway token
  changes; the product's own-audience token keeps the 5-minute lifetime.
- The job lifetime is a fixed constant (2 hours). The existing caps still
  apply: no token outlives the Doorway session or the Admission membership.
- No route uses it when this merges, so it changes no production behavior.
  Media Tools can opt in separately to fix its long-transcription path.

### 2. Gateway model lane (this repo)

1. **Evaluation first.** Commit a small, redistributable benchmark corpus and a
   scoring script (release-ready rate, veraPDF, fidelity, alt-text checks).
   Record the current Gemini baseline so the model change is judged on
   evidence.
2. **Gateway client.** Send page images (the existing local-semantic lane) to
   the Gateway's chat completions endpoint. Add image-based versions of the
   title/front-matter and TOC lanes. Cap image and request sizes. Remove the
   `LLM_API_KEY`→Gemini fallback and the "gemini" model-name validator.
3. **Per-job credential.** The job carries the caller's Gateway token in
   memory, never in the database. Expiry, revocation, and `quota_exceeded`
   fail the job with a clear message.
4. **Bake-off.** Run the corpus through candidate Gateway vision models
   (qwen3-vl, kimi-k2.6, others in the live catalog) against the Gemini
   baseline. Choose the model on the measured results.

### 3. Cutover behind Doorway (Doorway, this repo, whisper-server-nml)

- Doorway fronts `/pdf-accessibility` as a protected product with audience
  `cail:pdf-accessibility` and the job-length Gateway token, reaching the NML
  origin without dropping the app's own session and CSRF cookies.
- The app turns on `CAIL_IDENTITY_REQUIRED`. Jobs are owned by the signed-in
  subject. Cookies are scoped to `/pdf-accessibility/`.
- Remove the two no-script bypass Worker Routes and their release
  reconciliation. Fix the NML nginx header buffer. Bind the app to localhost.
- Admission needs no product registration; add the app to the admin desk's
  fleet status.
- Production cutover needs Stephen's go-ahead after the PRs are green.

### 4. Reliability (in place)

- An overall job deadline, kept inside the Gateway token lifetime.
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

## Follow-ups outside this plan

- Media Tools: opt its Gateway routes into the job-length token (suspected
  expired-token failure on long transcriptions).
- Moving the app or Docling off NML and actual-dell (Cloudflare Containers)
  is a separate project.
