# Evaluation

How we measure remediation quality, and the results so far. The goal is to
beat Adobe Acrobat's OCR and Auto-Tag on the same documents; see
[cail-integration-plan.md](cail-integration-plan.md). The documents that
matter most are printed books and articles, scanned or born digital;
handwriting is a side case.

## Method

Everything is scored on what a screen reader hears: the text reachable through
the structure tree, in tag order, with artifacts left out and marked-content
`/ActualText` read in place of the glyphs it covers
(`backend/app/services/structure_text.py`). Text inside a Figure is left out
unless the Figure has no alt text and the lenient reading is asked for; tables
can be kept as HTML so their cell structure is scored.

- **Benchmark:** [olmOCR-Bench](https://huggingface.co/datasets/allenai/olmOCR-bench)
  (Ai2, ODC-BY). Each test checks one page: a passage must be present, a
  passage must be absent (running heads, page numbers), one passage must come
  before another, or a table cell must have given neighbors and headings.
  Scores are the share of tests passed, with a 95% bootstrap confidence
  interval.
- **`old_print`** (our addition, 49 pages, 509 tests): pages from 11
  public-domain books printed 1794–1925, from Internet Archive scans, with
  tests taken from Project Gutenberg transcriptions of the same editions and
  checked by eye against each page image. Sources are listed in
  [backend/eval/old_print/sources.md](../backend/eval/old_print/sources.md).
- **`cuny_rt`** (our addition, 19 documents, 282 pages): well-tagged CUNY
  course materials and papers from CUNY Academic Works, scored like the gold
  round-trip below; `cuny_rt_scan` is the same documents as image-only scans.
  How they were chosen is in [backend/eval/README.md](../backend/eval/README.md).
- **OCR stage:** `backend/scripts/ocr_bench.py` runs the app's own OCRmyPDF
  command inside the production image and reads the raw text layer.
- **Full pipeline:** `backend/scripts/run_pipeline_bench.sh` runs
  `run_pipeline` (classify, OCR, Docling structure through docling-serve as in
  production, tagging, validation) on each page and reads the tagged result.
  The app's AI steps are off unless a run names a Gateway model (`--llm`).
  Every pipeline result here used docling-serve 1.35.0 (Docling 2.130) on
  CPU; production's actual-dell runs 1.12.0 (Docling 2.72) until it is
  upgraded, which loses heading levels.
- **AI steps:** `backend/scripts/score_semantics.py` compares the title,
  language, and figure alt text of each remediated gold document with the
  author's. A judge model (`deepseek-v4-pro-0813` on the Gateway) scores
  titles and alt text 1–5 for how much of what the author's version tells a
  reader the remediated one tells them too; wording may differ.
- **Adobe:** `backend/scripts/adobe_bench.py` runs Adobe PDF Services OCR then
  Auto-Tag within the free tier of 500 transactions a month. Auto-Tag costs
  ten transactions a page, so about 45 pages fit in a month: `old_scans` (44
  pages) in September 2026, `old_print` (all 49) in October.
- **Corpus:** defined in [backend/eval/](../backend/eval/README.md) and
  rebuilt byte for byte by `backend/scripts/fetch_eval_corpus.py`. The
  original data was git-ignored and lost with its worktree in late September
  2026; on 2026-10-04 the rebuilt corpus reproduced every September
  measurement that could be re-run (NID within 0.002, all else exactly).
  Adobe's `old_scans` outputs were lost and wait for a month's free tier.

**Correction (2026-09-24).** Results published here before this date read the
structure tree without marked-content `/ActualText`, so they badly understated
production, whose tagger carries OCR text that way (printed books appeared to
go from 30.5% to 91.7%). Every number below was measured with the corrected
reader.

## Results: printed books (`old_print`, 49 pages, 509 tests)

What a screen reader hears after the full pipeline:

| Candidate | Score | Present | Order | Absent |
|---|---|---|---|---|
| Production (v1) | 92.1% ± 2.3 | 94.1% | 86.4% | 95.9% |
| Current branch, Tesseract | **92.5% ± 2.3** | 93.8% | 88.4% | **95.9%** |
| Adobe OCR + Auto-Tag (October 2026) | 83.7% ± 3.0 | **95.8%** | **93.9%** | 15.1% |
| Current branch, Gateway OCR (Qwen3-VL-235B) | 83.3% ± 3.3 | 83.7% | 76.2% | 95.9% |

Production already handles printed books well. Gateway OCR does worse than
Tesseract here, so Tesseract stays the default. Adobe reads the words and
their order better than any candidate (order 93.9% against the branch's
88.4%), but it leaves running heads, folios, and catchwords in what a screen
reader hears, so it fails most absent tests. Reading order on printed pages
is the gap v2 has to close.

OCR stage alone (raw text layer read by pdfminer; its own layout guessing
scrambles Tesseract's order, so these understate what the pipeline hears):
Tesseract 59.9% ± 4.1, Qwen3-VL line spotting 66.2% ± 4.2 (67.4% with the
retry ladder).

## Results: born-digital pages (random samples of 60 pages each)

| Subset (tests) | Production (v1) | Current branch |
|---|---|---|
| `tables` (cell neighbors and headings, 336) | 10.1% ± 3.3 | **64.8% ± 5.4** |
| `multi_column` (reading order, 219) | 41.1% ± 6.4 | **58.0% ± 6.4** |
| `headers_footers` (absent tests, 170) | 95.3% ± 3.3 | 95.9% ± 2.7 |

Docling found these tables correctly; the production tagger lost them. It
guessed text positions from its own content-stream reading (ignoring the text
matrix's scale on `Td`, font widths, and `TJ` offsets, and reading `TJ`
offsets as text), and it tagged a table row drawn by one `TJ` as one cell.
The branch takes positions and text from pdfminer's measurement of each text
operator (`app/pipeline/page_glyphs.py`) and splits a `TJ` that spans several
cells into one per cell, which renders identically.

## Results: scanned articles (the same samples as image-only scans)

The born-digital samples above, rendered at 300 ppi in greyscale, turned a
fraction of a degree, softened, given noise, and saved as JPEG
(`scripts/synthetic_scan.py`), under the same tests.

| Subset (tests) | Production (v1) | Current branch |
|---|---|---|
| `tables_s60_scan` (336) | 22.7% ± 4.5 | **56.1% ± 5.4** |
| `multi_column_s60_scan` (219) | 50.7% ± 6.2 | 49.8% ± 6.8 |
| `headers_footers_s60_scan` (170) | 90.5% ± 4.4 | 89.9% ± 4.4 |

The branch keeps most of its table gain on scans but none of its
multi-column gain, which came from measuring born-digital text positions; on
a scan both versions read Tesseract's text layer. Scanned multi-column
articles, about half right in either version, are the largest gap left
among the documents that matter most.

## Results: structure round-trip (11 gold documents)

Gold documents (the PDF/UA reference suite, the Matterhorn Protocol, a NOAA
report, a PDF/UA paper, a table set; 224 pages, 322 headings, 62 tables) have
their tags stripped (`scripts/strip_accessibility.py`), go through the
pipeline, and the resulting tag tree is compared with the original, both
rendered as Markdown. Metrics are opendataloader-bench's: NID (text in
reading order), TEDS (table tree edit similarity), MHS (section tree), plus
MHS-L, which also counts heading levels.

| | NID | TEDS | MHS | MHS-L |
|---|---|---|---|---|
| Production (v1) | 0.792 | 0.301 | 0.576 | 0.339 |
| Current branch | **0.876** | **0.449** | **0.620** | **0.361** |

For scale, opendataloader-bench reports NID about 0.90 and TEDS 0.887 for
Docling's own output on its corpus.

## Results: CUNY documents (`cuny_rt`, 19 documents, 282 pages)

The same round trip on well-tagged CUNY documents from Academic Works:
syllabi, OER chapters, assignments, lesson plans, slides, two conference
papers, a book chapter, a capstone, and a library newsletter.

| | NID | TEDS | MHS | MHS-L |
|---|---|---|---|---|
| Production (v1) | 0.879 | 0.467 | 0.498 | **0.345** |
| Current branch | **0.956** | **0.715** | **0.513** | 0.310 |
| Production (v1), scanned (`cuny_rt_scan`) | 0.893 | 0.409 | 0.501 | **0.344** |
| Current branch, scanned | **0.934** | **0.610** | **0.516** | 0.305 |

The scans are the same pages rendered at 300 ppi in greyscale, turned a
fraction of a degree, softened, given noise, and saved as JPEG, so the
pipeline has to recognize them with Tesseract. v1 orders the scans better
than the born-digital originals, because there it reads Tesseract's text
layer instead of guessing positions from the content stream.

The branch reads CUNY documents in a far better order and recovers their
tables, but it nests headings worse than v1 by MHS-L, the one regression in
the suite. Four of the seven documents that lose most were tagged by their
authors with every heading at level 1 (two syllabi, an assignment, the
newsletter); v1 also puts every heading at level 1, so it matches them,
while the branch nests. The two conference papers lose with two-level gold,
so part of the loss is real.

## Results: AI steps (model bake-off, gold documents)

The same 11 gold documents, remediated by the current branch with its AI
steps on, each run with a different model. The gold set has 163 figures with
author-written alt text.

| Model | Title (1–5) | Figures given alt text | Alt text (1–5) |
|---|---|---|---|
| None (AI steps off) | 3.91 | 49 of 163 | 3.90 |
| Gemini 3 Flash Preview (production until now) | 4.09 | 124 of 163 | 3.76 |
| Gemini 3.8 Flash | 4.09 | 124 of 163 | 3.76 |
| **Qwen3-VL-235B (Gateway)** | **4.27** | 110 of 163 | 3.75 |

Every candidate set the right language on all 11 documents. The model makes
no difference to structure: NID, TEDS, and MHS stay within 0.005 of the run
without AI steps, and the `tables` sample scores 64.8% without AI steps,
65.1% with Gemini 3.8 Flash, and 64.8% with Qwen3-VL. Without AI steps, alt
text comes only from figure captions, so it covers few figures.

Qwen3-VL writes alt text for 14 fewer figures than Gemini. Eight of those are
screenshots of tables in one document: Qwen3-VL decides each is a table, not
an image, and leaves it for manual review. The alt text it does write
judges as well as Gemini's (3.75 against 3.76). Qwen3-VL is the default
model.

Mistral Large 3 (at most three images per request), Kimi K2.5, and
Gemma 3 27B (repeated timeouts) were dropped before scoring.

## Results: manuscripts (`old_scans`)

Library of Congress letters (Clara Barton, Theodore Roosevelt, and Joseph Holt
papers), mostly handwritten, some typed. A side case, kept because Adobe was
measured here.

On the 44 pages Adobe finished (240 tests):

| Candidate | Score | Present | Order | Absent |
|---|---|---|---|---|
| Production pipeline, as heard | 24.2% ± 5.6 | 15.2% | 8.4% | 100% |
| Current branch, as heard | 24.2% ± 5.8 | 15.2% | 8.4% | 100% |
| Adobe OCR + Auto-Tag, as heard | 19.2% ± 5.2 | 8.8% | 4.8% | 96.9% |
| Adobe, counting text inside alt-less Figures | 21.2% ± 5.2 | 11.2% | 7.2% | 96.9% |
| Tesseract text layer (OCR stage) | 39.2% ± 6.2 | 55.2% | 2.4% | 71.9% |
| Qwen3-VL-235B plain transcription (no positions) | 47.1% ± 6.3 | 50.4% | 34.9% | 65.6% |

On all 98 pages (526 tests): production 25.3% ± 3.7, current branch
25.5% ± 3.9. OCR stage: Tesseract 29.5% ± 3.9 (best models and `--clean` did
not change it); Qwen3-VL plain transcription 44.1% ± 4.3. Published
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
- The AI steps (title, language, alt text) on the CUNY documents.
- veraPDF failures on the gold and CUNY sets.
- CUNY documents in languages other than English, and pages of untagged
  CUNY articles and dissertations with per-page tests.
