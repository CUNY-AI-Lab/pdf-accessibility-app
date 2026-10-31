"""Tag benchmark pages again from the tagger inputs a pipeline run kept.

``pipeline_bench.py`` keeps what the tagger was given for each page in
``<bench_dir>/<source>/<subset>/<stem>.tag-inputs/``. This tags each page
again with this checkout's tagger and saves the result as
``<bench_dir>/<candidate>/<subset>/<stem>.tagged.pdf``, or ``<stem>.failed``
holding the error. OCR and Docling are not rerun, so a change to the tagger
is measured on every page in about a minute. Font repairs the pipeline makes
to a tagged PDF afterwards are not replayed, so pages marked
``rewritten-after-tagging`` can score differently from the pipeline.

With ``--table-model-cache``, the table-model step (``app/pipeline/table_model.py``)
rewrites each page's tables first, answering every crop from that cache only
(``table_model_bench.py fill`` fills it); a crop not in the cache keeps
Docling's table. Each page's table report is kept as ``<stem>.table-model.json``.

    uv run python scripts/retag_bench.py --bench-dir data/eval/olmocr-bench/bench_data \\
        --source pipe_v3 --candidate retag_v3 tables multi_column
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path


def retag(inputs: Path, tagged: Path, table_model: dict | None = None) -> str:
    from app.pipeline.tagger import tag_pdf

    try:
        structure = json.loads((inputs / "structure.json").read_text())
        if table_model:
            from app.pipeline.table_model import AnswerCache, CachedReader, rewrite_tables

            reader = CachedReader(AnswerCache(Path(table_model["cache"]), table_model["model"]))
            structure, audit = asyncio.run(
                rewrite_tables(
                    inputs / "input.pdf",
                    structure,
                    reader,
                    min_coverage=table_model["min_coverage"],
                    min_coverage_scan=table_model["min_coverage_scan"],
                    correct_scan_text=table_model["scan_text"],
                )
            )
            audit["cache_misses"] = reader.misses
            report = tagged.with_name(tagged.name.replace(".tagged.pdf", ".table-model.json"))
            report.write_text(json.dumps(audit, indent=1))
        asyncio.run(
            tag_pdf(
                inputs / "input.pdf",
                tagged,
                structure,
                **json.loads((inputs / "arguments.json").read_text()),
            )
        )
        return "ok"
    except Exception as exc:  # noqa: BLE001 - a page's failure is recorded, not raised
        tagged.with_name(tagged.name.replace(".tagged.pdf", ".failed")).write_text(repr(exc))
        return f"{inputs.name}: {exc!r}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench-dir", type=Path, required=True)
    parser.add_argument("--source", required=True, help="the candidate whose inputs to use")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--table-model-cache", type=Path, help="rewrite tables from these answers")
    parser.add_argument("--table-model-name", default="paddleocr-vl-1.5")
    parser.add_argument("--table-model-min-coverage", type=float, default=0.7)
    parser.add_argument("--table-model-min-coverage-scan", type=float, default=0.9)
    parser.add_argument(
        "--table-model-scan-text", action="store_true", help="correct OCR confusions in cells"
    )
    parser.add_argument("subsets", nargs="+")
    options = parser.parse_args()
    table_model = None
    if options.table_model_cache:
        table_model = {
            "cache": str(options.table_model_cache),
            "model": options.table_model_name,
            "min_coverage": options.table_model_min_coverage,
            "min_coverage_scan": options.table_model_min_coverage_scan,
            "scan_text": options.table_model_scan_text,
        }
    jobs: list[tuple[Path, Path]] = []
    for subset in options.subsets:
        out_dir = options.bench_dir / options.candidate / subset
        out_dir.mkdir(parents=True, exist_ok=True)
        for inputs in sorted((options.bench_dir / options.source / subset).glob("*.tag-inputs")):
            stem = inputs.name.removesuffix(".tag-inputs")
            jobs.append((inputs, out_dir / f"{stem}.tagged.pdf"))
    with ProcessPoolExecutor(max_workers=max((os.cpu_count() or 2) // 2, 1)) as pool:
        results = pool.map(
            partial(retag, table_model=table_model),
            [inputs for inputs, _ in jobs],
            [tagged for _, tagged in jobs],
        )
        failures = [result for result in results if result != "ok"]
    rewritten = sum((inputs / "rewritten-after-tagging").exists() for inputs, _ in jobs)
    print(
        f"{options.candidate}: {len(jobs) - len(failures)} tagged, {len(failures)} failed; "
        f"{rewritten} rewritten after tagging in the pipeline, not replayed"
    )
    for failure in failures[:10]:
        print(f"  {failure}")


if __name__ == "__main__":
    main()
