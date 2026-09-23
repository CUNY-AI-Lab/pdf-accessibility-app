# CAIL integration and improvement plan

Status: approved 2026-09-23, revised the same day. This is the plan of record
for moving PDF Accessibility behind Doorway and the CAIL Gateway and for the
improvement work that follows. Update it as phases land.

## Decisions

- **Audience:** CUNY-only. Doorway signs people in and Admission membership
  gates access. Anonymous use ends at cutover.
- **Models:** open-weight vision models through the CAIL Gateway. The direct
  Gemini path is removed after cutover, not kept as a fallback.
- **Spend:** each person's own Gateway budget. A job holds a Doorway
  delegation grant (Phase 1) and refreshes it for 5-minute `cail:gateway`
  tokens, so it can keep calling the Gateway after the upload request ends.
  Gateway still checks membership and quota on every call.
- **Hosting:** a new Cloudflare Worker version of the app owns the interface,
  sign-in, grants, jobs, and storage. The Python pipeline (Docling, OCR,
  pikepdf, veraPDF) runs outside Cloudflare, on actual-dell or on the Lab's
  AWS account, because Cloudflare Containers cost too much for this workload.
  The grant stays in the Worker; the pipeline host only receives 5-minute
  tokens with each unit of work. Which host is still to be decided.
- **Improvements:** reliability, remediation quality, human-readable reports,
  and code health.

## Phases

Each phase ships on its own and leaves a working product.

### 1. Doorway delegation grants (cail-tools-admission)

Doorway's refresh-token grant for registered first-party Workers: 24-hour
grants, rotating refresh tokens with reuse detection, an Admission check at
every refresh, and 5-minute audience-bound tokens carrying `client_id`. People
see and revoke running work on the Account Workspace settings page. Contract:
`apps/doorway/docs/DELEGATION-GRANTS.md` in cail-tools-admission. Media Tools,
Agent Studio, and Site Studio move onto it first; PDF Accessibility registers
as a client when its Worker exists.

### 2. Gateway model lane (this repo)

1. **Evaluation first.** Commit a small, redistributable benchmark corpus and a
   scoring script (release-ready rate, veraPDF, fidelity, alt-text checks).
   Record the current Gemini baseline so the model change is judged on
   evidence.
2. **Gateway client.** Send page images (the existing local-semantic lane) to
   the Gateway's chat completions endpoint. Add image-based versions of the
   title/front-matter and TOC lanes. Cap image and request sizes. Remove the
   `LLM_API_KEY`→Gemini fallback and the "gemini" model-name validator.
3. **Per-job credential.** The Worker holds the job's grant and sends the
   pipeline a fresh 5-minute Gateway token with each unit of work; the
   pipeline never stores it. Revocation and `quota_exceeded` fail the job
   with a clear message.
4. **Bake-off.** Run the corpus through candidate Gateway vision models
   (qwen3-vl, kimi-k2.6, others in the live catalog) against the Gemini
   baseline. Choose the model on the measured results.

### 3. Worker version behind Doorway

- A Worker serves the app at `/pdf-accessibility` as a protected Doorway
  product with audience `cail:pdf-accessibility`, and registers as a
  delegation client for `cail:gateway`.
- Jobs, files, and results move to Cloudflare storage; the pipeline host
  receives work and returns results over an authenticated channel.
- Retire the NML deployment, the two no-script bypass Worker Routes, and
  their release reconciliation.
- Admission needs no product registration; add the app to the admin desk's
  fleet status.
- Production cutover needs Stephen's go-ahead after the PRs are green.

### 4. Reliability

- An overall job deadline, kept inside the 24-hour grant.
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

## Open decisions

- Pipeline host: actual-dell or the Lab's AWS account.
