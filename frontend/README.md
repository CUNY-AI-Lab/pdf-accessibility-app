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

- the backend assigns an anonymous HTTP-only browser session cookie
- the dashboard and job pages only show jobs created by that browser session
- clearing cookies or switching browser profiles means the user loses access to in-flight and completed jobs from the old session
- backend TTL cleanup deletes old jobs and files even if the browser still has a valid cookie

## Development

Run the dev server:

```bash
cd frontend
bun dev
```

Build production assets:

```bash
cd frontend
bun run build
```

The frontend expects the backend at `http://127.0.0.1:8001` during local development.

## Current review surface

The UI supports:
- figure-semantics QA controls to keep a decision, undo a decision, or retry a figure decision
- optional visible checks for generated alt text
- optional visible checks for annotation and link descriptions
- manual-remediation pages with current-PDF and report downloads

The frontend is intentionally not the source of truth for remediation logic. It renders output state and optional review context while the backend owns remediation, trust gating, and persistence.
