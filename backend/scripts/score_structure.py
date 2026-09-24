"""Score remediated structure against gold with opendataloader-bench's metrics.

Each document's structure tree is compared as Markdown (see
``app/services/structure_text.py``): NID for text in reading order, TEDS for
tables, MHS for the section tree, and MHS-L, which is MHS with each heading's
level as part of its type, so a heading at the wrong level costs a rename.

The evaluators come from a checkout of opendataloader-bench (Apache 2.0) at
commit 7af1d8f, passed with ``--odl-bench``:

    uv run --with apted --with beautifulsoup4 --with lxml \\
        python scripts/score_structure.py --odl-bench ../opendataloader-bench \\
        data/eval/olmocr-bench/gold_rt data/eval/olmocr-bench/bench_data/pipe_v3/gold_rt

The gold directory holds ``<document>.md`` (see ``prepare_gold_roundtrip.py``);
each candidate directory holds ``<document>.tagged.pdf``.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from pathlib import Path

from app.services.structure_text import screen_reader_text

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
METRICS = ("NID", "TEDS", "TEDS-S", "MHS", "MHS-S", "MHS-L")


def load_evaluators(odl_bench: Path):
    sys.path.insert(0, str(odl_bench / "src"))
    import converter_markdown_table
    import evaluator_heading_level
    import evaluator_reading_order
    import evaluator_table

    return (
        converter_markdown_table,
        evaluator_heading_level,
        evaluator_reading_order,
        evaluator_table,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--odl-bench", type=Path, required=True)
    parser.add_argument("gold", type=Path)
    parser.add_argument("candidates", type=Path, nargs="+")
    options = parser.parse_args()
    markdown_tables, headings, reading_order, tables = load_evaluators(options.odl_bench)

    def leveled_tree(markdown: str):
        """MHS's section tree with the heading level in each heading's tag."""
        root = headings.HeadingTree("document")
        container, pending = root, []
        for line in markdown.splitlines():
            if match := HEADING.match(line):
                headings._flush_content(pending, container)
                container = headings.HeadingTree(
                    f"h{len(match.group(1))}", headings._normalize_text(match.group(2))
                )
                root.children.append(container)
            elif line.strip():
                pending.append(headings._normalize_text(line))
        headings._flush_content(pending, container)
        return root

    def mhs_levels(gold: str, candidate: str) -> float | None:
        gold_tree = leveled_tree(markdown_tables.convert_to_markdown_with_html_tables(gold))
        if not any(child.tag.startswith("h") for child in gold_tree.children):
            return None
        tree = leveled_tree(markdown_tables.convert_to_markdown_with_html_tables(candidate))
        if not any(child.tag.startswith("h") for child in tree.children):
            return 0.0
        nodes = max(headings._count_nodes(gold_tree), headings._count_nodes(tree), 1)
        distance = headings._compute_edit_distance(gold_tree, tree, include_text=True)
        return max(0.0, 1.0 - distance / nodes)

    for candidate_dir in options.candidates:
        scores: dict[str, list[float]] = {metric: [] for metric in METRICS}
        lines = []
        for gold_file in sorted(options.gold.glob("*.md")):
            gold = gold_file.read_text()
            candidate_file = candidate_dir / f"{gold_file.stem}.tagged.pdf"
            candidate = (
                screen_reader_text(candidate_file, markdown=True) if candidate_file.exists() else ""
            )
            values = dict(
                zip(
                    METRICS,
                    (
                        reading_order.evaluate_reading_order(gold, candidate)[0],
                        *tables.evaluate_table(gold, candidate),
                        *headings.evaluate_heading_level(gold, candidate),
                        mhs_levels(gold, candidate),
                    ),
                    strict=True,
                )
            )
            for metric, value in values.items():
                if value is not None:
                    scores[metric].append(value)
            cells = " ".join(
                f"{metric}={'-' if value is None else f'{value:.2f}'}"
                for metric, value in values.items()
            )
            missing = "" if candidate_file.exists() else " MISSING"
            lines.append(f"  {gold_file.stem:<24}{cells}{missing}")
        print(f"\n{candidate_dir}")
        print(
            "  "
            + "  ".join(
                f"{metric} {statistics.mean(values):.3f} (n={len(values)})"
                for metric, values in scores.items()
                if values
            )
        )
        print("\n".join(lines))


if __name__ == "__main__":
    main()
