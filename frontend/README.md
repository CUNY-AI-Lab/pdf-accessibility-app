# Frontend

The frontend is a React + TypeScript application that exposes:

- upload and job tracking
- outcome and validation reporting
- review workflows for unresolved semantics
- structure editing for reading order and table/header adjustments

## Main pages

- [src/pages/DashboardPage.tsx](src/pages/DashboardPage.tsx)
- [src/pages/UploadPage.tsx](src/pages/UploadPage.tsx)
- [src/pages/JobDetailPage.tsx](src/pages/JobDetailPage.tsx)
- [src/pages/ReviewPage.tsx](src/pages/ReviewPage.tsx)

## Important components

- [src/components/OutcomeHero.tsx](src/components/OutcomeHero.tsx)
- [src/components/ValidationReport.tsx](src/components/ValidationReport.tsx)
- [src/components/ReviewTaskCard.tsx](src/components/ReviewTaskCard.tsx)
- [src/components/StructureEditor.tsx](src/components/StructureEditor.tsx)
- [src/components/FontTargetPanel.tsx](src/components/FontTargetPanel.tsx)

## UI model

The UI is organized around three questions:

1. is the PDF release-ready?
2. what did the app change automatically?
3. what still requires review?

That is why the frontend separates:
- outcome summary
- technical report
- targeted review tasks

## Development

Run the dev server:

```bash
cd /Users/stephenzweibel/Apps/pdf-accessibility-app/frontend
bun dev
```

Build production assets:

```bash
cd /Users/stephenzweibel/Apps/pdf-accessibility-app/frontend
bun run build
```

The frontend expects the backend at `http://127.0.0.1:8001` during local development.

## Current review capabilities

The UI supports:
- reading-order editing
- table-by-table review cards
- alt-text review
- form semantics review tasks
- font-text review context
- Gemini-backed review suggestions with deterministic apply paths

The frontend is intentionally not the source of truth for remediation logic. It renders review context and sends constrained edits back to the backend.
