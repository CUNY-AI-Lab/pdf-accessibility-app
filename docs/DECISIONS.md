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
