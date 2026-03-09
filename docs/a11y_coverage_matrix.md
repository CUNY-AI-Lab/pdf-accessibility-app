# Accessibility Coverage Matrix

Updated: 2026-03-09

Sources:
- [WCAG 2.0 PDF Techniques](https://www.w3.org/TR/2014/NOTE-WCAG20-TECHS-20140311/pdf.html)
- [PublishingCG Accessibility Metadata Display Guidelines](https://www.w3.org/community/reports/publishingcg/CG-FINAL-a11y-display-guidelines-20251222/)

Status legend:
- `Covered`: implemented with current benchmark evidence
- `Partial`: implemented, but still dependent on document complexity or review
- `Missing`: not a first-class product area yet
- `N/A`: not in current scope

## WCAG PDF Techniques

| Area | Status | Evidence | Notes |
|---|---|---|---|
| Tagged PDF baseline | Covered | [backend/app/pipeline/tagger.py](../backend/app/pipeline/tagger.py) | The app writes a fresh structure tree, metadata, and ParentTree rather than trusting source tags. |
| Document language | Partial | [backend/app/pipeline/tagger.py](../backend/app/pipeline/tagger.py), [backend/app/pipeline/fidelity.py](../backend/app/pipeline/fidelity.py) | Document-level language is strong. Element-level language is present. Inline span language remains limited. |
| Document title + display title | Covered | [backend/app/pipeline/tagger.py](../backend/app/pipeline/tagger.py) | Title is written to Info/XMP and `DisplayDocTitle` is set. |
| Headings and lists | Covered | [backend/app/pipeline/tagger.py](../backend/app/pipeline/tagger.py), [backend/app/pipeline/structure.py](../backend/app/pipeline/structure.py) | Heading/list tagging is strong on current corpora. |
| Tables | Partial | [backend/app/pipeline/tagger.py](../backend/app/pipeline/tagger.py), [backend/app/pipeline/fidelity.py](../backend/app/pipeline/fidelity.py), [backend/app/services/intelligence_gemini_tables.py](../backend/app/services/intelligence_gemini_tables.py) | Table tagging is strong, but complex statistical tables still require semantic review. |
| Image alternative text | Partial | [backend/app/pipeline/alt_text.py](../backend/app/pipeline/alt_text.py), [backend/app/services/intelligence_gemini_figures.py](../backend/app/services/intelligence_gemini_figures.py) | Figure handling is much stronger now, including figure/table/form reclassification, but substantive figures can still require review. |
| Links and annotations | Covered | [backend/app/pipeline/tagger.py](../backend/app/pipeline/tagger.py), [backend/app/pipeline/fidelity.py](../backend/app/pipeline/fidelity.py) | Link/annotation association is strong; link-quality issues are surfaced for review. |
| Forms | Partial | [backend/app/services/form_fields.py](../backend/app/services/form_fields.py), [backend/app/services/intelligence_gemini_forms.py](../backend/app/services/intelligence_gemini_forms.py) | Official form set currently passes, but rich grouping/help semantics remain narrower than the best specialist tools. |
| OCR for scanned PDFs | Covered | [backend/app/pipeline/ocr.py](../backend/app/pipeline/ocr.py), [backend/app/pipeline/fidelity.py](../backend/app/pipeline/fidelity.py) | OCR and OCR-coverage gating are in place and proven on the scanned fixture set. |
| Reading order | Partial | [backend/app/services/intelligence_gemini_reading_order.py](../backend/app/services/intelligence_gemini_reading_order.py), [frontend/src/components/StructureEditor.tsx](../frontend/src/components/StructureEditor.tsx) | Good on many documents, still partial on dense multi-column layouts. |
| Decorative content as artifacts | Covered | [backend/app/pipeline/tagger.py](../backend/app/pipeline/tagger.py), [backend/app/services/intelligence_gemini_figures.py](../backend/app/services/intelligence_gemini_figures.py) | Decorative content is artifacted when confidence is high enough. |
| Visual WCAG checks (contrast, color-only cues) | Missing | - | Not yet a first-class audit layer. |

## Accessibility Metadata Display

| Area | Status | Evidence | Notes |
|---|---|---|---|
| Outcome summary in UI | Covered | [frontend/src/pages/JobDetailPage.tsx](../frontend/src/pages/JobDetailPage.tsx), [frontend/src/components/OutcomeHero.tsx](../frontend/src/components/OutcomeHero.tsx) | The UI clearly shows whether a job is release-ready. |
| Compliance provenance | Covered | [backend/app/pipeline/orchestrator.py](../backend/app/pipeline/orchestrator.py), [frontend/src/components/ValidationReport.tsx](../frontend/src/components/ValidationReport.tsx) | Validator name, timing, and report details are exposed. |
| Distinguish automated vs review-required decisions | Covered | [backend/app/pipeline/fidelity.py](../backend/app/pipeline/fidelity.py), [backend/app/api/review.py](../backend/app/api/review.py) | Review tasks are explicit and blocking/advisory is clear. |
| Structured benchmark/cost output | Covered | [backend/scripts/corpus_benchmark.py](../backend/scripts/corpus_benchmark.py), [backend/app/services/llm_client.py](../backend/app/services/llm_client.py) | Benchmark output now includes real OpenRouter cost data. |
| Search/catalog-facing accessibility metadata | Missing | - | No downstream discovery/catalog export model yet. |

## Current Product Position

The app is strongest on:
- PDF/UA structure and metadata
- font and Unicode remediation
- OCR and scanned-document handling
- semantic adjudication for difficult local regions
- release gating that combines compliance, fidelity, and review status

The app is still partial on:
- complex table understanding
- advanced form semantics beyond labels
- reading order on visually dense layouts
- broad visual WCAG auditing
