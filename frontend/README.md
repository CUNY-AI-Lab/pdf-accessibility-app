# Frontend

The frontend is a React + TypeScript application that exposes:

- upload and job tracking
- outcome and validation reporting
- optional advanced review for visible changes and checks
- manual remediation reporting when a run stops short of a trustworthy output

## Main pages

- [src/pages/DashboardPage.tsx](src/pages/DashboardPage.tsx)
- [src/pages/UploadPage.tsx](src/pages/UploadPage.tsx)
- [src/pages/JobDetailPage.tsx](src/pages/JobDetailPage.tsx)
- [src/pages/ReviewPage.tsx](src/pages/ReviewPage.tsx)

## Important components

- [src/components/OutcomeHero.tsx](src/components/OutcomeHero.tsx)
- [src/components/RemediationSummary.tsx](src/components/RemediationSummary.tsx)
- [src/components/ValidationReport.tsx](src/components/ValidationReport.tsx)
- [src/components/AppliedChangeCard.tsx](src/components/AppliedChangeCard.tsx)
- [src/components/ReviewTaskCard.tsx](src/components/ReviewTaskCard.tsx)
- [src/components/PipelineProgress.tsx](src/components/PipelineProgress.tsx)

## UI model

The UI is organized around three questions:

1. did the run finish `complete`, `manual_remediation`, or `failed`?
2. what did the app change automatically?
3. do I want to spot-check any visible items?

That is why the frontend separates:
- outcome summary
- technical report
- optional visible follow-up

The frontend intentionally does not surface PDF-structural mechanics. Reading order internals, table header indexing, font repair, widget cleanup, and similar pipeline decisions stay system-owned and off-screen.

## Session Behavior

The frontend does not implement login, account management, or browser-side PDF persistence.

In v2, Doorway signs the person in before the app loads, and the Worker's API
returns only the signed-in person's jobs. Jobs and files are deleted 12 hours after
their last change.

## Development

The full local setup is the Worker/runner harness in
[../worker/README.md](../worker/README.md), `worker/dev/e2e.mjs`. It starts
the dev Worker on port 8791 and signs temporary identities for the test.
For UI iteration against that Worker, run the Vite server:

```bash
cd frontend
bun dev
```

Build production assets:

```bash
cd frontend
bun run build
```

Run lint, state tests and the production build with `bun run check`. Tests use
[Vitest](https://vitest.dev/guide/environment) with pure transition tests and
jsdom for DOM interactions. `bun run test:mutations` temporarily breaks each main
guard, requires its named test to fail, and restores the source in cleanup. Run
mutation checks separately from other checks to avoid testing modified files.

Vite proxies API and health requests to the dev Worker at
`http://127.0.0.1:8791/pdf-accessibility`. `VITE_DEV_PROXY_TARGET` overrides
the Worker origin. Requests with `VITE_APP_BASE_PATH=/pdf-accessibility/`
preserve that prefix; root-path development adds the Worker prefix. The
Worker still requires signed development identities for API access. For Vite
write requests, set the dev Worker's `CAIL_CANONICAL_ORIGIN` to the Vite origin.

## Current review surface

The UI supports:
- figure-semantics QA controls to keep a decision, undo a decision, or retry a figure decision
- optional visible checks for generated alt text
- optional visible checks for annotation and link descriptions
- manual-remediation pages with current-PDF and report downloads

The frontend is intentionally not the source of truth for remediation logic. It renders output state and optional review context while the backend owns remediation, trust gating, and persistence.
