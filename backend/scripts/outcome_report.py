"""What the full pipeline decided for each document of a bench run.

Reads ``<candidate_dir>/<stem>.run.json`` (written by ``pipeline_bench.py``)
and the tagged PDF beside it, and prints one row per document: the final
status, the release gate checks that held it, the review tasks a person
sees (and how many blocking tasks the review screen does not show),
veraPDF's verdict and remaining rules, figures and how many carry usable alt
text, the title the output announces, pages, seconds, model requests, and
the cost estimated from tokens at the model's catalog price (the Gateway
does not report a cost per call). ``--json`` writes the rows instead.

    uv run python scripts/outcome_report.py \\
        data/eval/olmocr-bench/bench_data/<candidate>/<subset> --price-in 0.53 --price-out 2.66
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

import pikepdf
from score_semantics import document_title, figures_by_page

from app.pipeline.tagger_semantics import _is_weak_figure_alt_text

HOLDING = {"fail", "error"}


def document_row(run_path: Path, price_in: float, price_out: float) -> dict:
    run = json.loads(run_path.read_text())
    outcome = run.get("outcome") or {}
    stem = run_path.name.removesuffix(".run.json")
    tagged = run_path.with_name(f"{stem}.tagged.pdf")
    title, figures, usable = None, 0, 0
    pages = None
    if tagged.exists():
        with pikepdf.open(tagged) as pdf:
            title = document_title(pdf) or None
            pages = len(pdf.pages)
            alts = [alt for page in figures_by_page(pdf).values() for alt in page]
        figures = len(alts)
        usable = sum(1 for alt in alts if not _is_weak_figure_alt_text(alt))
    status, _, classification = str(run.get("status") or "").partition(" ")
    tasks = outcome.get("review_tasks") or []
    summary = outcome.get("fidelity_summary") or {}
    visible_blocking = sum(1 for task in tasks if task.get("blocking"))
    usage = run.get("llm_usage") or {}
    return {
        "document": stem,
        "status": status,
        "classification": classification,
        "pages": pages,
        "seconds": run.get("seconds"),
        "held_by": [
            check["check"] for check in outcome.get("checks") or [] if check["status"] in HOLDING
        ],
        "review_tasks": sorted(
            f"{task['task_type']}{'*' if task.get('blocking') else ''}" for task in tasks
        ),
        "hidden_blocking": max(0, int(summary.get("blocking_tasks") or 0) - visible_blocking),
        "verapdf_compliant": outcome.get("validation_compliant"),
        "remaining_rules": outcome.get("remaining_rules") or [],
        "alt_entries": len(outcome.get("alt_texts") or []),
        "figures_tagged": figures,
        "figures_with_alt": usable,
        "title": title,
        "requests": usage.get("request_count", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "cost_usd": round(
            (
                usage.get("prompt_tokens", 0) * price_in
                + usage.get("completion_tokens", 0) * price_out
            )
            / 1e6,
            4,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("candidate_dir", type=Path)
    parser.add_argument("--price-in", type=float, default=0.0, help="USD per million prompt tokens")
    parser.add_argument(
        "--price-out", type=float, default=0.0, help="USD per million completion tokens"
    )
    parser.add_argument("--json", action="store_true")
    options = parser.parse_args()
    rows = [
        document_row(path, options.price_in, options.price_out)
        for path in sorted(options.candidate_dir.glob("*.run.json"))
    ]
    if options.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return
    for row in rows:
        print(
            f"{row['document']:<28} {row['status']:<19} {row['classification']:<8}"
            f" p{row['pages'] or '-':<4} {row['seconds']:>7}s"
            f"  veraPDF {'ok' if row['verapdf_compliant'] else 'NO'}"
            f"  alt {row['figures_with_alt']}/{row['figures_tagged']}"
            f"  ${row['cost_usd']:.4f}"
        )
        if row["held_by"]:
            print(f"    held by {', '.join(row['held_by'])}")
        if row["review_tasks"] or row["hidden_blocking"]:
            print(
                f"    tasks {', '.join(row['review_tasks']) or '-'}"
                f" (hidden blocking {row['hidden_blocking']})"
            )
        if row["remaining_rules"]:
            print(f"    rules {', '.join(row['remaining_rules'])}")
        print(f"    title {row['title']!r}")
    statuses = Counter(row["status"] for row in rows)
    held = Counter(check for row in rows for check in row["held_by"])
    print(
        f"\n{len(rows)} documents: "
        + ", ".join(f"{status} {count}" for status, count in statuses.most_common())
    )
    print("held by: " + ", ".join(f"{check} {count}" for check, count in held.most_common()))
    print(
        f"veraPDF compliant {sum(1 for row in rows if row['verapdf_compliant'])}/{len(rows)}"
        f", figures with alt {sum(row['figures_with_alt'] for row in rows)}"
        f"/{sum(row['figures_tagged'] for row in rows)}"
        f", hidden blocking tasks on {sum(1 for row in rows if row['hidden_blocking'])} documents"
    )
    seconds = [row["seconds"] for row in rows if row["seconds"] is not None]
    print(
        f"pages {sum(row['pages'] or 0 for row in rows)}"
        f", median seconds {statistics.median(seconds) if seconds else '-'}"
        f", requests {sum(row['requests'] for row in rows)}"
        f", tokens {sum(row['prompt_tokens'] for row in rows)} in"
        f" / {sum(row['completion_tokens'] for row in rows)} out"
        f", estimated ${sum(row['cost_usd'] for row in rows):.4f}"
    )


if __name__ == "__main__":
    main()
