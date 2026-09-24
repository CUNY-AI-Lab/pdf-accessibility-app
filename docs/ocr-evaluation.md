# OCR evaluation

How we measure OCR quality on scanned pages, and the results so far. The goal
is to beat Adobe Acrobat Pro on the same documents; see
[cail-integration-plan.md](cail-integration-plan.md).

## Method

- **Benchmark:** [olmOCR-Bench](https://huggingface.co/datasets/allenai/olmOCR-bench)
  (Ai2, ODC-BY). Each test checks one page: a passage must be present, a
  passage must be absent, or one passage must come before another. Scores are
  the share of tests passed, with a 95% bootstrap confidence interval.
- **Harness:** `backend/scripts/ocr_bench.py` runs the app's own OCRmyPDF
  command (`_build_ocrmypdf_args`) inside the production image, extracts the
  text layer with pdfminer (Poppler's `pdftotext` when pdfminer cannot parse
  the file), and writes the Markdown files the scorer expects. Score with
  `python -m olmocr.bench.benchmark --dir <view> --skip_baseline`.
- **Data** lives in `backend/data/eval/` (git-ignored). Only pages without a
  text layer measure OCR: the app skips OCR on pages that already have text.

## Results: `old_scans` (98 pages, 526 tests)

All 98 pages are Library of Congress manuscript letters (Clara Barton,
Theodore Roosevelt, and Joseph Holt papers), mostly handwritten or typed.

| Candidate | Score | Present | Order | Absent |
|---|---|---|---|---|
| Production: Tesseract 5.3, Debian fast models, `eng` | 29.5% ± 3.9 | 37.3% | 3.4% | 64.3% |
| Tesseract best models (`tessdata_best`) | 29.5% ± 3.9 | 38.0% | 3.4% | 61.4% |
| Best models + `--clean` (unpaper) | 29.5% ± 3.9 | 38.0% | 3.4% | 61.4% |
| Qwen3-VL-235B via the CAIL Gateway, plain transcription | 44.1% ± 4.3 | 48.7% | 35.0% | 48.6% |

- `--oversample 300` could not be measured as configured: on large pages it
  exceeds the production `--max-image-mpixels 75` guard (13 of 98 pages).
- Qwen3-VL's transcription is text only, with no positions; it is an upper
  bound on recognition, not yet a usable text layer. Its lower "absent" score
  comes from transcribing page furniture the tests expect to be left out.
- Published `old_scans` scores for reference: PaddleOCR-VL-1.5 39.2, Mistral
  OCR 3 48.8 (third-party runs).

## Not yet measured

- Printed books: `old_scans` has none.
- What a screen reader finally hears after Docling and the tagger (the
  full-pipeline track), including words the tagger marks as artifacts.
- The Adobe baseline (Auto-Tag API).
