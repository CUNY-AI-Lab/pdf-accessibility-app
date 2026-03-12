# Accessibility Coverage

Updated: 2026-03-12

This document describes what the app currently covers, what is still partial, and what remains outside the product today.

The important distinction is:

- `compliant` means the output passes `veraPDF` for PDF/UA-1
- `accessible enough to release` means compliant, fidelity-passed, and not forced into `manual_remediation` by unresolved blocking conditions

## Strong Coverage

### PDF/UA structure and metadata

The app writes a fresh accessible PDF rather than trying to preserve an arbitrary source tag tree.

Current strong areas:
- document title, language, metadata, and PDF/UA identification
- structure tree and ParentTree
- headings, paragraphs, lists, links, figures, notes, code blocks, formulas when detected
- link and annotation OBJR association
- bookmarks
- TOC output for detected contents sections
- form widgets tagged as form content

Primary implementation:
- [backend/app/pipeline/tagger.py](backend/app/pipeline/tagger.py)
- [backend/app/pipeline/orchestrator.py](backend/app/pipeline/orchestrator.py)

### Font remediation and text accessibility

The app has real automatic and LLM-guided font repair paths.

Covered areas include:
- embedding missing fonts
- repairing or rebuilding Unicode mapping paths
- CIDSet/CIDToGIDMap repair
- `ActualText` and targeted symbol mapping where needed
- grounding suspicious text against native extraction, OCR, and page imagery

Primary implementation:
- [backend/app/pipeline/orchestrator.py](backend/app/pipeline/orchestrator.py)
- [backend/app/services/page_intelligence.py](backend/app/services/page_intelligence.py)
- [backend/app/services/intelligence_gemini_pages.py](backend/app/services/intelligence_gemini_pages.py)

### Semantic adjudication for hard regions

Hard semantic decisions are now grounded against multiple evidence sources and routed through Gemini structured outputs.

Current semantic-unit families:
- suspicious text blocks
- tables
- forms
- figures
- TOC groups
- complex reading-order pages

This is the main product differentiator: the app no longer treats all semantic ambiguity as a pure heuristic problem.

Primary implementation:
- [backend/app/services/semantic_units.py](backend/app/services/semantic_units.py)
- [backend/app/services/intelligence_gemini_semantics.py](backend/app/services/intelligence_gemini_semantics.py)

### Forms

The app now has real form-label support.

Covered today:
- widget tagging
- `/TU` writing for risky fields
- grouped field detection and label generation
- high-confidence Gemini-assisted label generation

Evidence:
- official form acceptance set: [backend/data/benchmarks/corpus_20260309_123540/corpus_report.md](backend/data/benchmarks/corpus_20260309_123540/corpus_report.md)

### OCR and scanned documents

Covered today:
- OCRmyPDF integration
- page rotation and deskew
- OCR rescue where needed
- OCR coverage check in fidelity
- local crop OCR grounding for suspicious text

Evidence:
- scanned fixture sweep: [backend/data/benchmarks/scanned_fixture_corpus_20260307_rerun/workflow.sqlite3](backend/data/benchmarks/scanned_fixture_corpus_20260307_rerun/workflow.sqlite3)

## Partial Coverage

### Complex tables

The app is honest about table risk now.

What is strong:
- table extraction and tagging
- header rows and row-header columns
- table risk detection
- Gemini-first table interpretation for risky tables

What is still partial:
- grouped headers beyond simple header band modeling
- logical splitting of visually dense statistical tables
- full accessibility confidence on large multi-level tables, which can still force manual remediation on hard cases

### Reading order on complex layouts

What is strong:
- page/block-level semantic adjudication
- Gemini reading-order decisions on hard pages
- deterministic structure apply paths before tagging

What is still partial:
- multi-column pages with dense sidebars or callouts
- some dense layouts still depend on upstream extraction quality and fidelity backstops

### Figure semantics

What is strong:
- figure vs non-figure reclassification
- decorative vs meaningful decisions
- caption-backed alt text
- optional visible checks for generated descriptions when extra confidence is useful

What is still partial:
- substantive uncaptioned charts and diagrams still need stronger summaries or manual remediation more often than we would like
- figure-heavy guides remain a cost outlier class

### Math and formula semantics

The app can conservatively detect formulas, tag them as `/Formula`, preserve raw formula text, and generate speakable formula alt text, but it does not yet provide a rich math semantics stack.

### Link quality

The app now detects weak link text and broken internal destinations, but it does not rewrite arbitrary visible link text.

## Not Yet Covered As A First-Class Product Area

### Visual WCAG audits

The app does not yet run a dedicated visual accessibility audit for:
- color contrast
- color-only meaning
- weak visual distinction of links
- text over complex backgrounds

### Rich media

No full support yet for:
- embedded audio/video semantics
- captions or transcripts
- multimedia review workflows

### Inline language shifts

Document-level and many element-level language decisions are covered, but inline language span modeling is still limited.

### Digital signatures

Remediation changes invalidate existing signatures. Re-signing is not handled.

## Release Standard

A document is release-ready only if all are true:

1. `veraPDF` says compliant
2. fidelity says faithful enough
3. the run ends `complete`, not `manual_remediation`

Optional visible review items do not block release. That is stricter than validator pass alone because hidden structural blockers still force manual remediation.

## Current Evidence Snapshot

- exact curated corpus: [backend/data/benchmarks/corpus_20260308_202258/corpus_report.md](backend/data/benchmarks/corpus_20260308_202258/corpus_report.md)
  - `25 / 25` successful outputs release-ready
- representative non-huge corpus: [backend/data/benchmarks/corpus_20260311_121723/corpus_report.md](backend/data/benchmarks/corpus_20260311_121723/corpus_report.md)
  - `7 / 7` release-ready
- official form set: [backend/data/benchmarks/corpus_20260309_123540/corpus_report.md](backend/data/benchmarks/corpus_20260309_123540/corpus_report.md)
  - `7 / 7` release-ready
