#!/usr/bin/env python3
"""Compare a remediated PDF against a gold accessible PDF."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from app.services.roundtrip_compare import (
    compare_roundtrip_pdfs,
    load_roundtrip_manifest,
    render_roundtrip_markdown,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT_DIR / "data" / "benchmarks"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, help="Known-good accessible PDF.")
    parser.add_argument(
        "--candidate",
        required=True,
        help="Remediated PDF to compare against the gold file.",
    )
    parser.add_argument(
        "--manifest",
        help="Optional round-trip JSON manifest with recoverable assertions.",
    )
    parser.add_argument(
        "--work-dir",
        help="Optional directory for temporary structure extraction artifacts.",
    )
    parser.add_argument(
        "--json-out",
        help="Optional output path for the JSON report.",
    )
    parser.add_argument(
        "--markdown-out",
        help="Optional output path for the Markdown report.",
    )
    args = parser.parse_args()

    gold_pdf = Path(args.gold).expanduser().resolve()
    candidate_pdf = Path(args.candidate).expanduser().resolve()
    if not gold_pdf.exists():
        raise SystemExit(f"Gold PDF not found: {gold_pdf}")
    if not candidate_pdf.exists():
        raise SystemExit(f"Candidate PDF not found: {candidate_pdf}")

    manifest = None
    if args.manifest:
        manifest = load_roundtrip_manifest(Path(args.manifest).expanduser().resolve())

    work_dir = Path(args.work_dir).expanduser().resolve() if args.work_dir else None
    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest=manifest,
        work_dir=work_dir,
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_base = OUTPUT_ROOT / f"roundtrip_compare_{ts}"
    json_out = Path(args.json_out).expanduser().resolve() if args.json_out else default_base.with_suffix(".json")
    markdown_out = (
        Path(args.markdown_out).expanduser().resolve()
        if args.markdown_out
        else default_base.with_suffix(".md")
    )

    json_out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    markdown_out.write_text(render_roundtrip_markdown(report))

    recoverable = report.get("assertions", {}).get("recoverable", {})
    structure = report.get("comparisons", {}).get("structure", {})
    reading_order = structure.get("reading_order", {})

    print(f"Wrote JSON report to: {json_out}")
    print(f"Wrote Markdown report to: {markdown_out}")
    print(
        "Summary: "
        f"recoverable={recoverable.get('passed', 0)}/{recoverable.get('total', 0)} "
        f"raw_text_similarity={report.get('metadata', {}).get('raw_text_similarity')} "
        f"reading_order_hit_rate={reading_order.get('hit_rate')} "
        f"field_match_rate={report.get('comparisons', {}).get('fields', {}).get('named_field_match_rate')}"
    )


if __name__ == "__main__":
    asyncio.run(main())
