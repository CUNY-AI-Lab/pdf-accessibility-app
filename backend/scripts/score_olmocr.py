"""Score olmOCR-Bench candidates on one subset.

Builds a scorer view, ``<bench>/views/<subset>[-<restrict>]/``, holding the
subset's tests, its PDFs, and one Markdown file per page for each candidate,
then runs the olmOCR-Bench scorer on each candidate. A candidate's page is
what a screen reader hears from its ``<stem>.tagged.pdf`` (see
``app/services/structure_text.py``; ``--markdown`` keeps tables as HTML for
the table tests), empty for a ``<stem>.failed`` page, or else its
``<stem>_pg1_repeat1.md`` (an OCR-stage candidate's raw text layer). With
``--restrict``, only the pages that candidate has output for are scored,
such as the pages Adobe finished.

    uv run --with "olmocr[bench]==0.4.27" python scripts/score_olmocr.py \\
        --bench data/eval/olmocr-bench old_print pipe_prod pipe_v3
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from app.services.structure_text import screen_reader_text

SUMMARY = re.compile(r"Average Score: ([\d.]+)% ± ([\d.]+)%")
PASS_RATE = re.compile(r"(\w+)\s*: ([\d.]+)% average pass rate")


def candidate_pages(bench: Path, candidate: str, subset: str) -> dict[str, Path]:
    """Each page's output file, by PDF stem: a tagged PDF, a failure marker,
    or Markdown, in that order of preference."""
    pages: dict[str, Path] = {}
    directory = bench / "bench_data" / candidate / subset
    for pattern, suffix in (
        ("*_pg1_repeat1.md", "_pg1_repeat1.md"),
        ("*.failed", ".failed"),
        ("*.tagged.pdf", ".tagged.pdf"),
    ):
        for file in directory.glob(pattern):
            pages[file.name.removesuffix(suffix)] = file
    return pages


def page_text(output: Path, *, markdown: bool, figure_text: bool) -> str:
    if output.name.endswith(".tagged.pdf"):
        return screen_reader_text(output, markdown=markdown, figure_text=figure_text)
    if output.name.endswith(".failed"):
        return ""
    return output.read_text(encoding="utf-8")


def page_name(pdf: str) -> str:
    return f"{Path(pdf).stem}_pg1_repeat1.md"


def build_view(
    bench: Path,
    subset: str,
    restrict: str | None,
    candidates: list[str],
    *,
    markdown: bool,
    figure_text: bool,
) -> Path:
    tests = [json.loads(line) for line in (bench / f"{subset}.jsonl").read_text().splitlines()]
    pdfs = sorted({test["pdf"] for test in tests})
    if restrict:
        covered = candidate_pages(bench, restrict, subset)
        pdfs = [pdf for pdf in pdfs if Path(pdf).stem in covered]
        tests = [test for test in tests if test["pdf"] in pdfs]

    view = bench / "views" / (f"{subset}-{restrict}" if restrict else subset)
    shutil.rmtree(view, ignore_errors=True)
    (view / "pdfs" / subset).mkdir(parents=True)
    (view / f"{subset}.jsonl").write_text("".join(json.dumps(test) + "\n" for test in tests))
    for pdf in pdfs:
        (view / "pdfs" / pdf).symlink_to((bench / "bench_data" / "pdfs" / pdf).resolve())
    for candidate in candidates:
        pages = candidate_pages(bench, candidate, subset)
        (view / candidate / subset).mkdir(parents=True)
        for pdf in pdfs:
            if (output := pages.get(Path(pdf).stem)) is not None:
                text = page_text(output, markdown=markdown, figure_text=figure_text)
                (view / candidate / subset / page_name(pdf)).write_text(text, encoding="utf-8")
    print(f"{view.name}: {len(pdfs)} pages, {len(tests)} tests")
    return view


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--restrict", help="score only the pages this candidate covers")
    parser.add_argument("--markdown", action="store_true", help="read tables as HTML")
    parser.add_argument(
        "--figure-text", action="store_true", help="read text inside Figures without alt text"
    )
    parser.add_argument("subset")
    parser.add_argument("candidates", nargs="+")
    options = parser.parse_args()

    view = build_view(
        options.bench,
        options.subset,
        options.restrict,
        options.candidates,
        markdown=options.markdown,
        figure_text=options.figure_text,
    )
    for candidate in options.candidates:
        pages = len(list((view / candidate / options.subset).iterdir()))
        output = subprocess.run(
            [
                sys.executable,
                "-m",
                "olmocr.bench.benchmark",
                "--dir",
                str(view),
                "--skip_baseline",
                "--candidate",
                candidate,
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        summary = output[output.rfind(f"{candidate:<20} : Average Score") :]
        score = SUMMARY.search(summary)
        rates = "  ".join(f"{kind} {rate}" for kind, rate in sorted(PASS_RATE.findall(summary)))
        result = f"{score.group(1)}% ± {score.group(2)}" if score else "no score (missing pages?)"
        print(f"  {candidate:<22} {pages:>3} pages  {result}  {rates}")


if __name__ == "__main__":
    main()
