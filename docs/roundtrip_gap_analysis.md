# Round-Trip Gap Analysis

This note treats the seeded round-trip corpus as an application-level probe, not as a set of document-specific bugs.

The current question is:

Can the app take a known-good accessible PDF, strip recoverable accessibility semantics, remediate it, and restore the accessibility layer users actually depend on?

The first seeded corpus already shows a useful split between generalized strengths and generalized defect classes.

## Current strengths

- Visible text preservation is strong.
  - The first corpus run held raw text similarity at `1.0` for both seed documents.
- Reading-order recovery is strong.
  - The first corpus run held structure-transcript and reading-order metrics at `1.0/1.0`.
- Basic structural tagging is strong.
  - Headings, lists, bookmarks, and broad structure-tree reconstruction are working at a high level.

These are application-level capabilities, not one-off successes for the BMUV and USCIS fixtures.

## Generalized defect classes

### 1. Document language restoration is underpowered

Observed failure mode:

- The application can set `/Lang`, but it previously defaulted to the explicit `language` argument or to `"en"` when no stronger signal was passed.
- In practice this meant the tagging step could emit a generic language tag even when the document had a more specific recoverable or inferable language.

Relevant code paths:

- `backend/app/pipeline/structure.py`
- `backend/app/pipeline/tagger.py`
- `backend/app/pipeline/orchestrator.py`

What changed:

- Structure extraction now infers a document-level language from element language hints and visible text.
- Tagging now resolves document language from `structure_json["language"]` before falling back to explicit OCR/default language values, and it normalizes Tesseract-style codes like `eng` to BCP-47.

Remaining limitation:

- Locale-specific tags such as `en-GB` are not always recoverable from stripped visible content alone.
- If the stripped benchmark removes `/Lang` and XMP language metadata and the visible text is simply English, the app can usually recover `en`, but not necessarily `en-GB`.

Implication for evaluation:

- The benchmark should distinguish base-language recovery from exact locale-subtag recovery.

### 2. Link accessible names were too generic

Observed failure mode:

- Internal links could be tagged structurally while still receiving generic `/Contents` values like `Link to destination`.
- That is syntactically safer than an empty `/Contents`, but weak for actual assistive use.

Relevant code paths:

- `backend/app/pipeline/tagger.py`
- `backend/app/pipeline/fidelity.py`
- `backend/app/services/roundtrip_compare.py`

What changed:

- Link tagging now prefers overlapping visible page text when a link annotation clearly covers a labeled region.
- This is especially important for TOC-style internal links, where the visible row text is often the best accessible name.

Remaining limitation:

- Some links rely on invisible author-supplied semantics or have rectangles that do not cleanly overlap a text region.
- Those cases still fall back to generic destination- or URI-based strings.

Implication for evaluation:

- The round-trip lane should keep treating descriptive link text as a first-class accessibility signal, not as a secondary nicety.

### 3. Form accessible-name generation is biased toward over-compression

Observed failure mode:

- The form-intelligence prompts explicitly preferred concise visible labels.
- That bias tends to collapse richer accessible labels into shorter labels like `State` or `ZIP Code`, even when assistive technology benefits from section context or action cues such as `Select State` or `Enter ZIP Code`.

Relevant code paths:

- `backend/app/services/intelligence_gemini_forms.py`
- `backend/app/services/intelligence_gemini_semantics.py`
- `backend/app/services/semantic_pretag_policy.py`
- `backend/app/pipeline/orchestrator.py`

What changed:

- The form prompts now explicitly preserve short section/group context and short action cues when they are needed to maintain meaning.
- The prompts also stop rewarding shortening purely for brevity.

Remaining limitation:

- This class still depends on model behavior, not a deterministic rule.
- The next likely improvement is to provide stronger structural context for repeated short labels and to expand the eval corpus for ambiguous form pages.

Implication for evaluation:

- The benchmark should score not just whether a field has any accessible name, but whether the resulting label is disambiguating enough for repeated controls and form sections.

### 4. Title restoration is only partially recoverable

Observed failure mode:

- The app currently restores titles from visible structure extraction, not from hidden original metadata that was stripped out.
- That means visible-title recovery can succeed while exact gold metadata title recovery still fails.

Relevant code paths:

- `backend/app/pipeline/structure.py`
- `backend/app/pipeline/tagger.py`
- `backend/app/services/roundtrip_compare.py`

Current status:

- This is partly a pipeline issue and partly an eval-design issue.
- When the gold title includes metadata-only wording that is not visible after stripping, exact title equality is not a pure recoverable-core target.

Implication for evaluation:

- The benchmark should distinguish:
  - visible-title recovery
  - exact metadata-title recovery

## Recommended next steps

1. Add a primary-language benchmark signal alongside exact language match.
2. Expand the round-trip corpus by failure class rather than by document genre alone.
3. Add form-specific assertions for repeated short labels, section context, and action cues.
4. Split title evaluation into visible-title recovery vs metadata-title recovery.
5. Continue treating raw text and reading order as necessary but insufficient gates.
