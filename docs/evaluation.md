# Evaluation

How we measure remediation quality, and the results so far. The goal is to
beat Adobe Acrobat's OCR and Auto-Tag on the same documents; see
[cail-integration-plan.md](cail-integration-plan.md). The documents that
matter most are printed books and articles, scanned or born digital;
handwriting is a side case.

## Method

Everything is scored on what a screen reader hears: the text reachable through
the structure tree, in tag order, with artifacts left out
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
  `backend/data/eval/olmocr-bench/old_print_sources.md`.
- **OCR stage:** `backend/scripts/ocr_bench.py` runs the app's own OCRmyPDF
  command inside the production image and reads the raw text layer.
- **Full pipeline:** `backend/scripts/run_pipeline_bench.sh` runs
  `run_pipeline` (classify, OCR, Docling structure through docling-serve as in
  production, tagging, validation) on each page in the production image and
  reads the tagged result. The app's LLM steps are off in these runs.
- **Adobe:** `backend/scripts/adobe_bench.py` runs Adobe PDF Services OCR then
  Auto-Tag, two transactions per page. The free tier ran out after 44
  `old_scans` pages in September 2026.
- **Data** lives in `backend/data/eval/` (git-ignored).

## Results: printed books (`old_print`, 49 pages, 509 tests)

OCR stage, raw text layer:

| Candidate | Score | Present | Order | Absent |
|---|---|---|---|---|
| Tesseract 5.3 (production) | 59.9% ± 4.0 | 62.6% | 76.2% | 16.4% |
| Qwen3-VL-235B line spotting via the Gateway (`OCR_ENGINE=gateway`) | 66.2% ± 3.9 | 73.7% | 84.4% | 0.0% |

"Absent" tests expect running heads and page numbers to be left out. Both OCR
engines keep them, as they should; the tagger is what must mark them as
artifacts, which the full-pipeline runs measure.

## Results: manuscripts (`old_scans`)

Library of Congress letters (Clara Barton, Theodore Roosevelt, and Joseph Holt
papers), mostly handwritten, some typed. A side case, kept because Adobe was
measured here.

On the 44 pages Adobe finished (240 tests):

| Candidate | Score | Present | Order | Absent |
|---|---|---|---|---|
| Production pipeline, as heard | 17.1% ± 4.6 | 6.4% | 1.2% | 100% |
| Adobe OCR + Auto-Tag, as heard | 20.8% ± 4.8 | 11.2% | 6.0% | 96.9% |
| Adobe, counting text inside alt-less Figures | 22.5% ± 5.2 | 12.8% | 8.4% | 96.9% |
| Tesseract text layer (OCR stage) | 39.2% ± 6.2 | 55.2% | 2.4% | 71.9% |
| Qwen3-VL-235B plain transcription (no positions) | 47.1% ± 6.3 | 50.4% | 34.9% | 65.6% |

On all 98 pages (526 tests), OCR stage: Tesseract 29.5% ± 3.9 (best models and
`--clean` did not change it); Qwen3-VL plain transcription 44.1% ± 4.3.
Published third-party `old_scans` scores: PaddleOCR-VL-1.5 39.2, Mistral OCR 3
48.8.

- The production pipeline hears far less than its own OCR produced: the tagger
  kept one text object per paragraph and marked the rest of an OCR'd
  paragraph's lines as artifacts. Fixed in the tagger; the fixed pipeline is
  being measured.
- Adobe loses text the same way at a smaller scale: it hears about a third of
  its own text layer. Auto-Tag often wraps typed letter text in Figure tags
  without alt text, and its OCR read some handwritten pages as Arabic script.

## Gateway OCR engine

`OCR_ENGINE=gateway` recognizes each page with a vision model on the CAIL
Gateway (`OCR_MODEL`, default `qwen3-vl-235b-a22b-instruct`), which returns
every line with a box; OCRmyPDF renders them as the invisible text layer.
Measured cost is about 2.5k input and 1k output tokens per page (about $0.004
at September 2026 prices), and about 25 s per page unloaded. Known failure
modes: repetition loops on dot leaders and dense tables that run to the token
cap, and occasional malformed JSON; such pages fall back to Tesseract.

## Not yet measured

- Full pipeline on `old_print`: production, fixed tagger, and fixed tagger
  with Gateway OCR (running).
- Born-digital pages: `multi_column` (reading order), `headers_footers`
  (artifacts), `tables` (cell structure from the tags).
- Structure fidelity on well-tagged PDFs (strip-and-restore): headings, lists,
  figures and alt text, title, language, veraPDF failures.
- The app's LLM steps on Gateway models.
