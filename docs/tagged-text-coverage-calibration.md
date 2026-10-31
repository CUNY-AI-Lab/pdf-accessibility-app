# Tagged text coverage calibration, 2026-10-30

Coverage has two tiers. It blocks only measured catastrophic text loss.
Other coverage below 97% produces advisory review tasks naming affected PDF
pages. The no-header table check remains blocking.

The 90% gate was too sensitive to picture OCR, handwriting, and furniture.
After the first Figure and pagination exemptions, it held 230 of 1,926
pipeline outputs; a 15-document sample contained 2 genuine and 13 false holds.
The catastrophic policy reduced coverage blocks to 55, but a fresh sample
contained 7 genuine and 13 false blocks. That sample included off-CropBox
text and Artifact OCR inside pictures. This iteration excludes those words
when their page and Figure geometry establish that they are outside the
visible page or inside a tagged picture.

The catastrophic thresholds remain unchanged. They preserve the motivating
case of a substantial body disappearing from the tag-order reading. The
German checklist still blocks at 7.79% coverage. Smaller omissions remain
advisory. The thresholds and the 60% spatial overlap rule were set in review
before the new sample was read; none were adjusted after reading it.

## Final rules

Coverage is the multiset intersection of normalized eligible raw words and
the tag-order reading, calculated overall and separately for every page.
Normalization uses Unicode NFKC, case folding, soft-hyphen removal, and
punctuation splitting.

**Block measured coverage only when either condition is true:**

- Overall coverage is **strictly below 0.50** and at least **50 words** are missing.
- At least one page has **100 or more eligible words**, coverage **strictly
  below 0.50**, and at least **50 missing words on that page**.

The overall condition has no 100-word floor. Exactly 50% coverage is advisory,
not blocking. A page can block even when overall coverage remains high.

**Advisory:** otherwise, overall coverage below **0.97**, or any page below
**0.97**, creates a nonblocking review task. There is no minimum page-word or
missing-word floor for advisories. Exactly 97% passes unless another page
falls below 97%. The task detail and `pages_to_check` list every affected page;
metrics separately record `blocking_pages` and `advisory_pages`. A document
with a catastrophic block receives one blocking coverage task, with all
sub-97% pages listed for review. An unreadable check still records its error
and fails closed; an unknown coverage value is not treated as a pass.

The following exemptions and reader behavior remain in place:

- Exclude characters mapped to Figure or Formula descendant marked content,
  whether or not Alt/ActualText is usable. Keep inherited `/Pg`, MCR `/Pg`
  and `/Stm`, page/Form stream paths, nested MCIDs, and marked Form invocations.
  This marked-content mapping is retained separately from the spatial rule below.
- Exclude words wholly outside the visible page box: PDFium's intersection of
  MediaBox and CropBox, including inherited boxes. Word and page bounds use the
  same PDF page coordinates, including on rotated pages. A word with any
  visible area remains eligible under this rule.
- Exclude an Artifact word when at least **60% of its box area** lies inside
  a tagged Figure's bounds on the same page. All characters of the word must
  belong to Artifact marked content. Prefer the Figure's direct `/BBox` or
  its Layout attribute `/BBox`. The tagger does not write Figure `/BBox`, so
  its outputs use the union of that Figure's descendant marked-content
  painted bounds. Include page/Form MCID scope, Form transforms, inherited
  marks, and clip bounds. A clipped full-page image does not exempt unrelated
  body text outside its clipped Figure. Non-Artifact words and pictures
  without a tagged Figure receive no spatial Figure exemption.
- Exclude whole words in Artifact content with `/Type /Pagination` or
  `/Subtype /Header` or `/Footer`, when the word center lies in the displayed
  top/bottom **10%** bands. This applies to documents of any length. The tagger
  writes Pagination plus Header/Footer and Attached Top/Bottom. Named
  property dictionaries and Form content/invocations are supported. Body-band
  text and untyped artifacts receive no exemption from this rule.
- Keep repeated margin words at the same position on **3 or more pages** as
  furniture. Top/bottom and left/right margins use 10% bands, positions use
  1% bins, and digits are folded for page numbers. Typed furniture still
  supplies repetition evidence for untyped copies on other pages. Repeated
  interior body text stays eligible.
- Ignore an unmarked duplicate glyph only when an identical marked glyph has
  all four box edges within **0.5 point**. Unmarked text elsewhere remains eligible.

The separate figure-description check still holds undescribed Figure elements.
The visual-gap fail-closed change and screen-reader fixes remain in place.
Coverage warnings do not override another blocking check.

**No-header hold:** two-column tables need **3 or more rows**; tables with
**3 or more columns** need **2 or more rows**. A qualifying table without
TH cells still blocks. Summaries do not supply header semantics. Role mapping,
page references, and column spans retain their existing handling. All twelve
2×2 tables remain excluded, reducing the earlier 176 findings to 164.

Metrics retain `excluded_figure_words`, `excluded_pagination_words`, and
`excluded_furniture_words`. The existing `excluded_described_words` key is an
alias for the combined Figure/Formula exclusion count. New metrics
`excluded_off_page_words` and `excluded_artifact_figure_words` record the two
spatial exclusions. Counts are applied after duplicate removal; off-page
words are removed before Figure and furniture counts, so exclusions are not
double-counted.

## Pipeline outputs: before → after spatial exclusions

All 1,926 existing `retag_main` outputs were checked again, without rerunning
the pipeline or making network/model calls. AI steps were disabled when these
outputs were generated. They are distinct from the author-tagged originals
of similarly named subsets. Both sides below already use the catastrophic
thresholds, the earlier Figure/pagination exemptions, and the revised
no-header table-size rule.

| Subset | PDFs | Coverage blocks | No-header holds | Either check |
|---|---:|---:|---:|---:|
| cuny_rt | 19 | 0 → 0 | 6 → 6 | 6 → 6 |
| cuny_rt_scan | 19 | 1 → 1 | 7 → 7 | 8 → 8 |
| gold_rt | 11 | 2 → 0 | 5 → 5 | 6 → 5 |
| headers_footers | 266 | 15 → 11 | 7 → 7 | 22 → 18 |
| headers_footers_s60 | 60 | 5 → 4 | 3 → 3 | 8 → 7 |
| headers_footers_s60_scan | 60 | 3 → 2 | 5 → 5 | 8 → 7 |
| headers_footers_scan | 266 | 5 → 3 | 9 → 9 | 14 → 12 |
| multi_column | 231 | 5 → 3 | 4 → 4 | 9 → 7 |
| multi_column_s60 | 60 | 2 → 1 | 2 → 2 | 4 → 3 |
| multi_column_s60_scan | 60 | 1 → 1 | 2 → 2 | 3 → 3 |
| multi_column_scan | 231 | 3 → 3 | 5 → 5 | 8 → 8 |
| old_print | 49 | 0 → 0 | 2 → 2 | 2 → 2 |
| old_scans | 98 | 5 → 4 | 3 → 3 | 8 → 7 |
| tables | 188 | 5 → 3 | 30 → 30 | 35 → 33 |
| tables_s60 | 60 | 0 → 0 | 9 → 9 | 9 → 9 |
| tables_s60_scan | 60 | 1 → 1 | 7 → 7 | 8 → 8 |
| tables_scan | 188 | 2 → 2 | 28 → 28 | 30 → 30 |
| **Total** | **1926** | **55 → 39** | **134 → 134** | **188 → 173** |

Coverage blocks fell from **55 to 39** (**2.02%** of outputs). Advisories
changed from **367 to 341**, and passes from **1,504 to 1,546**. No new coverage
blocks appeared. No-header holds remain **134**. Holds for either check fell
from **188 to 173** (**8.98%**). Both checks completed without errors.

The new exclusions removed **1,858 off-page words** and **6,198 additional
Artifact words inside tagged Figures** from these outputs. Figure/Formula
exclusions now total **11,986 words**, including those additional Artifact
words. Typed pagination excludes **20,316 words** after the spatial exclusions.

The cropped article
`multi_column/0925342e1efabb7957efa34203f65d3cbc4d_page_9_pg1.tagged.pdf`
now passes at **98.5%** after removing **523 off-page words**. The German
checklist still has **367 missing words out of 398 eligible words**.

For comparison, the pre-exemption local commit `a2a4ce6` held 328 outputs for
coverage and 138 for no-header tables, affecting 435 PDFs. The earlier
Figure/pagination and table-size changes reduced these to 230, 134, and 345.
The catastrophic policy reduced them to 55, 134, and 188. The final spatial
exclusions reduce them to 39, 134, and 173.

## Author-tagged originals: before → after spatial exclusions

All 30 originals were checked again: 11 gold and 19 CUNY PDFs.

| Subset | PDFs | Coverage blocks | No-header holds | Either check |
|---|---:|---:|---:|---:|
| cuny_rt | 19 | 0 → 0 | 2 → 2 | 2 → 2 |
| gold_rt | 11 | 0 → 0 | 0 → 0 | 0 → 0 |
| **Total** | **30** | **0 → 0** | **2 → 2** | **2 → 2** |

Coverage remains **0 blocks, 9 advisories, and 21 passes**. There are no
coverage blocks to inspect in this corpus. The spatial rules exclude
**320 off-page words** and **40 additional Artifact words inside tagged
Figures**. Both checks completed without errors.

The no-header holds on `gc_etds_6742.pdf` and `qc_oers_10.pdf` remain blocking.
Earlier inspection found genuine survey-table header gaps in the former and
a false hold on a syllabus layout table in the latter. The table policy is
unchanged. The magazine cover and `qb_oers_7.pdf` chart-axis losses remain
advisory. These two checks do not establish full remediation readiness or
PDF/UA compliance.

## Inspection of remaining coverage blocks

There are **39** coverage-blocking outputs, above the 30-document cutoff.
A fresh **20-document** sample was selected from the sorted block list by
shuffling with seed **20261101** and taking the first 20 distinct output
filenames. Every page triggering a page-level block was inspected. For an
overall-only block, pages below 50% with missing words were inspected. This
required **21 rendered pages**. Renderings, missing-word boxes, and tag-order
readings were compared. The selection and page measurements were verified
against the completed full rerun; the sample was not rerolled after inspection.

The sample contains **12 genuine losses and 8 false coverage blocks**.
Remaining false blocks involve graphic OCR and handwriting that the new
rules cannot establish as Artifact text inside a tagged Figure. One false
catastrophic block includes a short real body omission whose review is still
appropriate. The sample supports a reduction in false blocks but does not
establish that every remaining block is genuine. No further exemption or
threshold change was made from this sample.

| # | Subset / output | Triggering PDF pages | Eligible / missing words | Classification and evidence |
|---|---|---|---|---|
| 1 | `headers_footers/68adea4d76e152a7abebfb39dc0d851fe5695a84_page_4.tagged.pdf` | 1 | p1: 665 / 665 | **Genuine.** The tag-order reading is empty. Article Discussion paragraphs and a figure caption are absent: all 665 eligible words. |
| 2 | `headers_footers/65bcb3969d0566759e3c06271a89377a111d5ca2_page_2.tagged.pdf` | 1 | p1: 683 / 683 | **Genuine.** The tag-order reading is empty. All 683 eligible words of a two-column energy-systems article page are absent. |
| 3 | `multi_column/0c6c8f76cdecc74698360d34af4d58e721a0_page_1_pg1.tagged.pdf` | 1 | p1: 904 / 904 | **Genuine.** The tag-order reading is empty. The paper title, abstract, and body text are absent: all 904 eligible words. |
| 4 | `headers_footers_scan/3f4659b0291c5acbd144111c14668eab984b5ae4_page_1.tagged.pdf` | 1 | p1: 456 / 253 | **Genuine.** The blood-drive article is read, but substantial readable IBM advertisement copy below the portrait is absent. This prose accounts for most of the 253 missing words. |
| 5 | `multi_column_scan/081875a1035e34dee439b9a2a3a55e319405_page_17_pg1.tagged.pdf` | 1 | p1: 770 / 517 | **False.** The explanatory paragraphs and side panels are read. Missing tokens are periodic-table graphic lettering, element symbols, numbers, and OCR fragments. Some graphic tokens remain outside the qualifying Figure/artifact geometry. |
| 6 | `headers_footers/39e0e582e2aeb751d615308c4486e5d9422a37db_page_2.tagged.pdf` | 1 | p1: 59 / 59 | **Genuine.** The tag-order reading is empty. The French seminar cover and paper title lose all 59 eligible words. This is an overall-only block. |
| 7 | `tables_scan/2ad3ead4f2ead0eabe58d10674f3c1bfef6b_pg27.tagged.pdf` | 1 | p1: 144 / 99 | **False.** The summary table and introductory text are read. Missing tokens are chart lettering, sleep-quality question labels, and footer OCR. |
| 8 | `headers_footers_s60/29019be34d0c59210165af50a8904f749d7c0678_page_1.tagged.pdf` | 1 | p1: 398 / 367 | **Genuine.** The German safety checklist reads headings and short notes but omits its checklist items: 367 of 398 eligible words are missing (7.79% coverage). |
| 9 | `tables_scan/3d780cdcc987c65fdc7e1628c6a32af33d5d_pg22_pg1.tagged.pdf` | 1 | p1: 223 / 218 | **False.** A composite map graphic with an embedded table and legend labels. The caption is read; the missing tokens belong to the graphic. |
| 10 | `multi_column_scan/00f6c2eea6b51fb1bf637b5f8a850588d7e2_page_11_pg1.tagged.pdf` | 1 | p1: 562 / 353 | **False.** Figure captions and conclusion paragraphs are read. Chart axes, confusion-matrix labels, and OCR fragments account for most missing tokens. |
| 11 | `headers_footers/038704b29be26b6d45233e5e11218273d372957e_page_2.tagged.pdf` | 1 | p1: 405 / 405 | **Genuine.** The tag-order reading is empty. The Spanish/English paper title, abstracts, and author information are absent: all 405 eligible words. |
| 12 | `old_scans/92.tagged.pdf` | 1 | p1: 161 / 154 | **False.** Handwriting OCR is unreliable, with fragments including “aeypwet”, “atictl”, and “ceanher”. The measured loss does not establish catastrophic body text hidden by tagging. |
| 13 | `headers_footers_scan/0bdb95a688274d4700bb459e1d9e98e795bbbd05_page_25_processed.tagged.pdf` | 1 | p1: 160 / 156 | **False.** The Google Earth/KML slide title is read. Missing tokens are screenshot/code lettering and footer OCR rather than a hidden body passage. |
| 14 | `tables/105e91a0c9e26af4bb6fc23e13067d596a81_pg1_pg1.tagged.pdf` | 1 | p1: 249 / 125 | **Genuine.** The product description and features are read, but the ordering table is absent, including catalog identifiers, sizes, quantities, and weights. Some footer/logo words also contribute to the 125 missing words. |
| 15 | `headers_footers_s60/0f692b1bde8c3e04d1ec47ae43d616fc5c2ff2a9_page_23_processed.tagged.pdf` | 1 | p1: 1508 / 1508 | **Genuine.** The tag-order reading is empty. All 1,508 eligible words are absent, including bibliography entries and an author biography. |
| 16 | `tables_s60_scan/fbeb6edcf6ad16adbd2a27b54da0c1771389_pg1.tagged.pdf` | 1 | p1: 217 / 217 | **False.** An engineering drawing with dimensions, labels, a parts list, and title-block lettering. The missing tokens are graphic content rather than a hidden body page. |
| 17 | `multi_column/0114396ecb251aa3ebaeaeaed6bd4fe50bd2_page_2_pg1.tagged.pdf` | 1 | p1: 789 / 789 | **Genuine.** The tag-order reading is empty. All 789 eligible words of the article body and methods paragraphs are absent. |
| 18 | `headers_footers/d93aa3ed6cf33401e7427cff9f1feada5bae46e7_page_4.tagged.pdf` | 1 | p1: 698 / 698 | **Genuine.** The tag-order reading is empty. All 698 eligible words of discussion, conclusion, and references are absent. |
| 19 | `cuny_rt_scan/jj_arch_pubs_99.tagged.pdf` | 14, 18 | p14: 132 / 84; p18: 103 / 55 | **False.** Both triggering pages were inspected. Page 14 loses screenshot OCR and a short instruction paragraph while the following paragraph and closing are read. Page 18 loses infographic lettering. Graphic OCR pushes these losses into the catastrophic tier; the short real body omission still warrants advisory review. |
| 20 | `multi_column_scan/0d7f2cbc54e1b8e58b42a867c3f7620bcd29_page_8_pg1.tagged.pdf` | 1 | p1: 448 / 273 | **Genuine.** Material-description paragraphs are read, but substantial bulleted technical specifications are absent. Those body lists account for most of the 273 missing words. |

## Reproduction and validation

Read-only inputs:

- Captured outputs:
  `../pdf-a11y-eval-capture/backend/data/eval/olmocr-bench/bench_data/retag_main/`.
- Original measurement scripts/results:
  `../pdf-a11y-eval-capture/backend/data/eval/branch-measure-2026-10-30/`.
- Author originals:
  `/Users/stephenzweibel/Apps/cail/pdf-accessibility-eval-data/gold_rt/`
  and `cuny_rt/`.

The local adapter is `scratch/calibrate.py`. The comparison files are
`scratch/before-spatial.jsonl` and `scratch/spatial-final.jsonl`, each with
all **1,956 PDFs**. Earlier stages remain in `scratch/before.jsonl`,
`scratch/after.jsonl`, and `scratch/catastrophic.jsonl`.

The fresh inspection is reproducible with `scratch/inspect_spatial.py`.
`scratch/spatial-inspection/` contains `sample.json`, `index.json`,
`classifications.json`, per-page JSON, rendered PNGs, and contact sheets.
Earlier samples remain in `scratch/inspection/` and
`scratch/catastrophic-inspection/`.

New regressions:

- `test_coverage_excludes_words_outside_visible_page`: CropBox and MediaBox,
  with rotations 0 and 90 degrees.
- `test_coverage_exempts_only_artifact_words_inside_tagged_figure`: direct
  BBox, Layout BBox, marked-image bounds, and a clipped transformed Form.
  Non-Artifact text stays eligible, undescribed Figures still fail their
  separate check, and hidden body text outside the Figure still blocks.
- `test_artifact_figure_exemption_requires_sixty_percent_word_overlap`:
  59% overlap stays eligible; 61% is excluded.

Against the previous implementation, these parameterized cases produced
**13 failures and 1 pass**. The passing case is the negative 59% control.
All 14 cases pass with the changes.

`test_artifact_body_is_advisory_unless_catastrophic` and
`test_page_twelve_without_tags_is_held_even_when_overall_coverage_exceeds_threshold`
continue to verify catastrophic hidden-body and whole-page blocking. The
50%/97%, 50-missing-word, and 100-eligible-page boundaries, page-naming
advisories, no-header size boundaries, reader fixes, and visual-gap
fail-closed regressions remain in the suite.

Validation from `backend/`:

- `.venv/bin/python -m ruff check .`: **passed**.
- Full requested pytest command with check-only LLM settings and blank
  Docling settings: **878 passed in 129.49 seconds**.
- Both checks completed without errors on all **1,956 PDFs**.
- `backend/uv.lock` matches its initial checksum; `git diff --check` passed.
- No repository-wide formatting, commits, pushes, network calls, or model calls.
