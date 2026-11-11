# Accessible document HTML export

`backend/app/services/html_export.py` exposes `export_html(tagged_pdf: Path) -> str`.
It reads the final PDF, including reviewer edits, without source JSON, model
calls, or network access. The v2 application offers it beside the PDF when the current revision includes
`document.html`. Local CLI use remains available.

From `backend/`:

```sh
PYTHONPATH=. .venv/bin/python scripts/export_html.py tagged.pdf -o document.html
```

Omit `-o` to write to stdout. The CLI rejects overwriting the input PDF.

## Mapping

| PDF semantics | HTML |
| --- | --- |
| XMP title, then document-info title, then filename stem | `title`; prepend `h1` when no H1, H, or Title appears in the exported tree |
| Catalog `/Lang`, default `und` | `html lang`; inherited element languages become `lang` where they change |
| H1–H6, H, Title | `h1`–`h6`; H and Title become `h1` |
| P | `p` |
| L / LI / Lbl / LBody | `ol` or `ul` / `li` / `span` / `div` |
| Table / THead / TBody / TFoot / TR / TH / TD | `table` / row groups / `tr` / `th` / `td` |
| Table attributes | Row/Column scope becomes `row`/`col`; spans are retained; `/Headers` references generated HTML IDs |
| Caption | `caption` inside Table; `p` elsewhere |
| Figure | PNG data-URI `img`, with `/Alt` as `alt`; explicitly empty alternatives stay empty |
| Formula | `/ActualText`, then `/Alt`, then marked-content text |
| Link | `a` for HTTP(S), mailto, tel, or explicit local page destinations; otherwise text in a `span` |
| Note / BlockQuote / Code | `aside` / `blockquote` / `code` |
| Document / Part / Sect / Div | `div` / `div` / `section` / `div` |
| Other roles | `span`, retaining children and language |
| Artifacts | omitted |

Role maps, including namespace maps, resolve through the screen-reader reader.
`MarkedContentReader` in `structure_text.py` supplies the shared PDFium text,
marked-content ActualText, Form handling, spacing, and dehyphenation.
Element ActualText replaces descendants. Figure ActualText appears once as
text alongside a decorative rendering, avoiding duplicate announcements.

Lists choose ordered/unordered by labels, including markers retained in LBody.
ListNumbering is the fallback when labels are absent. Original labels remain
visible; CSS suppresses duplicate browser markers. Table attributes support
arrays and ClassMap attributes. Generated IDs remain unique if PDF IDs collide.
PDF Scope Both has no single HTML scope equivalent; explicit headers remain.
Local links target the first exported element on the destination page. Named
destinations and coordinate-specific targets are not resolved.

## Figures and size limits

Figure crops use marked-content references and the existing
`PdfiumText.marked_bounds`, including Form transforms and clipping. A Layout
BBox, when present, supplies the region. PDFium renders the crop, respecting
rotation and visible page bounds, without annotations.

Limits: 150 ppi, 1,600 pixels on the longest side, 2 MiB per embedded PNG data
URI, and 8 MiB total. Byte limits include base64 and URI prefixes. Text is never
truncated. Oversized, unavailable, multi-page, or unrenderable regions become
bordered placeholders: informative alternatives use `role="img"` and
`aria-label`; decorative placeholders are hidden from assistive technology.
An HTML comment records each omission.

The document includes inline CSS and a restrictive CSP. All PDF text and
attributes are escaped. There are no scripts or automatically fetched external
resources. Untagged PDFs are rejected.

## Tests and text equivalence

`backend/tests/test_html_export.py` uses synthetic PDFs, the real tagger, and
reviewer edits to final tags. Its standard-library HTML parser checks balanced
elements, unique IDs, list/table nesting, and header/anchor references.

Test functions (parameterized where appropriate):

- `test_headings_lists_links_and_language_from_tagger`
- `test_table_scope_spans_headers_caption_and_duplicate_pdf_ids`
- `test_figures_are_png_crops_with_alt_and_decorative_empty_alt`
- `test_image_budget_and_missing_region_keep_alternatives`
- `test_synthetic_fixtures_match_screen_reader_and_omit_artifacts`
- `test_spacing_dehyphenation_and_marked_actual_text`
- `test_formula_note_quote_code_internal_link_and_inherited_lang`
- `test_unsafe_link_actions_remain_plain_text`
- `test_title_fallback_and_untagged_input`
- `test_cli_writes_self_contained_utf8_html`
- `test_nested_lists_with_labels_and_inherited_language`
- `test_inline_spans_keep_cross_mcid_word_boundaries`
- `test_marked_actual_text_without_glyphs_and_artifact_nodes`
- `test_image_resolution_and_cumulative_budget`
- `test_figure_region_in_a_form_xobject`
- `test_decorative_artifacts_from_tagger_are_omitted`
- `test_cli_cannot_overwrite_input_pdf`
- `test_figure_actual_text_replaces_alt_once_and_keeps_language`

Each semantic fixture compares normalized body text with `screen_reader_text`.
Assertions explicitly account for the requested injected title H1 and Formula
Alt fallback, which the plain-text reader omits. Figure alternatives are tested
separately because the reader deliberately excludes Figure Alt. These are
parser and PDF-rendering tests, not browser/screen-reader acceptance tests.

Local sample exports, 2026-10-31 (bytes; no image omissions):

| Tagged input | PDF | HTML | Reader characters | Images |
| --- | ---: | ---: | ---: | ---: |
| `pdf_fixtures.tagged_text_pages`, 3 pages, running heads | 1,981 | 1,453 | 299 | 0 |
| `pdf_fixtures.tagged_table`, 3 × 3 | 2,024 | 1,448 | 53 | 0 |
| `pdf_fixtures.described_text`, P in Form XObject | 1,456 | 1,567 | 409 | 0 |
| `test_html_export.tag_lines`, heading and figure | 3,419 | 1,914 | 14 | 1 |
| `backend/test_sample.pdf`, locally tagged from measured operators | 5,173 | 1,563 | 355 | 0 |
| `backend/test_climate.pdf`, locally tagged from measured operators | 8,124 | 1,826 | 562 | 0 |

All six samples passed text-equivalence and structural checks. These small
inputs are examples, not a remediation benchmark. The checked-in 89-page
`backend/test_wac.pdf` also exported (4,366,457-byte input; 1,111-byte HTML), but
its Document tag has no children and the reader returns zero characters. Its
title-only output supplies no useful text-coverage evidence.

## Product wiring and compatibility

The runner generates UTF-8 `document.html` in `render_views` from the final
`job.output_path` for complete and manual-remediation jobs. Process, review,
and sync tasks all use this path. HTML stays in memory until the existing
lease-scoped uploader sends it with the other revision files; it is regenerated
from the final PDF rather than saved in the state archive.

Export failure is optional: the PDF and other files still upload and the task
completes without HTML. A JSON warning has event `html_export_failed`, job ID,
and exception type. It excludes exception messages, tracebacks, document text,
and paths.

The optional file name is declared on both sides: `DOCUMENT_HTML_FILE` in
`backend/app/runner/views.py` and `worker/src/contract.ts`. Older runners can
still complete without this file; no new completion fields or database
migration are required. The existing runner-api upload handling and JobBoard
revision manifest already carry arbitrary allowed files, so their code and
the task/state code need no new path. Deploy the updated Worker receiver
before enabling the updated runner. No deployment is part of this change.

The Worker adds `has_html` to job responses from the current manifest and
output-ready status. Its owned-job route is
`GET /api/jobs/{id}/download/document.html`, with:

- `Content-Type: text/html; charset=utf-8`
- `Content-Disposition: attachment`, with `accessible_<PDF stem>.html`
- `Content-Security-Policy: default-src 'none'; img-src data:; style-src 'unsafe-inline'`
- `X-Content-Type-Options: nosniff`
- `Cache-Control: private, no-store`

Type and security headers are enforced when serving, independently of R2
metadata. Another owner receives the same 404 as a PDF request. Missing,
unlisted, or not-yet-ready HTML returns 404. A new revision replaces the
manifest, so a failed subsequent export cannot expose stale HTML. The
frontend's plain Accessible HTML link appears beside PDF on job and review
pages only when `has_html` is true. Older saved job views without this
optional capability field continue to hide the control.

Additional runner tests in `backend/tests/test_runner.py`:

- `test_process_uploads_accessible_html_from_final_pdf` (both output statuses)
- `test_html_export_failure_still_completes_pdf_with_safe_diagnostic` (both statuses)
- `test_later_tasks_regenerate_accessible_html` (review and sync)

Workerd tests in `worker/test/worker.test.ts`, under accessible HTML downloads,
cover the owner response/headers for both statuses, another user's refusal,
older runner completions and unlisted files, missing R2 content, and a reviewed
revision that drops the prior HTML. The frontend has no test harness; its
TypeScript build checks the new capability and component props.

Validation of this wiring: Ruff passed; Pyright reported 0 errors, 0 warnings;
all 1,038 backend tests passed, including the six new runner cases (14 total
runner tests). Frontend ESLint and TypeScript/Vite production builds passed.
The Worker check used Bun 1.3.14 first on PATH: lint, both TypeScript checks,
and the strict Wrangler dry-run build passed. Its workerd pool could not
start because the sandbox refused the localhost listener
(`listen EPERM 127.0.0.1`), so none of the Worker runtime tests ran. Run
`bun run check` from worker/ outside that restriction for runtime acceptance.
The five new Worker cases remain unverified at runtime. Secret scanning and
`git diff --check` passed. Python and Bun lockfiles are unchanged. No network
or model calls, commits, pushes, or deployments were performed.
