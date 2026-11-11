"""Fill the table model's answer cache for captured benchmark pages.

For every table Docling found on each captured page
(``<bench_dir>/<source>/<subset>/<stem>.tag-inputs``), the table-model step's
crop (``app/pipeline/table_model.py``) is sent to the model server unless its
answer is already in the cache. Requests go one at a time. Then
``retag_bench.py --table-model-cache`` measures the step without the model.

    uv run python scripts/table_model_bench.py --bench-dir data/eval/olmocr-bench/bench_data \\
        --source pipe_8a81fdc --cache data/eval/cache/table-model \\
        --url http://127.0.0.1:8141/v1 tables tables_scan
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from app.pipeline.table_model import AnswerCache, HttpReader, prepare_tables


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench-dir", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--url", required=True, help="the model server's OpenAI-compatible base")
    parser.add_argument("--model", default="paddleocr-vl-1.5")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("subsets", nargs="+")
    options = parser.parse_args()
    cache = AnswerCache(options.cache, options.model)
    reader = HttpReader(options.url, options.model, timeout=options.timeout, cache=cache)
    for subset in options.subsets:
        started = time.monotonic()
        tables = hits = failed = 0
        for inputs in sorted((options.bench_dir / options.source / subset).glob("*.tag-inputs")):
            structure = json.loads((inputs / "structure.json").read_text())
            for prep in prepare_tables(inputs / "input.pdf", structure.get("elements") or []):
                tables += 1
                if cache.get(prep.png) is not None:
                    hits += 1
                    continue
                if await reader.read(prep.png) is None:
                    failed += 1
        print(
            f"{subset}: {tables} tables, {hits} cached before, {failed} failed, "
            f"{time.monotonic() - started:.0f} s",
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
