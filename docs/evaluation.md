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
  Pipeline results here used docling-serve 1.35.0 (Docling 2.130), on CPU
  through October 5 and on the GPU since; production's actual-dell runs
  1.12.0 (Docling 2.72) until it is upgraded, which loses heading levels.
- **Re-tagging:** a pipeline run keeps what the tagger was given for each
  page (`scripts/pipeline_bench.py`), and `scripts/retag_bench.py` tags every
  page again with other tagger code in about a minute, without OCR and
  Docling. The whole categories were measured that way, from inputs captured
  with docling-serve on the Mac Studio's GPU (MPS, one worker: more crash
  PyTorch's Metal backend), which scores the same as on CPU. Re-tagging does
  not replay the font repairs the pipeline makes to many born-digital tagged
  PDFs afterwards (24 of the multi-column sample's 60 pages, 10 of the
  tables sample's), so on born-digital pages it can differ from a pipeline
  run by a few tests (two on each of those samples).
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
mostly hurt tools that tag without `/ActualText`. Every number below was
re-measured with the current reader (opendataloader-pdf on multi-column
pages 56.6% → 64.8%, Adobe on printed books 81.5% → 83.7%).

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
- The AI steps (title, language, alt text) on the CUNY documents.
- veraPDF failures on the gold and CUNY sets.
- CUNY documents in languages other than English, and pages of untagged
  CUNY articles and dissertations with per-page tests.
