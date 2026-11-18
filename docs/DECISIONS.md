# Decisions

A short record of engineering decisions, newest last. Product and hosting
decisions made before this file existed are in
[cail-integration-plan.md](cail-integration-plan.md#decisions).

## 2026-11-18 — Engineering work on one branch, tracked in IMPROVEMENT_PLAN.md

The assessment's fixes land as separate commits on `gabe-prod-work`, each
ticked off in [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md), rather than as a
branch per item. One branch keeps the work reviewable in order while it is in
progress; it can still be split into PRs per item when it is merged.

## 2026-11-18 — The AWS fork stays reference only

`CUNY-AI-Lab/pdf-accessibility` (ASUCICREPO's AWS solution) does not serve the
live tool. Its review findings are kept in the appendix of IMPROVEMENT_PLAN.md
and no work is planned there.

## 2026-11-18 — The runner restarts itself instead of relying on an autoheal sidecar

Docker's `restart: unless-stopped` ignores health status, so an unhealthy
runner was never restarted. Rather than add an autoheal container (another
image with access to the Docker socket), the runner exits with an error when
the Worker refuses its token three times in a row or hasn't answered for
`RUNNER_CONTACT_TIMEOUT_SECONDS` (600 s, longer than a 3-minute lease, so no
held task is cut short). The existing restart policy then restarts it, and
the restarts are visible in `docker compose ps`.

## 2026-11-18 — Queued work times out on runner absence, not on queue age

A plain "queued too long" limit would fail jobs legitimately waiting behind
busy runners (12 global slots, 2 runner slots, jobs up to 3 hours). The real
failure is no runner at all. Every runner polls `claim` every few seconds,
busy or not, so the JobBoard remembers the last poll and fails queued work
once there has been none for 30 minutes. The time is kept in memory rather
than written on every poll; after an eviction it restarts at "now", which
can delay this failure but never cause a wrong one.
