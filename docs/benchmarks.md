# Benchmarks And Cost

Updated: 2026-03-09

This page tracks the benchmark sets that are most useful for the current product decisions.

## Release gate reminder

A document is counted as release-ready only if it is:

1. compliant
2. fidelity-passed
3. free of blocking review tasks

## Exact curated corpus

Artifact: [backend/data/benchmarks/corpus_20260308_202258/corpus_report.md](../backend/data/benchmarks/corpus_20260308_202258/corpus_report.md)

- `25 / 25` successful outputs release-ready
- `2` failed inputs, both damaged PDFs

This is the main regression corpus for the current pipeline.

## Representative CUNY-like corpus

Baseline before semantic batching/caching:
- [backend/data/benchmarks/corpus_20260309_131555/corpus_report.md](../backend/data/benchmarks/corpus_20260309_131555/corpus_report.md)
- [backend/data/benchmarks/corpus_20260309_131555/corpus_summary.json](../backend/data/benchmarks/corpus_20260309_131555/corpus_summary.json)

Current measured run:
- [backend/data/benchmarks/corpus_20260309_134955/corpus_report.md](../backend/data/benchmarks/corpus_20260309_134955/corpus_report.md)
- [backend/data/benchmarks/corpus_20260309_134955/corpus_summary.json](../backend/data/benchmarks/corpus_20260309_134955/corpus_summary.json)

Corpus mix:
- articles and readings
- guides and admin documents
- syllabi/course materials
- scanned office documents

Current result:
- `10 / 10` complete
- `10 / 10` compliant
- `10 / 10` fidelity-passed

### Cost summary

Measured from OpenRouter `usage.cost`, not a hand-built pricing estimate.

| Metric | Value |
|---|---:|
| Total cost | `$2.751915` |
| Average cost / PDF | `$0.275192` |
| Median cost / PDF | `$0.134327` |
| Average cost / page | `$0.025163` |
| Average runtime / PDF | `81.28s` |
| Median runtime / PDF | `49.99s` |

### Cost range examples

| PDF | Cost |
|---|---:|
| `AI-Assisted Programming - Spring 2026 Syllabus.pdf` | `$0.000000` |
| `CUNY_Libraries_AI_Discovery_Guide (1).pdf` | `$0.055204` |
| `Code4Lib BePress Article.pdf` | `$0.095892` |
| `Bucinca-2021-cognitive-forcing-functions.pdf` | `$0.223823` |
| `1-s2.0-S0360131524002380-main.pdf` | `$0.604448` |
| `filer-user-guide-january-2025.pdf` | `$1.234529` |

Interpretation:
- many ordinary CUNY documents are under `$0.10`
- harder academic/admin documents often land in the `$0.20 - $0.60` range
- the current outliers are figure-heavy or semantics-heavy guides

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
- OpenRouter structured outputs instead of looser JSON prompting
- provider retry/backoff instead of rerunning whole workflows after transient failures

The next likely cost target for the real CUNY audience is figure-heavy guide/admin documents.
