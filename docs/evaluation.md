# Evaluation

How we measure remediation quality, and the results so far. The goal is to
beat Adobe Acrobat's OCR and Auto-Tag on the same documents; see
[cail-integration-plan.md](cail-integration-plan.md). The documents that
matter most are printed books and articles, scanned or born digital;
handwriting is a side case.

## Method

Everything is scored on what a screen reader hears: the text reachable
through the structure tree, in tag order, with artifacts left out
(`backend/app/services/structure_text.py`). Each marked-content sequence is
read as pdfium extracts it. pdfium is Chrome's PDF engine, whose
accessibility tree screen readers read: it infers word spaces from the
font's space width, expands ligatures, drops a string drawn twice over
itself (fake bold), and joins a word hyphenated at a line end. Marked-content
`/ActualText` is read in place of the glyphs it covers. Text inside a Figure
is left out unless the Figure has no alt text and the lenient reading is
asked for; tables can be kept as HTML so their cell structure is scored.

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
- **Scanned samples** (`*_scan`): the born-digital samples rendered at
  300 ppi in greyscale, turned a fraction of a degree, softened, given noise,
  and saved as JPEG (`scripts/synthetic_scan.py`), under the same tests.
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
  Every pipeline result here used docling-serve 1.35.0 (Docling 2.130) on
  CPU; production's actual-dell runs 1.12.0 (Docling 2.72) until it is
  upgraded, which loses heading levels.
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
between glyphs by our own gap rule rather than as a PDF engine does; every
number below was re-measured with the pdfium reader, which moved scores by
up to two points (Adobe on printed books 83.7% → 81.5%, v1 on multi-column
41.1% → 41.6%).

## Results (2026-10-05)

What a screen reader hears after each tool, AI steps off. "Branch,
September" is this branch before the October fixes; "Branch, now" is its
head.

| Subset (tests) | Production (v1) | Branch, September | Branch, now | opendataloader-pdf | Adobe |
|---|---|---|---|---|---|
| `old_print` (509) | 92.1% | 92.5% | **92.7%** | | 81.5% |
| `multi_column_s60` (219) | 41.6% | 59.4% | **69.4%** | 56.6% | |
| `multi_column_s60_scan` (219) | 51.6% | 50.2% | **63.5%** | | |
| `tables_s60` (336) | 10.4% | 64.5% | **69.9%** | 26.6% | |
| `tables_s60_scan` (336) | 23.0% | 56.7% | **57.0%** | | |
| `headers_footers_s60` (170) | 95.3% | **95.9%** | **95.9%** | 45.0% | |
| `headers_footers_s60_scan` (170) | **90.5%** | 89.9% | **90.5%** | | |
| `old_scans` (526) | 25.3% | 25.5% | **25.9%** | | |

| Round trip (NID / TEDS / MHS / MHS-L) | Production (v1) | Branch, September | Branch, now | opendataloader-pdf |
|---|---|---|---|---|
| `gold_rt` | 0.791 / 0.302 / 0.576 / 0.339 | 0.875 / 0.449 / **0.619** / 0.361 | **0.886 / 0.545 / 0.619 / 0.430** | 0.845 / 0.287 / 0.521 / 0.327 |
| `cuny_rt` | 0.877 / 0.512 / 0.511 / 0.350 | 0.956 / 0.683 / 0.527 / 0.317 | **0.960** / 0.683 / **0.528 / 0.391** | **0.960 / 0.739** / 0.418 / 0.328 |
| `cuny_rt_scan` | 0.893 / 0.409 / 0.501 / **0.344** | 0.934 / **0.610** / 0.516 / 0.305 | **0.938** / 0.600 / **0.518** / 0.338 | |

For scale, opendataloader-bench reports NID about 0.90 and TEDS 0.887 for
Docling's own output on its corpus.

The branch now scores above v1 on every subset but scanned headers and
footers, where they tie, and on every round-trip measure but heading levels
on the scanned CUNY documents (MHS-L 0.338 against 0.344). It beats
opendataloader-pdf on every born-digital sample and the gold round trip
(on CUNY documents it ties on order and recovers tables better), and Adobe
on printed books. Against the September branch it gains most on
multi-column pages (59.4% → 69.4%) and their scans (50.2% → 63.5%), on
tables (64.5% → 69.9%; gold TEDS 0.449 → 0.545), and on heading levels
(MHS-L 0.361 → 0.430 on gold, 0.317 → 0.391 on CUNY documents).

## What changed in October

Each change was measured on the whole suite before it was kept.

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
printed-book page, but on scans Docling often reads the page with its own
OCR, which takes characters for digits that Tesseract's layer does not, so
it rejected good glyph text and cost scanned multi-column pages about five
tests.

### On scans the element text is Tesseract's

On a scan, the glyph text is Tesseract's text layer, while Docling's text is
sometimes Tesseract's and sometimes its own RapidOCR reading. Where the two
agree within the tolerance, the tagger uses Tesseract's, which is also the
text a reader can search and copy. Each engine misreads different passages
(Tesseract "Lkept", "fora"; RapidOCR "cylindercovered", "beattracted"), so
the choice moves printed-book and scanned scores a few tests either way. A
better OCR engine, not a different choice between the two, is the fix.

### Element `/ActualText`: kept, by measurement

The PDF Association's Tagged PDF Best Practice Guide puts `/ActualText` on
spans, not on paragraphs: text a PDF engine can extract should be extracted,
and `/ActualText` on a paragraph hides a link nested in it. The tagger still
writes each element's text as `/ActualText` because the glyphs it assigns to
an element are not yet reliable enough to stand alone. Without the overlays
(measured on an earlier October build, read by the same pdfium reader),
multi-column pages score 57.1% against 68.9%,
headers and footers 93.5% against 95.9%, and the gold round trip's NID 0.877
against 0.884. Where a Link is nested, the parent's `/ActualText` is removed
so the link stays readable. Assigning content to elements correctly, so the
overlays can go, is the long-term fix.

### opendataloader-pdf

On default settings it orders CUNY documents as well as the branch (NID
0.960) and recovers their tables better (TEDS 0.739 against 0.683), but it
loses the tables in the olmOCR sample (26.6%), leaves most running heads in
(45.0%), and nests headings worse. Its reading order (XY-Cut++) is a
candidate for the branch's multi-column gap.

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
| Branch, September | 92.5% ± 2.4 | 93.8% | 88.4% | 95.9% |
| **Branch, now** | **92.7% ± 2.3** | 93.1% | **89.8%** | **97.3%** |
| Adobe OCR + Auto-Tag (October 2026) | 81.5% ± 3.2 | **94.5%** | 87.8% | 17.8% |

Gateway OCR (Qwen3-VL-235B) scored 83.3% here in September, below
Tesseract, so Tesseract stays the default. Adobe reads the most words, but
it leaves running heads, folios, and catchwords in what a screen reader
hears, so it fails most absent tests. The branch now orders printed pages
better than Adobe (89.8% against 87.8%), though order is still its weakest
measure there.

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
(`app/pipeline/page_glyphs.py`) and splits a `TJ` that spans several cells
into one per cell, which renders identically.

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
lost, so they cannot be re-read.

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
- The AI steps (title, language, alt text) on the CUNY documents.
- veraPDF failures on the gold and CUNY sets.
- CUNY documents in languages other than English, and pages of untagged
  CUNY articles and dissertations with per-page tests.
