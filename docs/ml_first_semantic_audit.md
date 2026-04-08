# ML-First Semantic Audit

This audit separates acceptable deterministic logic from places where the app is still making semantic decisions locally instead of relying on Docling evidence and LLM judgment.

## Current target architecture

This is the current direction for the app's semantic pipeline and bookmark/navigation work.

- Use direct Gemini for PDF-understanding lanes, not a proxy layer, whenever the task depends on document-native evidence.
- Upload or reuse bounded PDF slices through Gemini's file/document path and context caching when the same slice will support multiple semantic questions.
- Use Gemini native structured output with JSON Schema instead of prompt-only schema descriptions.
- Give the model grounded candidate inventories with stable `candidate_id` values and source provenance, then let the model adjudicate keep/drop, label, level, and parent relationships.
- Split long-document work by coherent slices or candidate groups when recall depends on many specific items; use caching to preserve economics across those follow-up calls.
- Keep deterministic code limited to evidence gathering, candidate IDs, schema validation, dedupe, safety bounds, audit logging, and PDF writing.

This means the app should not drift back toward:

- prompt language that encodes benchmark-shaped navigation policy
- local ranking rules for which bookmarks "feel" useful
- fuzzy post-hoc interpretation of model labels when the model could have selected a grounded candidate ID directly
- whole-document freeform summarization when the task really needs exhaustive retrieval over many specific candidates

The standard:

- Extraction should gather raw document evidence.
- Models should decide ambiguous semantics.
- Deterministic code should validate, bound, and write outputs.
- Local rules should not decide titles, TOC membership, bookmark structure, link labels, field labels, or language when the evidence is ambiguous.

## Acceptable deterministic logic

These are not the current concern:

- PDF parsing and writing in [`tagger.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/pipeline/tagger.py)
- schema validation and retry handling in [`intelligence_llm_utils.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/intelligence_llm_utils.py)
- BCP-47 normalization and OCR language mapping in [`language.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/pipeline/language.py)
- round-trip comparison logic in [`roundtrip_compare.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/roundtrip_compare.py), except where it accidentally encodes product expectations

## High-risk semantic heuristics

### Structure extraction

[`structure.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/pipeline/structure.py) still makes several semantic calls locally:

- `_mark_toc_sequences()`
  - decides that a heading is a TOC caption and that following elements are TOC items
- `_looks_like_toc_entry()`, `_looks_like_toc_table()`, `_looks_like_toc_group_heading()`, `_looks_like_structured_toc_section()`
  - regex and shape logic deciding TOC semantics
- `_rebuild_toc_with_docling_parse()`
  - useful evidence path, but still wrapped in local TOC classification rules
- `_infer_heading_levels()`
  - bbox-height clustering decides heading hierarchy when Docling is incomplete
- `_collapse_spaced_title_caps()`
  - local normalization decides how title fragments should be collapsed
- `_infer_document_language()`
  - local logic guesses a document language from extracted content

These are the main structure-stage semantic rule layers.

### Title selection

[`title_intelligence.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/title_intelligence.py) is model-assisted, but the evidence selection still encodes rules:

- `TITLE_TEXT_TYPES`
  - only a fixed subset of element types can ever become title evidence
- `_title_candidate_elements()`
  - only page 1 is considered
  - candidates are capped at 16
- `existing_title` short-circuit via `_collapse_spaced_title_caps()`
  - if a title already exists, local cleanup wins before the model sees alternatives

The model is deciding among a pre-curated candidate set rather than inspecting the fullest available Docling evidence.

### TOC intelligence

[`toc_intelligence.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/toc_intelligence.py) is also model-assisted but heavily shaped by local rules:

- `TOC_HEADING_TEXTS`
  - a hardcoded heading-name gate for TOC discovery
- `MAX_TOC_GROUPS`, `MAX_TOC_PAGES`, `TOC_CHUNK_SIZE`
  - practical bounds, but they also decide what evidence the model sees
- `TOC_ALLOWED_ENTRY_TYPES`
  - fixed element-type gate before the model judges TOC membership
- `collect_toc_candidates()`
  - locally decides when a candidate group is a TOC candidate
- `_merge_toc_group_intelligence()`
  - local merging reconciles chunk-level model outputs
- `TOC_AUTO_CONFIDENCE`
  - local confidence gate determines whether model output changes structure automatically

This subsystem is not rule-free; it is a local candidate generator plus LLM adjudicator.

### Bookmark intelligence

[`bookmark_intelligence.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/bookmark_intelligence.py) remains the densest semantic shaping layer:

- `BOOKMARK_HEADING_TYPES`, `BOOKMARK_LANDMARK_TYPES`
  - fixed type gates for what can become a bookmark candidate
- `_clean_bookmark_label()`, `_bookmark_section_key()`, `_candidate_merge_key()`
  - local normalization and section-key logic influence candidate identity
- `_select_heading_target()`
  - locally maps TOC entries to headings before the model sees them
- `_front_matter_page_candidates()` and `BOOKMARK_FRONT_MATTER_SCHEMA`
  - hardcoded front-matter role families
- `_build_landmark_candidate_chunks()`
  - local span logic decides which non-heading blocks are even eligible together
- `_generate_bookmark_outline_plan()`
  - local required-vs-optional candidate classes constrain the final outline

This path is now more ML-driven than before, but it is still a model operating inside a locally constructed navigation grammar.

### Pretag auto-apply policy

[`semantic_pretag_policy.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/semantic_pretag_policy.py) contains rule-based semantic gating:

- `PRETAG_TABLE_ALLOWED_ACTIONS`
- `PRETAG_FORM_ALLOWED_TYPES`
- `PRETAG_WIDGET_RATIONALIZATION_ALLOWED_TYPES`
- geometry-based nearby-field logic in `form_targets_for_intelligence()`
- explicit allowlists for retries in `should_retry_table_intelligence_*()`

This module decides when model output is trusted enough to mutate structure automatically.

### Grounded text auto-apply

[`grounded_text_apply.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/grounded_text_apply.py) is strongly heuristic:

- role allowlists
- char-count limits
- similarity thresholds
- code-shape detection
- artifacting rules
- neighbor-duplication rules

This module locally decides when text repair is safe, instead of using a model-backed adjudication with post-checks.

### Tagging fallbacks

[`tagger.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/pipeline/tagger.py) still contains semantic fallbacks:

- bookmark-source fallback order
  - `bookmark_plan -> native_toc -> toc_entries -> headings`
- `_infer_link_contents()`
  - local link-label inference still exists
- `_infer_widget_accessible_name()`
  - local field-name inference still exists
- `_should_artifact_nonsemantic_page_content()`
  - local artifacting of page content
- `_set_document_title()`
  - filename and `Untitled Document` fallbacks

Some of these are acceptable safety fallbacks, but some are still local semantic decisions.

## Medium-risk semantic heuristics

These are less central but still relevant:

- [`language.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/pipeline/language.py)
  - document or element language detection is inherently probabilistic; local detectors are still a model, but a separate offline one
- [`semantic_pretag_policy.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/semantic_pretag_policy.py)
  - widget and form grouping logic depends on geometry thresholds
- [`title_intelligence.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/title_intelligence.py)
  - page-1 focus is reasonable for many PDFs, but not universally justified

## Migration target

The app should move toward:

- raw Docling evidence packets
  - raw text
  - cleaned helper text
  - bbox/geometry
  - page position
  - Docling type/level metadata
  - native TOC/hyperlink/widget metadata
- model-centered semantic decisions
  - TOC detection
  - title selection
  - bookmark selection and hierarchy
  - link labels
  - field accessible names
  - page-role and front-matter roles
  - safe text repair decisions
- deterministic post-processing only for:
  - schema validation
  - bounds checking
  - PDF writing
  - audit logging
  - evaluation and safety gates

## Priority cuts

### Priority 1

- reduce candidate shaping in [`bookmark_intelligence.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/bookmark_intelligence.py)
- reduce TOC discovery heuristics in [`toc_intelligence.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/toc_intelligence.py)
- reduce title pre-curation in [`title_intelligence.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/title_intelligence.py)

### Priority 2

- replace grounded-text auto-apply heuristics in [`grounded_text_apply.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/grounded_text_apply.py) with model-backed adjudication plus fidelity gating
- reduce local semantic gates in [`semantic_pretag_policy.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/services/semantic_pretag_policy.py)

### Priority 3

- shrink semantic fallbacks in [`tagger.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/pipeline/tagger.py)
- shrink TOC and heading inference rules in [`structure.py`](/Users/stephenzweibel/Apps/pdf-accessibility-app/backend/app/pipeline/structure.py)

## Principle for future changes

When deciding whether code is acceptable:

- If it extracts evidence, it is usually fine.
- If it validates or writes outputs, it is usually fine.
- If it chooses meaning, structure, or labels without the model, it is suspect.
