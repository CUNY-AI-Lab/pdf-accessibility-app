# PDF Accessibility

Read README.md and docs/cail-integration-plan.md (the plan of record) first.
The fleet rules in the CAIL workspace AGENTS.md apply.

- `scripts/check.sh` is the source gate: backend (ruff, pyright, pytest), frontend
  (eslint, build), and the Worker (`worker/`: oxlint with the anti-slop
  profile, types, build, workerd tests). Run it before pushing.
- The runner contract is defined twice, in `worker/src/contract.ts` and
  `backend/app/runner/`; change both sides in one change, and keep the
  Worker accepting the runner version deployed on actual-dell until it is
  replaced.
- The runner never holds a Gateway key. Its model calls go through the
  Worker's relay with the task's lease; do not add a direct Gateway path.
- The backend is an in-process pipeline, with no HTTP server. Local app
  development uses `worker/dev/e2e.mjs` (see `worker/README.md`). Keep shared
  response mapping in `services/job_views.py`, review operations in
  `services/review_actions.py`, and progress interfaces in
  `services/pipeline_progress.py`.
- PDFium allows one call at a time per process. `backend/app/pdfium_guard.py`,
  installed by `app/__init__.py`, serializes `pypdfium2.raw` and pypdfium2's
  close path. Import the app before opening a PDF, never call `pypdfium2_raw`
  directly, and hold `PDFIUM_LOCK` across a page in per-character loops.
  Code a finalizer or `__del__` runs uses `try_acquire` and `defer`, never
  `acquire`. After a pypdfium2 upgrade, `tests/test_pdfium_guard.py` must
  still pass.
- Views the Worker serves are rendered by the runner with the backend's own
  response code. Do not re-implement pipeline or report logic in the Worker.
- The Worker releases only through `.github/workflows/worker-release.yml`;
  the runner deploys on actual-dell as `deploy/actual-dell/README.md`
  describes. Doorway's binding (cail-tools-admission) follows a verified
  Worker release.
- Vendored lint plugin: `worker/tools/oxlint/anti-slop/` (dmmulroy/anti-slop,
  see its UPSTREAM.md).
