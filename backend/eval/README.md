# Evaluation corpus

The documents and tests behind [docs/evaluation.md](../../docs/evaluation.md).
This folder holds everything needed to rebuild the corpus: `corpus.json`
names every source with its checksum or pinned revision, and our own tests
live here. The PDFs themselves are fetched into the git-ignored
`backend/data/eval/` by

```bash
cd backend
uv run python scripts/fetch_eval_corpus.py
```

which writes the layout the bench and scoring scripts read: each subset's
tests as `data/eval/olmocr-bench/<subset>.jsonl` and its PDFs in
`data/eval/olmocr-bench/bench_data/pdfs/<subset>/`. Downloads are cached in
`data/eval/sources/`.

## Subsets

| Subset | Pages | Tests | Source |
|---|---|---|---|
| `old_print` | 49 | 509 | Ours: pages from 11 public-domain books printed 1794–1925, tests from Project Gutenberg transcriptions checked against each page image ([old_print/sources.md](old_print/sources.md)) |
| `old_scans` | 98 | 526 | olmOCR-Bench, the whole subset: Library of Congress letters, mostly handwritten |
| `tables_s60` | 60 | 336 | olmOCR-Bench `tables`, a fixed sample of 60 pages |
| `multi_column_s60` | 60 | 219 | olmOCR-Bench `multi_column`, a fixed sample of 60 pages |
| `headers_footers_s60` | 60 | 170 | olmOCR-Bench `headers_footers`, a fixed sample of 60 pages |
| `gold_rt` | 224 (11 documents) | structure round-trip | Well-tagged PDFs: seven from the PDF/UA Reference Suite 1.1, the Matterhorn Protocol 1.1, Ross Moore's tagged PDF/UA paper, a NOAA report, and a table set |
| `cuny_rt` | 282 (19 documents) | structure round-trip | Well-tagged CUNY documents from [CUNY Academic Works](https://academicworks.cuny.edu/): five syllabi, two OER textbook chapters, three assignments, three lesson plans, two conference papers, lecture slides, a book chapter, a master's capstone, and a library newsletter |
| `cuny_rt_scan` | 282 (19 documents) | structure round-trip | The `cuny_rt` documents as image-only scans (`scripts/synthetic_scan.py`), scored against the same tags |

olmOCR-Bench ([allenai/olmOCR-bench](https://huggingface.co/datasets/allenai/olmOCR-bench),
ODC-BY) is read at revision `54a96a6f`, the revision used for the September
2026 measurements. The samples are drawn by the fetcher with the seed in
`corpus.json`, so they are the same 60 pages every time.

## Choosing the CUNY documents

`cuny_rt` was drawn on 2026-10-04 from a stratified random sample of 805 of
Academic Works' 33,318 PDFs. A document qualified when its tags reach at
least 95% of its page text, it has real headings and paragraphs, its
language is set, and any table marks header cells; then its heading outline
was read, and documents whose authors tagged footnotes, list items,
bibliography entries, or every slide text box as headings were dropped (about
a third), as were near-duplicate revisions. Gold tags from the wild can
still be imperfect (the newsletter marks a table's first column as row
headers), which affects every candidate alike.

## Gold PDFs

Every source file not read from a pinned dataset revision (the gold and
CUNY PDFs and the `old_print` page images) comes from the Lab's private
copy, [CUNY-AI-Lab/pdf-accessibility-eval-data](https://github.com/CUNY-AI-Lab/pdf-accessibility-eval-data),
which the fetcher clones into `data/eval/sources/lab-copy/` and checks file
by file against `corpus.json`. Origins are not reliable enough to fetch from:
pdfa.org refuses automated downloads and Ross Moore's server at Macquarie
often times out. `corpus.json` records each file's origin and license.

## Adobe baseline

`scripts/adobe_bench.py` runs Adobe's OCR and Auto-Tag within the free tier
of 500 transactions a month; Auto-Tag costs ten a page, so about 45 pages
fit in a month. Adobe's tagged outputs are kept in the Lab's copy under
`adobe/` so they are never paid for twice.

## Rules

- A test or source changes only with a commit that says why, and the
  results in `docs/evaluation.md` are re-measured on the changed corpus.
- Measurements name the docling-serve version they ran against; structure
  results depend on it.
