# Evaluation

How we measure remediation quality, and the results so far. The goal is to
beat Adobe Acrobat's OCR and Auto-Tag on the same documents; see
[cail-integration-plan.md](cail-integration-plan.md). The documents that
matter most are printed books and articles, scanned or born digital;
handwriting is a side case.

## Method

Everything is scored on what a screen reader that follows the tags hears
(NVDA or JAWS with Acrobat or Reader): the text reachable through the
structure tree, in tag order, with artifacts left out
(`backend/app/services/structure_text.py`). Each marked-content sequence is
read as pdfium, Chrome's PDF engine, extracts text: it infers word spaces
from the font's space width, expands ligatures, drops a string drawn twice
over itself (fake bold), and joins a word hyphenated at a line end.
Marked-content `/ActualText` is read in place of the glyphs it covers. Text
inside a Figure is left out unless the Figure has no alt text and the
lenient reading is asked for; tables can be kept as HTML so their cell
structure is scored. Chrome's own viewer ignores the tags by default and
reads pdfium's text in its own order, so there only the text layer counts.

- **Benchmark:** [olmOCR-Bench](https://huggingface.co/datasets/allenai/olmOCR-bench)
  (Ai2, ODC-BY). Each test checks one page: a passage must be present, a
  passage must be absent (running heads, page numbers), one passage must come
  before another, or a table cell must have given neighbors and headings.
  Scores are the share of tests passed, with a 95% bootstrap confidence
  interval. The samples (`*_s60`, 60 pages each) are compared with every
  tool; olmOCR-Bench's whole `multi_column`, `tables`, and
  `headers_footers` categories (685 pages, 2,666 tests) and their scans
  decide between versions of the tagger.
- **`old_print`** (our addition, 49 pages, 509 tests): pages from 11
  public-domain books printed 1794–1925, from Internet Archive scans, with
  tests taken from Project Gutenberg transcriptions of the same editions and
  checked by eye against each page image. Sources are listed in
  [backend/eval/old_print/sources.md](../backend/eval/old_print/sources.md).
- **Scans** (`*_scan`): the born-digital pages rendered at 300 ppi in
  greyscale, turned a fraction of a degree, softened, given noise, and saved
  as JPEG (`scripts/synthetic_scan.py`), under the same tests.
- **Structure round trip:** well-tagged documents have their tags stripped
  (`scripts/strip_accessibility.py`), go through the pipeline, and the
  resulting tag tree is compared with the original, both rendered as
  Markdown. Metrics are opendataloader-bench's: NID (text in reading order),
  TEDS (table tree edit similarity), MHS (section tree), plus MHS-L, which
  also counts heading levels. `gold_rt` is 11 gold documents (the PDF/UA
  reference suite, the Matterhorn Protocol, a NOAA report, a PDF/UA paper, a
  table set; 224 pages, 322 headings, 62 tables). `cuny_rt` is 19 well-tagged
  CUNY documents from Academic Works (282 pages: syllabi, OER chapters,
  assignments, lesson plans, slides, two conference papers, a book chapter, a
  capstone, a library newsletter); `cuny_rt_scan` is the same documents as
  image-only scans. How they were chosen is in
  [backend/eval/README.md](../backend/eval/README.md).
- **Full pipeline:** `backend/scripts/run_pipeline_bench.sh` runs
  `run_pipeline` (classify, OCR, Docling structure through docling-serve as in
  production, tagging, validation) on each page and reads the tagged result.
  The app's AI steps are off unless a run names a Gateway model (`--llm`).
  It runs in `pdf-a11y-eval:branch`, the production image built from the
  checkout (`--image` names another, such as a published runner image).
  Pipeline results here used docling-serve 1.35.0 (Docling 2.130), on CPU
  through October 5 and on the GPU since. Production on actual-dell has run
  the same 1.35.0 since 2026-10-28; before that it ran 1.12.0 (Docling 2.72),
  which loses heading levels.
- **Run records:** each page's run leaves `<stem>.run.json` (every step's
  status and seconds, docling-serve cache use, model requests, tokens and
  cost), and each capture a `capture.json` (commit, image, docling-serve and
  Docling versions and device, OCR engine, Tesseract and OCRmyPDF versions,
  date). Bench runs keep docling-serve's answers in
  `data/eval/cache/docling-serve`, keyed by the PDF's bytes, the request's
  options and the Docling version, so a page converted before is not
  converted again (a scan's OCR output differs from run to run, so only
  born-digital pages repeat). With `--llm`, model calls go through a
  cassette (`scripts/bench_caches.py`): `replay` answers the requests it
  has recorded and records the rest, `record` calls the model every time,
  `strict` calls nothing and fails what it lacks. The caches live in the
  bench process only; the app and the runner never load them.
- **Re-tagging:** a pipeline run keeps what the tagger was given for each
  page (`scripts/pipeline_bench.py`), and `scripts/retag_bench.py` tags every
  page again with other tagger code in about a minute, without OCR and
  Docling. The whole categories were measured that way, from inputs captured
  with docling-serve on the Mac Studio's GPU (MPS, one worker: more crash
  PyTorch's Metal backend), which scores the same as on CPU. Re-tagging does
  not replay the font repairs the pipeline makes to many born-digital tagged
  PDFs afterwards (24 of the multi-column sample's 60 pages, 10 of the
  tables sample's), so on born-digital pages it can differ from a pipeline
  run by a few tests (two on each of those samples). Captures are not
  committed; each set is kept as a release asset of the Lab's
  [eval-data repository](https://github.com/CUNY-AI-Lab/pdf-accessibility-eval-data/releases),
  with its `capture.json` files and per-test results.
- **Per-test results:** `scripts/score_olmocr.py` keeps every test's result
  in `results/<subset>/<candidate>.jsonl`, and `--against <candidate>` lists
  the tests fixed and broken between two candidates with the paired counts.
- **Where a test is lost:** `scripts/attribute_failures.py` repeats each
  failed test's check on what each stage handed the next: the text layer of
  the PDF the tagger was given, Docling's text in the structure's order, and
  the tagged output. A failure is the tagger's when Docling's text passes or
  has the test's words in order; OCR's when a word the test needs is not in
  the text layer at all (on a born-digital page, the font repair's when the
  source PDF has the word and the PDF the pipeline rewrote does not, else
  the source PDF's own); otherwise Docling's. Failures where the tagged output has every word
  in order and only punctuation, accents, case or spacing differ are counted
  apart.
- **OCR stage:** `backend/scripts/ocr_bench.py` runs the app's own OCRmyPDF
  command inside the production image and reads the raw text layer.
- **AI steps:** `backend/scripts/score_semantics.py` compares the title,
  language, and figure alt text of each remediated gold document with the
  author's. A judge model (`deepseek-v4-pro-0813` on the Gateway) scores
  titles and alt text 1–5 for how much of what the author's version tells a
  reader the remediated one tells them too; wording may differ.
- **Adobe:** `backend/scripts/adobe_bench.py` runs Adobe PDF Services OCR then
  Auto-Tag within the free tier of 500 transactions a month. Auto-Tag costs
  ten transactions a page, so about 45 pages fit in a month: `old_scans` (44
  pages) in September 2026, `old_print` (all 49) in October.
- **opendataloader-pdf:** version 2.5.12 (Apache-2.0), the most complete
  open-source auto-tagger we found, run with its default settings on the
  born-digital samples and round trips. It tags without `/ActualText` and
  orders text with XY-Cut++.
- **Corpus:** defined in [backend/eval/](../backend/eval/README.md) and
  rebuilt byte for byte by `backend/scripts/fetch_eval_corpus.py`. The
  original data was git-ignored and lost with its worktree in late September
  2026; on 2026-10-04 the rebuilt corpus reproduced every September
  measurement that could be re-run (NID within 0.002, all else exactly).
  Adobe's `old_scans` outputs were lost and wait for a month's free tier.

**Corrections.** Results published here before 2026-09-24 read the structure
tree without marked-content `/ActualText`, so they badly understated
production, whose tagger carries OCR text that way (printed books appeared to
go from 30.5% to 91.7%). Results published before 2026-10-05 put a space
between glyphs by our own gap rule rather than as a PDF engine does. Results
published before 2026-10-07 joined two marked-content sequences without a
space even where the PDF draws a space character at the edge of one, which
mostly hurt tools that tag without `/ActualText`. The October 7 results below
were re-measured with that reader (opendataloader-pdf on multi-column
pages 56.6% → 64.8%, Adobe on printed books 81.5% → 83.7%).

## Results with AI steps on, real documents (2026-10-31)

We ran the whole pipeline with its AI steps on, as production runs it,
over the gold and CUNY round trips and 17 untagged CUNY documents
(`cuny_untagged`,
[backend/eval/](../backend/eval/README.md#untagged-cuny-documents)). The
code is main at `e128b6e` in `pdf-a11y-eval:branch`, with docling-serve
1.35.0 on the Mac Studio's GPU (MPS, one worker). The model is
`qwen3-vl-235b-a22b-instruct`, the one production's pipeline host on
actual-dell uses: `deploy/actual-dell` sets no `LLM_MODEL`, so the app's
default applies, and the Worker's relay passes the request's model
through. We recorded the 925 model responses in the cassette, so a rerun
of the same documents costs nothing. `pipeline_bench.py` writes each
run's outcome to its `run.json`, and `scripts/outcome_report.py` prints
it. The judge is `deepseek-v4-pro-0813`, as in September.

### Titles, language and alt text against the authors'

| Set | Title (1–5) | Titles at 4+ | Language | Author figures given usable alt text | Alt text (1–5) |
|---|---:|---:|---:|---:|---:|
| `gold_rt` (11 documents) | 4.27 | 9 | 11/11 | 115 of 163 | 3.02 |
| `gold_rt` without the Danish magazine | 4.60 | 9 | 10/10 | 53 of 83 | 3.92 |
| `cuny_rt` (19 documents) | 4.89 | 18 | 19/19 | 12 of 16 | 4.25 |

The magazine has 62 of the gold set's 115 judged descriptions. The scorer
pairs each author figure with the app's figure at the same position on
the page, and in the magazine the author's order and the app's order
differ. About 20 of its 34 scores of 1 or 2 come from pairs of different
pictures (a coffee press with a tea cozy, a phone screen with Louis
Braille). In most of the others the author named a person or a logo and
the model did not; since 10-30 the figure prompt has told the model not to
infer identities or copy captions. The model wrote every description in
English for a Danish magazine, and the title step chose the magazine's
first heading, "KÆRE BIDRAGYDER" ("Dear contributor"), which scored 1. September's bake-off (3.75, 110 of 163) ran older code with
older scoring, and we have not re-run it.

### Release outcomes

| Set | Documents | Complete | veraPDF compliant | Held with blocking tasks the review screen does not show | Held with no failed check |
|---|---:|---:|---:|---:|---:|
| `gold_rt` | 11 | 1 | 8 | 6 | 3 |
| `cuny_rt` | 19 | 2 | 3 | 16 | 0 |
| `cuny_untagged` | 17 | 5 | 11 | 9 | 2 |

Documents held by each release gate check (failed or could not run):

| Check | `gold_rt` | `cuny_rt` | `cuny_untagged` |
|---|---:|---:|---:|
| `compliance` (veraPDF) | 3 | 16 | 6 |
| `table_headers` | 5 | 6 | 6 |
| `figure_descriptions` | 3 | 2 | 5 |
| `font_text_fidelity` | 0 | 15 | 2 |
| `table_risk` | 2 | 1 | 4 |
| `text_drift` | 1 | 0 | 1 |

- Unembedded fonts. 15 of the 19 CUNY round trips fail veraPDF rule
  7.21.4.1-1 because Arial or Times New Roman is not embedded on the
  authors' own pages (after Academic Works' cover sheet). Stripping the
  tags leaves the fonts as they were, and the pipeline does not embed
  them, so the release gate holds all 15 on this rule and on
  `font_text_fidelity`.
  [pdfua_rule_coverage_matrix.md](pdfua_rule_coverage_matrix.md) lists the
  font rules as covered.
- Blocking tasks the person cannot see. The pipeline creates review tasks
  of the types `font_text_fidelity`, `figure_semantics`, `reading_order`
  and `form_semantics` as blocking, and the review screen shows only the
  types in `review_surface.USER_VISIBLE_REVIEW_TASK_TYPES`, which leaves
  these out. A person cannot see or resolve them. When a detected figure
  is not tagged, `figure_coverage` reports a warning and adds a blocking
  `figure_semantics` task; that task alone holds five documents (three
  gold, two untagged).
- Tables. `table_headers` fails on 17 documents across the three sets,
  including two born-digital syllabi with simple schedule tables.
- Short scans. The pipeline released four of the nine Academic Works scans
  as complete. OCR gave each a text layer, three got their title from the
  repository's cover sheet, and none has a figure.

### After the fixes (2026-10-31, `b0d9b79`)

The same 47 documents again, at `b0d9b79`, with:
- the hold policy (#40);
- embedded standard fonts (#42);
- simple table headers (#41);
- titles (#43);
- layout grids tagged as lists (#44).

The same docling-serve and model were used. The cassette answered 877 model requests, and 10 went to the model.

| Set | Documents | Complete before | Complete after | veraPDF compliant before | veraPDF compliant after |
|---|---:|---:|---:|---:|---:|
| `gold_rt` | 11 | 1 | 6 | 8 | 8 |
| `cuny_rt` | 19 | 2 | 14 | 3 | 16 |
| `cuny_untagged` | 17 | 5 | 8 | 11 | 11 |
| **Total** | **47** | **8** | **28** | **22** | **35** |

- **Hold policy:** replayed on the recorded outcomes, it accounts for 8 → 15 by itself. It stopped holding on checks the review screen didn't show: `font_text_fidelity`, `table_risk` and `text_drift`.
- **The rest of the gain:** most of it is the CUNY round trips passing veraPDF once their fonts were embedded.
- **Remaining holds:** 19 documents, all with a visible task. They overlap:
  - **A figure without a description: 10.** In every one, veraPDF fails rule 7.3-1, which flags the same missing alternative text, and three of them also fail other rules.
  - **A data table without header cells: 11.** Seven are held for this alone.
  - **Other veraPDF failures only: 2.**

### Held-out tests (2026-10-31 and 2026-11-01)

The 47 documents above were used to find and fix problems, so their completion rate overstates what an unseen PDF gets. Two held-out sets measure that instead. Each was drawn from public CUNY PDFs by fixed search queries, in rank order, before any run. Each takes several kinds of document and keeps tagged and untagged files as found. The protocol, queries and manifests (URLs and sha256) are kept outside the repository with the evaluation data.

| Set | Documents | Code | Complete | Failed outright |
|---|---:|---|---:|---:|
| 1 | 39 (25 already tagged) | production `b0d9b79` | 9 | 2 |
| 1 | 39 | `main` + #49 + #50 | 20 | 2 |
| 2 | 40 (33 already tagged) | released `ee76934` | 30 | 0 |

**Set 1** exposed:
- the tagger discarding authors' table headers on tagged uploads (#54);
- OCRmyPDF exit 4 failing the whole job (#51);
- a font-embedding regression from #48 (#53);
- an over-eager table-readability guard (#50).

Once fixes were made from it, set 1 stopped being a held-out measure.

**Set 2** found one cause behind 7 of its 10 holds: standard-14 Type 1 fonts (Times-Roman, Helvetica) were left unembedded in the Linux image (#55). Replayed through tagging and font repair in the production image, all seven pass veraPDF after #55, and none passed before. The full pipeline has not been re-run on set 2. Set 2 has now informed a fix, so the next honest measure needs a third sample.

**Set 3** (2026-11-01) was drawn the same way, excluding sets 1 and 2, and ran on `main` at `363387d` (PRs #57–#61) before release:

| Set | Documents | Code | Complete | Failed outright |
|---|---:|---|---:|---:|
| 3 | 33 (28 already tagged) | `363387d` | 26 | 0 |

Every untagged input completed (5 of 5), and 21 of the 28 tagged inputs did. Its scan stratum came up empty: every scan the queries returned was already in set 1 or 2.

The seven still held were:
- four on font rules: 7.21.4.1-1 (embedding), 7.21.8-1 (.notdef glyph) and 7.21.7-2 (Unicode values);
- one fillable handbook on 7.18.1-2;
- three long handbooks on screen-reader text coverage.

### Untagged CUNY documents

| Document | Kind | Pages | Classified | Status | Held by | veraPDF | Figures with alt text | Seconds | Cost (USD) |
|---|---|---:|---|---|---|---|---:|---:|---:|
| `bb_pubs_348` | book chapter (scan) | 4 | mixed | complete | | yes | 0/0 | 20 | 0.003 |
| `bb_pubs_360` | book chapter (scan) | 4 | mixed | complete | | yes | 0/0 | 37 | 0.003 |
| `bb_pubs_361` | book chapter (scan) | 4 | mixed | manual | `table_risk` | yes | 0/0 | 92 | 0.020 |
| `bb_pubs_363` | book chapter (scan) | 5 | mixed | manual | hidden task only | yes | 0/0 | 27 | 0.006 |
| `bb_pubs_492` | article (scan) | 2 | mixed | complete | | yes | 0/0 | 17 | 0.003 |
| `bx_arch_minutes_1392` | minutes (scan) | 4 | scanned | complete | | yes | 0/0 | 29 | 0.000 |
| `cc_oers_365` | syllabus (scan) | 9 | mixed | manual | `compliance` (7.1-3) | no | 0/0 | 143 | 0.015 |
| `jj_pubs_384` | book chapter (scan) | 15 | scanned | manual | `table_headers` | yes | 0/0 | 87 | 0.016 |
| `nc_pubs_23` | book chapter (scan) | 16 | scanned | manual | `compliance`, `figure_descriptions` | no | 0/1 | 93 | 0.013 |
| `wsq_236` | journal front matter | 2 | digital | manual | hidden task only | yes | 1/1 | 31 | 0.009 |
| `gc_cs_tr_403` | report | 58 | mixed | manual | `compliance`, `table_headers`, `table_risk`, `figure_descriptions`, `font_text_fidelity` | no | 21/22 | 152 | 0.118 |
| `ccny_business_communications_syllabus` | syllabus | 11 | digital | manual | `table_headers` | yes | 0/0 | 38 | 0.010 |
| `citytech_buf_2246_syllabus` | syllabus | 10 | digital | manual | `table_headers` | yes | 1/1 | 75 | 0.048 |
| `citytech_cst_1101_syllabus` | syllabus | 3 | digital | complete | | yes | 0/0 | 9 | 0.003 |
| `cuny_elo_badging_in_stem_slides` | slides | 24 | mixed | manual | `compliance`, `text_drift`, `table_headers`, `table_risk`, `figure_descriptions` | no | 39/40 | 176 | 0.119 |
| `gc_political_science_newsletter_2016` | newsletter | 22 | mixed | manual | `compliance`, `table_headers`, `table_risk`, `figure_descriptions`, `font_text_fidelity` | no | 75/108 | 420 | 0.308 |
| `gc_sociology_newsletter_2020_02` | newsletter | 19 | mixed | manual | `compliance`, `figure_descriptions` | no | 9/12 | 68 | 0.039 |

veraPDF still finds figures without alt text (7.3-1) in five documents,
an unembedded font in one, a font without a Unicode mapping (7.21.7-1) in
one, and content neither marked nor an artifact (7.1-3) in the scanned
syllabus. The classifier calls 10 documents mixed, either because
Academic Works put a born-digital cover sheet in front of a scan or
because born-digital pages carry large pictures.

We read ten generated descriptions (the first described figures of six
documents) and every title against the page images. Six descriptions are
accurate. Four are wrong. The model described a newsletter's masthead
with the caption of the photo below it, described three rows of a
sign-language figure identically when their mouth shapes differ, read the
LaGuardia logo's "LAGCC" as "LACCC", and invented a red border around a
black logo. Six of the ten figures are logos or mastheads. Of the 17
titles, 11 are accurate, and nine of the accurate ones are the Academic
Works record title printed on the cover sheet. Two are generic ("Front Matter",
"Syllabus Draft - Business Communications"). Four are wrong, three of them
section or slide headings ("February Calendar", "Awards Night 2016", "What
is a Digital Badge?") and one a file name ("CST-1101-CLASS_SYLLABUS_2023").
The title is right for one of the six campus documents, which have no
cover sheet.

### Cost and time

| Set | Pages | Model requests | Tokens in / out | Cost at catalog price | Median per document | Most | Median seconds |
|---|---:|---:|---:|---:|---:|---:|---:|
| `gold_rt` | 224 | 397 | 1.56M / 44k | $0.95 | $0.035 | $0.35 (84-page report) | 105 |
| `cuny_rt` | 282 | 230 | 0.90M / 30k | $0.56 | $0.019 | $0.08 | 51 |
| `cuny_untagged` | 212 | 344 | 1.23M / 30k | $0.73 | $0.013 | $0.31 (22-page newsletter, 149 requests) | 68 |

We priced the tokens at the Gateway's catalog price ($0.53 and $2.66 per
million), because the Gateway reports no cost per call and the app
records `cost_usd` as 0. The judge's 157 judgments cost $0.32. The
fidelity check used 37% of pipeline seconds on both round trips and 20% on
the untagged set, where OCR and alt text used more. We timed the runs on a
Mac GPU, and production's times on actual-dell will differ.

## Results (2026-10-31)

Main at `5af226b`, AI steps off, re-tagging all 1,926 captures from
`pipe_8a81fdc` as `retag_5af226b`: no tagging failures. The baseline is the
October 30 re-tag `retag_8a81fdc` (`retag_main` has identical per-test
results), scored again with the current reader. These are the same captured
inputs on both sides, not new OCR or Docling runs. The pipeline's later font
repairs on 220 captures are not replayed. Earlier results remain as history.

Counts below exclude olmOCR-Bench's baseline sanity checks, which the older
tables included in their test totals. olmOCR scores and every test's pass/fail
result are identical with the October 7 reader and the current reader.
Fixed and broken are paired against the baseline; no tests are unpaired.

| Subset (scored tests) | Baseline | Main (both readers) | Fixed | Broken |
|---|---:|---:|---:|---:|
| `old_print` (509) | 93.9% | 93.9% | 0 | 0 |
| `multi_column_s60` (219) | 72.6% | 72.6% | 0 | 0 |
| `multi_column_s60_scan` (219) | 67.6% | 67.6% | 0 | 0 |
| `tables_s60` (335) | 69.3% | 70.1% | 4 | 1 |
| `tables_s60_scan` (335) | 56.7% | 57.6% | 3 | 0 |
| `headers_footers_s60` (169) | 95.9% | 95.9% | 0 | 0 |
| `headers_footers_s60_scan` (169) | 89.3% | 89.3% | 0 | 0 |
| `old_scans` (526) | 24.1% | 24.1% | 0 | 0 |
| `multi_column` (884) | 71.8% | 74.3% | 22 | 0 |
| `multi_column_scan` (884) | 73.3% | 73.4% | 2 | 1 |
| `tables` (1,020) | 69.1% | 71.1% | 21 | 1 |
| `tables_scan` (1,020) | 58.2% | 59.0% | 8 | 0 |
| `headers_footers` (753) | 92.7% | 92.2% | 0 | 4 |
| `headers_footers_scan` (753) | 90.8% | 90.8% | 0 | 0 |

Round trips with the current reader, including gold rebuilt from the
author-tagged originals with that reader. The scanned CUNY documents use the
same gold as their born-digital originals.

| Round trip (NID / TEDS / MHS / MHS-L) | Baseline | Main |
|---|---|---|
| `gold_rt` | 0.865 / 0.545 / 0.578 / 0.436 | 0.879 / 0.545 / 0.618 / 0.463 |
| `cuny_rt` | 0.967 / 0.714 / 0.520 / 0.385 | 0.966 / 0.715 / 0.520 / 0.384 |
| `cuny_rt_scan` | 0.947 / 0.610 / 0.507 / 0.358 | 0.948 / 0.610 / 0.507 / 0.358 |

### Corrections

The #29 work corrected reading of nested marked content, including marks
around Form invocations. Its coverage check also joins words containing
soft hyphens and discounts an unmarked shadow copy only when a coincident
tagged copy exists. The soft-hyphen and shadow-text rules belong to the
coverage check; they do not add benchmark passes. Coverage calibration is in
[tagged-text-coverage-calibration.md](tagged-text-coverage-calibration.md).

The corrected reader changes the CUNY gold for `cc_oers_420` (list and table
text) and `qb_oers_7` (chart labels). The table below holds each tagged output
fixed and changes the reader on both the output and the gold. These changes
are scoring corrections, not gains from re-tagging. The October 7 reader is
the reader at `8a81fdc`, unchanged since October 7; its rebuilt gold matches
the stored gold.

| Round trip (NID / TEDS / MHS / MHS-L) | Output | October 7 reader | Current reader |
|---|---|---|---|
| `gold_rt` | Baseline | 0.865 / 0.545 / 0.578 / 0.436 | 0.865 / 0.545 / 0.578 / 0.436 |
| `gold_rt` | Main | 0.879 / 0.545 / 0.618 / 0.463 | 0.879 / 0.545 / 0.618 / 0.463 |
| `cuny_rt` | Baseline | 0.965 / 0.683 / 0.520 / 0.385 | 0.967 / 0.714 / 0.520 / 0.385 |
| `cuny_rt` | Main | 0.964 / 0.683 / 0.519 / 0.384 | 0.966 / 0.715 / 0.520 / 0.384 |
| `cuny_rt_scan` | Baseline | 0.946 / 0.582 / 0.507 / 0.358 | 0.947 / 0.610 / 0.507 / 0.358 |
| `cuny_rt_scan` | Main | 0.946 / 0.583 / 0.507 / 0.358 | 0.948 / 0.610 / 0.507 / 0.358 |

### Merged changes

The combined paired counts are above. Individual change measurements below
were recorded with the PRs; they are not separate re-runs for this section.

| PR | Change and measured contribution |
|---|---|
| #21 | Release-gate fixes: hold incomplete or unreadable remediations, preserve empty formulas' glyph text, reject placeholder figure descriptions and document titles. Re-tagging does not exercise the release decision. |
| #27 | Tag text inside Form XObjects; fix list order, splitting of quote text operators, Type 3 glyph boxes, overlapping cells, and source `/ActualText`. Its whole-category measurement fixed 39 tests and broke 5. Recovered form text exposes running heads Docling labels as body; main's `headers_footers` losses include those heads and an over-broad absent test for “98”. |
| #28 | Vote between Docling, drawing order and XY-cut; recover unclaimed text; put link text inside Link. Its measurement fixed 7 tests and broke 1, with gold NID +0.014, MHS +0.040, MHS-L +0.027. The broken table test is a recovered “US$)” joining the neighboring heading cell. |
| #29 | Gate text reachable through the tags and tables without headers, and correct the shared reader. Re-tagging measures the reader correction above, not the new holds. |
| #32 | Disable Docling's `table_cell_matching` on scanned documents. This is a docling-serve request option; captured tagger inputs cannot measure it. Pipeline results are below. |
| #33 | Infer table headers only on strong evidence and remove OCR'd rule strokes repeated at cell edges. Its measurement fixed 7 `tables_scan` tests and 3 in the sample, with none broken. |

#32 was measured by a **pipeline run against `8a81fdc`**, not by re-tagging.
These are that experiment's figures, with its reader and gold, not the
current-reader round-trip figures above. Its gains cannot be added to the
re-tagging gains without a new pipeline run.

| Subset / measure | Pipeline baseline | `table_cell_matching=false` |
|---|---:|---:|
| `tables_scan` | 58.2% | 61.3% |
| `tables_s60_scan` | 56.7% | 58.5% |
| `cuny_rt_scan`, TEDS | 0.582 | 0.566 |

### Experiments not adopted

Recorded experiment results; not re-run here. The olmOCR 2 measurements were
pipeline runs against `8a81fdc`, placing its text on Tesseract's boxes.
PaddleOCR-VL's own table HTML was substituted for each tagged table; that
does not show that its cells can be mapped back onto page glyphs.

| Experiment | Subset / measure | Baseline | Experiment | Fixed / broken |
|---|---|---:|---:|---:|
| olmOCR 2 on Tesseract boxes | `old_scans` | 24.1% | 40.7% | 93 / 6 |
| olmOCR 2 on Tesseract boxes | `old_print` | 93.9% | 98.2% | 27 / 5 |
| olmOCR 2 with alignment guard | `old_scans` | 24.1% | 29.3% | |
| olmOCR 2 with alignment guard | `old_print` | 93.9% | 95.7% | |
| PaddleOCR-VL-1.5's own table HTML | `tables` | 69.1% | 77.7% | 153 / 65 |
| PaddleOCR-VL-1.5's own table HTML | `tables_scan` | 58.2% | 79.9% | 256 / 35 |
| PaddleOCR-VL mapped to the tagger's cells | `tables_s60` | 69.3% | 63.9% | 19 / 37 |
| granite-docling's own HTML | `tables_s60` | 69.3% | 67.5% | |
| Heading levels from document-wide evidence | MHS-L | 0.3915 | 0.3906 | One document changed |

olmOCR 2 rewrote `$10,596,928` as `$10,506,928`, “millionnaire” as
“millionaire”, and “chronographic use” as “chronograph used”.

## Results (2026-10-07)

What a screen reader hears after each tool, AI steps off. "September" is
this branch before the October fixes, "October 5" its state when the first
October fixes were published, and "Now" its head.

| Subset (tests) | Production (v1) | September | October 5 | Now | opendataloader-pdf | Adobe |
|---|---|---|---|---|---|---|
| `old_print` (509) | 92.1% | 92.5% | 92.7% | **93.9%** | | 83.7% |
| `multi_column_s60` (219) | 41.6% | 60.3% | 69.4% | **73.5%** | 64.8% | |
| `multi_column_s60_scan` (219) | 51.6% | 50.2% | 63.5% | **67.6%** | | |
| `tables_s60` (336) | 10.4% | 65.1% | **69.9%** | **69.9%** | 27.2% | |
| `tables_s60_scan` (336) | 23.0% | 56.7% | **57.0%** | 56.7% | | |
| `headers_footers_s60` (170) | 95.3% | **95.9%** | **95.9%** | **95.9%** | 37.3% | |
| `headers_footers_s60_scan` (170) | **90.5%** | 89.9% | **90.5%** | 89.3% | | |
| `old_scans` (526) | 25.3% | 25.5% | **25.9%** | 24.1% | | |

| Round trip (NID / TEDS / MHS / MHS-L) | Production (v1) | September | October 5 | Now | opendataloader-pdf |
|---|---|---|---|---|---|
| `gold_rt` | 0.791 / 0.300 / 0.576 / 0.339 | 0.875 / 0.448 / **0.619** / 0.361 | **0.886 / 0.545 / 0.619** / 0.430 | 0.864 / 0.544 / 0.577 / **0.436** | 0.844 / 0.287 / 0.520 / 0.327 |
| `cuny_rt` | 0.877 / 0.512 / 0.511 / 0.350 | 0.956 / 0.683 / 0.527 / 0.317 | 0.960 / 0.683 / 0.528 / 0.391 | **0.963** / 0.683 / **0.534 / 0.397** | 0.961 / **0.739** / 0.420 / 0.330 |
| `cuny_rt_scan` | 0.893 / 0.409 / 0.501 / 0.344 | 0.934 / **0.610** / 0.516 / 0.305 | 0.938 / 0.600 / **0.518** / 0.338 | **0.947 / 0.610** / 0.507 / **0.358** | |

For scale, opendataloader-bench reports NID about 0.90 and TEDS 0.887 for
Docling's own output on its corpus.

Since October 5 the branch gains most on multi-column pages (69.4% →
73.5%) and their scans (63.5% → 67.6%), on printed books (92.7% → 93.9%),
and in reading order on the CUNY scans (NID 0.938 → 0.947). It loses on the
gold documents' order and sections (NID 0.886 → 0.864, MHS 0.619 → 0.577),
the cost of following Docling's reading order (see below), and on
manuscripts (25.9% → 24.1%), mostly the cost of recognizing text once.
It beats v1 on every subset but scanned headers and footers and
manuscripts, and on every round-trip measure. It beats opendataloader-pdf on
every born-digital sample and the gold round trip, and Adobe on printed
books.

On olmOCR-Bench's whole categories, re-tagging the same captured inputs:

| Category (tests) | Before the single path | One path | + Docling's order | + glyph text alone | Now |
|---|---|---|---|---|---|
| `multi_column` (884) | 68.3% | 68.2% | **71.7%** | 71.6% | 71.6% |
| `multi_column_scan` (884) | 73.4% | 73.4% | **73.5%** | **73.5%** | **73.5%** |
| `tables` (1,022) | **69.6%** | 69.3% | 69.3% | 68.3% | 69.1% |
| `tables_scan` (1,022) | **58.3%** | **58.3%** | **58.3%** | 58.2% | 58.2% |
| `headers_footers` (760) | 92.6% | 92.6% | **92.7%** | **92.7%** | **92.7%** |
| `headers_footers_scan` (760) | 90.8% | 90.8% | **91.1%** | 90.8% | 90.8% |

"Before the single path" is commit 2d8a9d7, "One path" 1469380, "+ Docling's
order" 2fad760, "+ glyph text alone" 47dab17 (element `/ActualText` gone,
content assigned by word), and "Now" the head.

## What changed in October

Each change was measured on the whole suite before it was kept.

### To October 5

- **Element text is what pdfium reads in the element's boxes.** Every tagged
  element's `/ActualText` was Docling's text, which straightens quotes and
  runs words together where the PDF spaces them by position. It is now the
  text of the words pdfium finds inside the element's layout boxes (every box
  Docling gives it on the page), kept when its letters and digits match
  Docling's to within 2%. A word belongs to a box holding at least half of
  it, as Docling assigns a word to the layout region holding the largest
  share of it: Docling's boxes on scans often stop short of a word's last
  letters, and on dense pages graze the next line. Table cells get their
  text the same way. This text agreed with poppler's on 96.0% of 5,862
  Docling elements; the glyph-to-text rules it replaced, 91.8%.
- **Docling's boxes are moved into PDF user space before tagging.** Docling
  places everything on the displayed page (turned by `/Rotate`, with the
  origin at the visible page box's corner), while glyphs, images, and
  annotations are in user space. The tagger compared the two unconverted, so
  on rotated pages and pages whose MediaBox does not start at the origin (16
  documents in the corpus) glyphs went to the wrong elements.
- **Sibling headings stay siblings.** Docling's levels are normalized with a
  stack of open sections, so three sections Docling put at one deep level are
  no longer staircased as H3, H4, H5.
- **Captions are text.** Figure and table captions and footnotes, which
  Docling keeps as children of the picture or table, were tagged as
  artifacts.
- **Pictured text in born-digital documents is recognized.** A page whose
  images cover a quarter of it with under 1,500 native characters has its
  text in the images (a pictured table, a scanned insert). Such pages are
  OCR'd with `--redo-ocr --pages`, which leaves printable vector text alone,
  unless the page already carries an invisible OCR layer, which `--redo-ocr`
  would replace.

Tried and dropped: requiring the glyph text to keep every digit in Docling's
text. It restored a list number hanging outside its item's box on one
printed-book page, but on scans Docling often read the page with its own
OCR, which takes characters for digits that Tesseract's layer does not, so
it rejected good glyph text and cost scanned multi-column pages about five
tests.

### October 6–7: one tagging path

- **Text is recognized once.** Docling read every scanned page again with
  its own RapidOCR after the OCR step had given it Tesseract's text layer,
  and where the two disagreed its reading could be what a screen reader
  heard. Docling now reads the text layer (`do_ocr` off). The scanned
  multi-column sample rose from 63.5% to 68.5%; manuscripts fell 1.4 points,
  as RapidOCR read some handwriting better than Tesseract. A document of
  born-digital pages with one picture page and no text on it (a scanned
  insert) is now OCR'd rather than treated as born digital.
- **One rule decides which element content belongs to.** The tagger decided
  two ways (whole text objects matched one to one by a weighted Hungarian
  assignment, or text runs scored one by one), picked between them per page,
  read positions from four sources, and wrote each element's text as
  `/ActualText` because the result was not reliable alone. Now each word
  (pdfminer's rule: a gap over a tenth of a glyph's size ends one) goes to
  the element or table cell whose box holds the middle of its height and at
  least half its width, the rule element text uses, judged in the direction
  the text runs; nested boxes go to the smallest. A text operator whose
  words have different owners is split, between `TJ` array elements or
  inside a string between glyph codes, as opendataloader-pdf cuts a string
  that crosses table cells; the page renders identically. Images go to the
  figure they overlap; drawn paths are wrapped whole as artifacts. The change
  removed 640 lines from the tagger, and scipy and rtree from the
  dependencies.
- **The structure tree follows Docling's reading order**, not the order
  content is drawn.
- **`/ActualText` only for corrections.** Elements are read from their own
  glyphs. `/ActualText` remains for explicit corrections, formulas, and
  accented letters drawn as a letter and a separate accent. On the whole
  categories the last build with the overlays (2fad760) and the head differ
  by at most 0.3 points (scanned headers and footers 91.1% and 90.8%; tables
  69.3% and 69.1%). The tables tests still lost are text Docling's cell boxes
  miss (a header's second line, subscripts in a math table), which the
  overlays covered with Docling's text.
- **A paragraph continued on the next page is one element.** Docling marks a
  paragraph or list item that runs across a page break; both parts are now
  one structure element instead of two.

### Reading order: Docling's, at a cost on well-made documents

Following Docling's reading order instead of drawing order (2fad760 alone)
raises the whole `multi_column` category from 68.2% to 71.7%, moves the
other categories by at most 0.3 points (on scans the drawing order is
Tesseract's, already in reading order), leaves the CUNY documents as they
were, and costs the gold documents: NID 0.887 → 0.862, MHS 0.620 → 0.576,
MHS-L 0.463 → 0.434. Four of the eleven lose order, most of all the DocEng
abstract (NID 0.89 → 0.75, MHS 0.79 → 0.37), where Docling reads the right
column of the first page before the left. The gold documents were made to
be tagged correctly, and their content is drawn in reading order; many real
documents are not, and the established auto-taggers (Adobe,
opendataloader-pdf with XY-Cut++) order by layout too. A better layout
order, not drawing order, is the fix.

### What established tools do

- **opendataloader-pdf** splits a text operator between glyphs where it
  crosses table cells, keeping `TJ` position adjustments with their piece,
  and writes no `/ActualText`. The tagger now does both.
- **PAVE 2.0** assigns content to the element whose box it overlaps most,
  close to the tagger's rule.
- **ISO 32000-1, 14.8.2.5** asks tagged PDF to carry word spaces as real
  space characters; axesPDF inserts them. The tagger does not yet, so a
  reader that extracts text without inferring spaces from geometry may run
  words together where the PDF spaces them by position.
- **Chrome** builds its accessibility tree from pdfium's text and layout,
  inventing spaces from geometry and ignoring tags unless asked, so its users
  hear the text layer, not the structure tree.

### opendataloader-pdf

On default settings it orders CUNY documents as well as the branch (NID
0.961 against 0.963) and recovers their tables better (TEDS 0.739 against
0.683), but it loses the tables in the olmOCR sample (27.2%), leaves most
running heads in (37.3%), and nests headings worse.

### Docling's tables

Docling's own Markdown, without our tagger, scores 69.0% on the `tables`
sample. Most remaining failures are Docling's: a borderless table read as
one row whose cells hold whole columns, or no table found. Docling's
TableFormer v2 preset scores 41.2% on the same sample (it fixes 11 tests and
breaks 104), so the default model stays. On the scanned sample, Docling
given the raw scans and its own RapidOCR scores 62.4% (TableFormer v2
45.7%): Tesseract, whose text layer the pipeline gives Docling, reads table
rules as "|", misreads digits, and drops lone characters.

## Results: printed books (`old_print`, 49 pages, 509 tests)

| Candidate | Score | Present | Order | Absent |
|---|---|---|---|---|
| Production (v1) | 92.1% ± 2.3 | 94.1% | 86.4% | 95.9% |
| Branch, September | 92.5% ± 2.2 | 93.8% | 88.4% | 95.9% |
| Branch, October 5 | 92.7% ± 2.3 | 93.1% | 89.8% | **97.3%** |
| **Branch, now** | **93.9% ± 2.2** | 95.2% | 89.8% | **97.3%** |
| Adobe OCR + Auto-Tag (October 2026) | 83.7% ± 3.0 | **96.2%** | **93.2%** | 15.1% |

Gateway OCR (Qwen3-VL-235B) scored 83.3% here in September, below
Tesseract, so Tesseract stays the default. Adobe reads the most words and
orders them best, but it leaves running heads, folios, and catchwords in
what a screen reader hears, so it fails most absent tests. Order is the
branch's weakest measure on printed pages.

OCR stage alone (raw text layer read by pdfminer; its own layout guessing
scrambles Tesseract's order, so these understate what the pipeline hears):
Tesseract 59.9% ± 4.1, Qwen3-VL line spotting 66.2% ± 4.2 (67.4% with the
retry ladder).

## Results: born-digital and scanned articles

Docling found most of the sample's tables; the production tagger lost them.
It guessed text positions from its own content-stream reading (ignoring the
text matrix's scale on `Td`, font widths, and `TJ` offsets, and reading `TJ`
offsets as text), and it tagged a table row drawn by one `TJ` as one cell.
The branch takes positions from pdfminer's measurement of each text operator
(`app/pipeline/page_glyphs.py`) and splits an operator that spans several
cells into one per cell, inside a string where needed, which renders
identically.

## Results: CUNY documents

v1 orders the CUNY scans better than their born-digital originals, because
there it reads Tesseract's text layer instead of guessing positions from the
content stream. Four of the documents tagged by their authors with every
heading at level 1 (two syllabi, an assignment, the newsletter) favor v1,
which also puts every heading at level 1; the branch nests them and still
beats v1 on MHS-L.

## Results: AI steps (model bake-off, gold documents, September 2026)

The same 11 gold documents, remediated by the branch with its AI steps on,
each run with a different model. The gold set has 163 figures with
author-written alt text.

| Model | Title (1–5) | Figures given alt text | Alt text (1–5) |
|---|---|---|---|
| None (AI steps off) | 3.91 | 49 of 163 | 3.90 |
| Gemini 3 Flash Preview (production until now) | 4.09 | 124 of 163 | 3.76 |
| Gemini 3.8 Flash | 4.09 | 124 of 163 | 3.76 |
| **Qwen3-VL-235B (Gateway)** | **4.27** | 110 of 163 | 3.75 |

Every candidate set the right language on all 11 documents. The model makes
no difference to structure: NID, TEDS, and MHS stay within 0.005 of the run
without AI steps, and the `tables` sample within 0.3 points. Without AI
steps, alt text comes only from figure captions, so it covers few figures.

Qwen3-VL writes alt text for 14 fewer figures than Gemini. Eight of those are
screenshots of tables in one document: Qwen3-VL decides each is a table, not
an image, and leaves it for manual review. The alt text it does write
judges as well as Gemini's (3.75 against 3.76). Qwen3-VL is the default
model.

Mistral Large 3 (at most three images per request), Kimi K2.5, and
Gemma 3 27B (repeated timeouts) were dropped before scoring.

## Results: manuscripts (`old_scans`, September 2026)

Library of Congress letters (Clara Barton, Theodore Roosevelt, and Joseph Holt
papers), mostly handwritten, some typed. A side case, kept because Adobe was
measured here. These numbers predate the pdfium reader; Adobe's outputs were
lost, so they cannot be re-read. On all 98 pages the branch now scores
24.1% (October 5: 25.9%), most of the drop from Docling no longer reading
the scans with its own OCR.

On the 44 pages Adobe finished (240 tests):

| Candidate | Score | Present | Order | Absent |
|---|---|---|---|---|
| Production pipeline, as heard | 24.2% ± 5.6 | 15.2% | 8.4% | 100% |
| Branch, as heard | 24.2% ± 5.8 | 15.2% | 8.4% | 100% |
| Adobe OCR + Auto-Tag, as heard | 19.2% ± 5.2 | 8.8% | 4.8% | 96.9% |
| Adobe, counting text inside alt-less Figures | 21.2% ± 5.2 | 11.2% | 7.2% | 96.9% |
| Tesseract text layer (OCR stage) | 39.2% ± 6.2 | 55.2% | 2.4% | 71.9% |
| Qwen3-VL-235B plain transcription (no positions) | 47.1% ± 6.3 | 50.4% | 34.9% | 65.6% |

OCR stage on all 98 pages: Tesseract 29.5% ± 3.9 (best models and `--clean`
did not change it); Qwen3-VL plain transcription 44.1% ± 4.3. Published
third-party `old_scans` scores: PaddleOCR-VL-1.5 39.2, Mistral OCR 3 48.8.

Adobe's Auto-Tag often wraps typed letter text in Figure tags without alt
text, and its OCR read some handwritten pages as Arabic script.

## Gateway OCR engine

`OCR_ENGINE=gateway` recognizes each page with a vision model on the CAIL
Gateway (`OCR_MODEL`, default `qwen3-vl-235b-a22b-instruct`), which returns
every line with a box; OCRmyPDF renders them as the invisible text layer.
Measured cost is about 2.5k input and 1k output tokens per page (about $0.004
at September 2026 prices), and about 25 s per page unloaded. Known failure
modes: repetition loops on dot leaders and dense tables that run to the token
cap, and occasional malformed JSON; such pages fall back to Tesseract.

## Not yet measured

- Adobe on born-digital pages, the gold and CUNY round trips, and
  `old_scans` again (its September outputs were lost): about 45 pages per
  month of free tier.
- opendataloader-pdf on the scanned samples and printed books (it needs a
  text layer, so it would need our OCR first).
- CUNY documents in languages other than English, and pages of untagged
  CUNY articles and dissertations with per-page tests.
- Alt text scored with figures paired by place on the page, not by order,
  where the author's and the app's reading orders differ.
- Born-digital articles and slides from Academic Works, which challenged
  automated downloads on 2026-10-31.
