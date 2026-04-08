# Benchmarks And Cost

Updated: 2026-03-12

This page tracks the benchmark sets that are most useful for the current product decisions.

## Release gate reminder

A document is counted as release-ready only if it is:

1. compliant
2. fidelity-passed
3. finalized as `complete`, not `manual_remediation`

Optional visible review items are advisory only.

## Exact curated corpus

Artifact: [backend/data/benchmarks/corpus_20260308_202258/corpus_report.md](../backend/data/benchmarks/corpus_20260308_202258/corpus_report.md)

- `25 / 25` successful outputs release-ready
- `2` failed inputs, both damaged PDFs

This is the main regression corpus for the current pipeline.

## Representative non-huge corpus

Current measured run:
- [backend/data/benchmarks/corpus_20260311_121723/corpus_report.md](../backend/data/benchmarks/corpus_20260311_121723/corpus_report.md)
- [backend/data/benchmarks/corpus_20260311_121723/corpus_summary.json](../backend/data/benchmarks/corpus_20260311_121723/corpus_summary.json)

Corpus mix:
- articles and readings
- guides and admin documents
- syllabi/course materials
- scanned office documents

Current result:
- `7 / 7` complete
- `7 / 7` compliant
- `7 / 7` fidelity-passed
- `7 / 7` release-ready
- `0` manual remediation

### Cost summary

Measured from recorded provider usage/cost fields, not a hand-built pricing estimate.

| Metric | Value |
|---|---:|
| Total cost | `$0.179212` |
| Average cost / PDF | `$0.025602` |
| Median cost / PDF | `$0.013667` |
| Average cost / page | `$0.003144` |
| Average runtime / PDF | `76.45s` |
| Median runtime / PDF | `54.91s` |

Interpretation:
- this is the current release-sanity corpus after the execution-first review cleanup
- ordinary non-huge CUNY documents are now often only a few cents each on this representative set
- the remaining cost outliers are still figure-heavy or semantics-heavy guides

## Official form set (stress suite)

Acceptance run:
- [backend/data/benchmarks/corpus_20260309_123540/corpus_report.md](../backend/data/benchmarks/corpus_20260309_123540/corpus_report.md)

Result:
- `7 / 7` complete
- `7 / 7` compliant
- `7 / 7` fidelity-passed

This is a stress suite, not the normal CUNY audience baseline.

### Batching/caching win case

`irs_ss4.pdf` before semantic batching:
- source: [backend/data/benchmarks/corpus_20260309_131058/corpus_summary.json](../backend/data/benchmarks/corpus_20260309_131058/corpus_summary.json)
- `89` LLM requests
- `$0.331174`
- `125.70s`

`irs_ss4.pdf` after page-scoped batching:
- source: [backend/data/benchmarks/corpus_20260309_134239/corpus_summary.json](../backend/data/benchmarks/corpus_20260309_134239/corpus_summary.json)
- `5` LLM requests
- `$0.047978`
- `56.84s`

Delta:
- requests: `-94.4%`
- cost: `-85.5%`
- runtime: `-54.8%`

## What cost optimization currently means

The biggest clean wins so far come from:
- semantic-unit prompt caching
- page-scoped form batching with per-field fallback
- Gemini structured outputs instead of looser JSON prompting
- provider retry/backoff instead of rerunning whole workflows after transient failures

The next likely cost target for the real CUNY audience is figure-heavy guide/admin documents.

## Stronger verification direction

There is now an explicit round-trip benchmark design for stronger verification than compliance plus fidelity alone:

- start from a gold accessible PDF
- strip benchmark-target accessibility semantics
- remediate the stripped file
- compare the output back to the gold file

See:

- [Gold-To-Stripped Round-Trip Benchmark](./roundtrip_benchmark.md)

Current stripping utility:

```bash
cd backend
PYTHONPATH=. uv run python scripts/strip_accessibility.py \
  --input /path/to/gold-accessible.pdf \
  --output data/benchmarks/roundtrip/mydoc_stripped.pdf
```

Comparison utility:

```bash
cd backend
PYTHONPATH=. uv run python scripts/roundtrip_compare.py \
  --gold /path/to/gold-accessible.pdf \
  --candidate /path/to/remediated-output.pdf \
  --manifest /path/to/mydoc.roundtrip.json
```

Corpus runner:

```bash
cd backend
PYTHONPATH=. uv run python scripts/roundtrip_corpus_benchmark.py
```

The round-trip runner defaults to the `assistive-core` workflow profile. That profile keeps the full downstream validation/fidelity/review loop and skips only the figure alt-text branch. Use `--workflow-profile full` when you want figure/alt-text behavior included as well.

The round-trip comparison now reports form field presence and field-type recovery separately from exact accessible-name replay, so assistive-core form checks can be written against name/role/value semantics rather than a single gold `/TU` string.
