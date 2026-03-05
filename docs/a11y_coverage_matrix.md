# Accessibility Coverage Matrix

Updated: 2026-03-05

Sources:
- https://www.w3.org/TR/2014/NOTE-WCAG20-TECHS-20140311/pdf.html
- https://www.w3.org/community/reports/publishingcg/CG-FINAL-a11y-display-guidelines-20251222/

Status legend:
- Covered: implemented end-to-end in current app output/UI.
- Partial: implemented in part, or only for some content.
- Missing: not implemented.
- N/A: not applicable for this product scope.

## WCAG 2.0 PDF Techniques (2014 Note)

| Area | Status | Evidence | Notes |
|---|---|---|---|
| Tagged PDF baseline (`/MarkInfo`, structure tree) | Covered | `backend/app/pipeline/tagger.py` | Sets `/MarkInfo`, builds `StructTreeRoot`, parent tree, MCIDs. |
| Document language | Covered | `backend/app/pipeline/tagger.py` | Sets root `/Lang`. |
| Document title + display title | Covered | `backend/app/pipeline/tagger.py` | Sets Info + XMP title and `/DisplayDocTitle`. |
| Heading/list/table semantics | Partial | `backend/app/pipeline/tagger.py`, `backend/app/pipeline/structure.py` | Headings/lists/tables emitted; table header association beyond TH/TD remains limited. |
| Image alternative text | Partial | `backend/app/pipeline/orchestrator.py`, `backend/app/pipeline/tagger.py`, `frontend/src/components/AltTextEditor.tsx` | Reviewed alt text emitted; decorative images now artifacted. |
| Link tagging / annotation association | Partial | `backend/app/pipeline/tagger.py` | `/Link` StructElem + OBJR association added for existing link annotations. |
| Artifact handling for non-content | Partial | `backend/app/pipeline/tagger.py`, `backend/app/pipeline/structure.py` | Artifacts are emitted, but detection quality depends on upstream structure extraction. |
| Validation loop | Covered | `backend/app/pipeline/validator.py`, `backend/app/pipeline/orchestrator.py` | veraPDF (or fallback) integrated into pipeline output. |
| Forms, complex math, rich media specifics | Missing | - | No dedicated form field tagging or rich media semantics yet. |

## PublishingCG Accessibility Metadata Display Guidelines (2025 Report)

| Area | Status | Evidence | Notes |
|---|---|---|---|
| Display of accessibility conformance summary | Partial | `frontend/src/pages/JobDetailPage.tsx`, `frontend/src/components/ValidationReport.tsx` | UI now shows standard/profile, validator, and compliance state. |
| Display of machine validation provenance | Covered | `backend/app/pipeline/orchestrator.py`, `backend/app/api/documents.py` | Report includes validator name, profile, and generated timestamp. |
| Distinguish automated vs manual claims | Partial | `backend/app/pipeline/orchestrator.py`, `frontend/src/pages/JobDetailPage.tsx` | Report includes claims flags and UI caveat text. |
| Structured accessibility metadata endpoint | Partial | `backend/app/api/documents.py` | JSON report endpoint exists; dedicated metadata schema endpoint not separated yet. |
| Rich display patterns (badges, faceting, discoverability metadata) | Missing | - | No catalog/search-facing accessibility metadata model yet. |

## Next Priority Targets

1. Add explicit table header association attributes where possible.
2. Improve link text-level tagging (not only annotation OBJR mapping).
3. Add dedicated accessibility metadata endpoint and stable schema for downstream catalog systems.
4. Add form field tagging and validation for common AcroForm patterns.
