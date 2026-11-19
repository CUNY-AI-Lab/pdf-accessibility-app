# Engineering assessment and improvement plan

Status: in progress on branch `gabe-prod-work` (started 2026-11-18). Each item
below is fixed in its own commit, test first, with `scripts/check.sh` green
before the commit. Tick an item off here in the same commit that fixes it.
Decisions taken along the way are recorded in [DECISIONS.md](DECISIONS.md).

[cail-integration-plan.md](cail-integration-plan.md) remains the plan of record
for the product. This document covers engineering quality: resilience,
security, testing and delivery.

## Scope

| Repository | Role |
|---|---|
| `CUNY-AI-Lab/pdf-accessibility-app` (this one) | The live v2 tool at `tools.ailab.gc.cuny.edu/pdf-accessibility/`. All work here. |
| `CUNY-AI-Lab/pdf-accessibility-eval-data` | Evaluation corpus sources. Used by item 14. |
| `CUNY-AI-Lab/pdf-accessibility` | A mirror of ASUCICREPO's AWS solution. Not deployed; kept for reference only (see the appendix). |

## Summary

The app is in good shape for its size. The CI source gate runs ruff, pyright,
pytest, eslint, vitest, oxlint and workerd tests. Actions are pinned by SHA and
lockfiles are frozen. Jobs, tasks, uploads and artifacts are explicit state
machines with idempotent completion and immutable revisions. Retention is
enforced twice (12 h in the Durable Object, a 2-day R2 rule checked before each
release). The runner token is compared in constant time and only lease hashes
are stored. The container runs as non-root under tini with veraPDF
GPG-verified. Worker releases refuse a stale `main` and roll back on failure.

The gaps are in four areas:

1. **Failures nobody notices.** The health check reports healthy while the
   runner can't take work; restarts and transient Worker errors fail jobs that
   should have succeeded; the sweep and stuck threads can stall the queue.
2. **Isolation of the runner host.** One static token, beside untrusted PDF
   parsers, in a host-networked container with no hardening.
3. **Tests across component boundaries.** No contract test between the Worker
   and the runner, no end-to-end test, and the real PDF tools never run in CI.
4. **Breadth of the gates.** Narrow lint rules, no coverage, no accessibility
   checks on the app's own UI, no dependency or code scanning.

## Workstreams

Priority: P0 = fix first (a live failure mode), P1 = next, P2 = as time allows.

### P0 — resilience

- [x] **1. A health check that tells the truth.**
  `_stay_alive` (`backend/app/runner/__main__.py`) touches the health file on
  a timer, whatever the claim loop is doing. A revoked token or an unreachable
  Worker loops on "Claim failed" every 30 s and the container stays healthy;
  `restart: unless-stopped` never acts on "unhealthy" anyway. Fix: touch the
  file only after a claim round-trip succeeds, and exit non-zero after
  repeated authentication refusals so Docker restarts the runner and the
  failure is visible. The CI smoke test should expect "unhealthy" against its
  dead endpoint.
  *Done:* the health file is touched only while the Worker answers (claims or
  lease renewals); three refused claims in a row, or 10 minutes without an
  answer, exit with an error. CI now runs the image against a stub Worker
  (must turn healthy) and a dead one (must exit non-zero). Verified locally
  by unit tests and by running the runner against both; the image itself
  could not be built in the sandbox (Debian mirrors blocked); the CI
  container job passed on this branch (run 44400133030).
- [x] **2. Graceful drain on SIGTERM.** Shutdown cancels in-flight jobs at
  once. Each then waits out its 3-minute lease and spends one of its two
  attempts (`worker/src/board.ts` `LIMITS.attempts`), so two restarts during a
  job fail it. Fix: stop claiming, let the runner hand unfinished tasks back
  through a new release call that requeues without spending an attempt, and
  lengthen `stop_grace_period`. Contract change on both sides.
  *Done:* the Worker has `POST /tasks/{id}/release` (lease-authorized);
  it requeues the task and returns the attempt its claim counted. On SIGTERM
  the runner waits up to `RUNNER_DRAIN_SECONDS` (40) for tasks to finish,
  then cancels and releases the rest before joining them; compose's
  `stop_grace_period` is 60 s. Release order: Worker first (an older Worker
  answers 404, which the runner treats as an already-lost lease).
- [x] **3. Retry the Worker calls that don't.** Only `upload` retries.
  `download`, `progress`, `complete` and `fail` (`backend/app/runner/client.py`)
  are single attempts, so a transient 502 on `complete` after every file is
  uploaded turns a finished job into `INTERNAL_ERROR`. Fix: the same bounded
  retry as upload, within the lease.
  *Done:* download, progress, complete and fail retry 408/425/429/5xx and
  transport errors three times with 2 s and 4 s backoff, within the task's
  deadline (the failure report ignores the deadline, since it may be what
  reports it). Safe to replay: completion has a per-lease receipt; a replayed
  failure report after a lost answer gets 409 and ends the task as lease lost.
- [x] **4. A sweep that can't be stopped by one bad row, and a queue that
  can't fill with dead jobs.** `sweep` (`worker/src/board.ts`) has no per-row
  isolation, unlike `claim`, so one throwing row stops lease recovery and
  retention deletion for everyone. It only looks at claimed rows: when the
  runner is down, queued jobs wait silently until retention deletes them, and
  twelve of them make every new upload fail with 503. Fix: isolate each row,
  and fail queued jobs that wait longer than a set limit with a clear message.
  *Done:* each job's sweep work runs in its own transaction, and a row that
  throws is retired as inconsistent (as at claim); upload and retention
  deletions are isolated per row too; the alarm re-arms in a `finally`.
  Queued work fails only when no runner has polled for 30 minutes, since a
  busy runner still polls and a long queue behind it is legitimate.
- [x] **5. Work that can be stopped.** `owned_thread_work`
  (`backend/app/services/thread_work.py`) waits for threads with no limit, and
  several synchronous `subprocess.run` calls bypass the process-group helper in
  `pipeline/subprocess_utils.py`; two have no timeout at all
  (`pipeline/language.py`, `services/runtime_diagnostics.py`). A PDF that hangs
  a parser holds a runner slot forever. Fix: timeouts and process-group kills
  everywhere; a thread that outlives its deadline fails the task and restarts
  the runner.
  *Done:* `run_bounded` (in `pipeline/subprocess_utils.py`) is the only way
  synchronous code starts a child process: a required timeout, its own
  process group, and the whole group killed on timeout or interruption. A
  test fails if `subprocess.run`/`Popen` appears anywhere else in `app/`.
  A task's threads get `RUNNER_THREAD_JOIN_SECONDS` (120) after it ends;
  past that the runner drains, releases the other slot's task and exits
  non-zero (via `os._exit`, since a stuck thread would otherwise hold the
  interpreter's shutdown).

### P0/P1 — security

- [ ] **6. Harden the runner container** (`deploy/actual-dell/compose.yml`):
  drop all capabilities, `no-new-privileges`, read-only root with tmpfs,
  `pids_limit`, a JVM heap cap for veraPDF, log rotation, and a minimum length
  for `RUNNER_TOKEN` in the Worker.
  *Part done:* compose drops all capabilities, sets `no-new-privileges`,
  `pids_limit: 1024` and log rotation; CI runs the smoke test and the PDF
  tools (Ghostscript, qpdf, OCRmyPDF, and veraPDF through the app's
  validator) under the same flags. The JVM already caps its default heap at
  25% of the container limit, so no flag was added.
  *Open:* a read-only root (OCRmyPDF's temp space, and tmpfs counting
  against `mem_limit`), a bridge network instead of `network_mode: host`
  (docling-serve's binding on the host), both to test on actual-dell; and a
  minimum `RUNNER_TOKEN` length in the Worker, which needs the production
  token's length confirmed first or the next release could lock the runner
  out.
  *Token length, step 1 done:* the token can't be read from outside (Cloudflare
  secrets are write-only; `runner.env` is on actual-dell), so the deployed
  code reports it: the Worker logs `runner_token_weak` (length only) once
  per isolate and the runner logs a start-up warning when it is under 32
  characters. Nothing is refused yet.
  *Step 2, after the next release of both:* if neither reports a weak
  token, make `sameSecret` refuse an expected token under
  `MIN_RUNNER_TOKEN_LENGTH` and make the runner exit at start-up on one,
  each with a test. If either reports one, rotate first
  (`openssl rand -base64 32`, Worker secret, then `runner.env`, then restart
  the runner), then enforce.
- [x] **7. Close preflight gaps** (`backend/app/services/pdf_preflight.py`):
  bound decompressed stream size, count inline images, annotation appearances,
  soft masks and inherited resources, honour `/UserUnit`, and cap object count.
  *Done:* non-image Flate streams are decoded incrementally and refused past
  256 MB each or 2 GB together; more than 1,000,000 objects is refused; soft
  masks and images in annotation appearances count toward the image limits
  (as new report fields, so classification is unchanged); `/UserUnit` scales
  the page area. Inherited resources were already counted (pikepdf resolves
  them), so nothing changed there. Same verdicts as before on all 79
  evaluation-corpus PDFs; slowest preflight 1.2 s.
  *Open:* inline images (needs a content-stream parse), Flate bombs inside
  image streams or object streams, and running preflight in a
  memory-limited subprocess.
- [ ] **8. Limit the relay and what the Worker serves**
  (`worker/src/runner-api.ts`, `worker/src/user-api.ts`): body size, token and
  call limits on the model relay; `nosniff`, a content-type allowlist and a
  CSP on HTML reports; a per-person cap on uploads in flight; accept the
  `%PDF-` header anywhere in the first 1024 bytes, as the PDF spec allows.
  *Part done:* every runner file is served with `nosniff`; the route, not the
  stored metadata, decides its type (HTML only for `report.html` and
  `document.html`, images only as PNG/JPEG, otherwise octet-stream); HTML
  reports and the batch report get the locked-down CSP `document.html` had;
  relayed model requests over 16 MiB are refused (bodies without a declared
  length are read under the same limit, so a proxy that drops the header
  can't stop processing).
  *Open:* a per-person cap on uploads in flight (the `uploads` table has no
  owner, so it needs a Durable Object migration); `max_tokens`, call-count
  and model limits on the relay (need the runner's real request profile
  first); a `%PDF-` header after byte 0 (needs checking that every pipeline
  tool accepts leading bytes).

### P1 — tests and gates

- [ ] **9. A runner contract test.** Publish the Worker's runner contract as
  JSON Schema, validate both sides' payloads against it in their tests, fail
  CI when it is stale, and send a runner version the Worker can check.
- [ ] **10. A hermetic end-to-end test in CI**: workerd, the runner image, the
  Gateway double from `worker/test`, and a canned Docling, processing one PDF.
- [ ] **11. Run the real PDF tools in CI**: an `integration` pytest marker run
  inside the built image, so veraPDF, Ghostscript, qpdf and OCRmyPDF execute.
- [ ] **12. Broaden the gates by ratchet**: more ruff rule families (bugbear,
  blind-except, async), `ruff format --check`, measured coverage that may not
  fall, two more pyright checks, and fixes for time-dependent tests and for
  failures reported as empty results (`services/text_grounding.py`).
- [ ] **13. Check the app's own accessibility**: jsx-a11y lint, axe checks on
  each page, tests for the untested pages, the existing mutation check in
  `bun run check`, and an error boundary per route.
- [ ] **14. An evaluation regression gate**: a weekly and on-demand workflow
  that runs a fixed subset of the corpus and fails below committed baselines.

### P1 — delivery

- [ ] **15. Container publishing**: pin actions by SHA, require CI, emit SBOM
  and provenance, scan the image, pin the base image by digest.
- [ ] **16. Dependabot** (with the Lab's seven-day delay) **and CodeQL**.
- [ ] **17. Worker release**: a protected GitHub environment and a smoke test
  after promotion.
- [ ] **18. Runner deploy**: a script for the actual-dell deploy with a health
  wait and rollback, reporting the deployed version to the Worker.

### P2 — operations and code health

- [ ] **19. Alerting** on queue depth, time since the last claim and failure
  rate; logs shipped off actual-dell.
- [ ] **20. Document actual-dell as a single point of failure**; a second
  runner is a cheap follow-up since runners pull work and hold no state.
- [ ] **21. Code health**: split `pipeline/orchestrator.py` and
  `pipeline/fidelity.py` along their lanes, move benchmark-only modules out of
  `app`, and add direct tests for the large `tagger_*` modules.

## Working rules for this branch

- One commit per item (or per coherent part of one), with a failing test first
  for every behaviour change.
- `scripts/check.sh` passes before each commit; anything not run or not
  verifiable here (actual-dell, Cloudflare and GitHub settings) is stated in
  the commit message and under the item.
- The runner contract changes in both `worker/src/contract.ts` and
  `backend/app/runner/` in the same commit, and the Worker keeps accepting the
  runner version deployed on actual-dell.
- New dependencies are agreed before they are added.

## Appendix: the AWS fork (`CUNY-AI-Lab/pdf-accessibility`)

Reviewed 2026-11-18 for reference; nothing is planned there. It is a mirror of
ASUCICREPO/PDF_Accessibility (Adobe Auto-Tag, Step Functions, ECS, Bedrock).
If it is ever deployed, these must be fixed first:

- `deploy.sh` builds upstream's `main` (`ASUCICREPO/PDF_Accessibility`), not
  the fork or a pinned commit, and its CodeBuild role can escalate to admin.
- No Step Functions state has a retry or catch; one throttled call fails the
  document. Several components (Java merger, title, checker Lambdas, splitter)
  report failures as success, so errors surface late or not at all.
- The autotag container can silently drop all alt text for a chunk.
- Images use `:latest`; the deployed merger is a committed jar never rebuilt
  from source; dependencies are unpinned; Node 18 is end-of-life.
- Document text is logged; the versioned bucket has no lifecycle rule on
  intermediate files.
- There are no tests and no CI.
