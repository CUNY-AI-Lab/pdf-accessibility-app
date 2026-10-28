# PDF Accessibility

Read README.md and docs/cail-integration-plan.md (the plan of record) first.
The fleet rules in the CAIL workspace AGENTS.md apply.

- `scripts/check.sh` is the source gate: backend (ruff, pytest), frontend
  (eslint, build), and the Worker (`worker/`: oxlint with the anti-slop
  profile, types, build, workerd tests). Run it before pushing.
- The runner contract is defined twice, in `worker/src/contract.ts` and
  `backend/app/runner/`; change both sides in one change, and keep the
  Worker accepting the runner version deployed on actual-dell until it is
  replaced.
- The runner never holds a Gateway key. Its model calls go through the
  Worker's relay with the task's lease; do not add a direct Gateway path.
- Views the Worker serves are rendered by the runner with the backend's own
  response code. Do not re-implement pipeline or report logic in the Worker.
- The Worker releases only through `.github/workflows/worker-release.yml`;
  the runner deploys on actual-dell as `deploy/actual-dell/README.md`
  describes. Doorway's binding (cail-tools-admission) follows a verified
  Worker release.
- Vendored lint plugin: `worker/tools/oxlint/anti-slop/` (dmmulroy/anti-slop,
  see its UPSTREAM.md).
