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

Each test's result is kept, one JSON line per test, in
``<bench>/results/<view>/<candidate>.jsonl``. ``--against <base>`` then lists
the tests each candidate fixed and broke relative to ``<base>`` on the same
tests, with the paired counts.

    uv run --with "olmocr[bench]==0.4.27" python scripts/score_olmocr.py \\
        --bench data/eval/olmocr-bench old_print pipe_prod pipe_v3 --against pipe_prod
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from app.services.structure_text import screen_reader_text

SUMMARY = re.compile(r"Average Score: ([\d.]+)% ± ([\d.]+)%")
PASS_RATE = re.compile(r"(\w+)\s*: ([\d.]+)% average pass rate")
PASSED = re.compile(r"\.jsonl\s*: [\d.]+% \((\d+)/(\d+) tests\)")
MISSING = "Missing MD files"


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


def page_name(pdf: str, page: int = 1) -> str:
    return f"{Path(pdf).stem}_pg{page}_repeat1.md"


def evaluate(tests: list, read_page: Callable[[str, int], str | None]) -> list[dict]:
    """Each test's result on the candidate's text for its page, as olmOCR-Bench
    judges one repeat: a missing page or a test that raises fails."""
    results = []
    for test in tests:
        text = read_page(test.pdf, test.page)
        if text is None:
            passed, explanation = False, MISSING
        else:
            try:
                passed, explanation = test.run(text)
            except Exception as exc:  # noqa: BLE001 - olmOCR-Bench counts it as a failure
                passed, explanation = False, str(exc)
        results.append(
            {
                "id": test.id,
                "pdf": test.pdf,
                "page": test.page,
                "type": test.type,
                "passed": bool(passed),
                "explanation": explanation,
            }
        )
    return results


def compare(base: list[dict], candidate: list[dict]) -> dict:
    """The tests a candidate fixed and broke relative to a base, and the
    paired counts over the tests both were scored on."""
    before = {result["id"]: result for result in base}
    after = {result["id"]: result for result in candidate}
    paired = sorted(before.keys() & after.keys())
    fixed = [after[i] for i in paired if after[i]["passed"] and not before[i]["passed"]]
    broken = [after[i] for i in paired if before[i]["passed"] and not after[i]["passed"]]
    return {
        "fixed": fixed,
        "broken": broken,
        "both_passed": sum(before[i]["passed"] and after[i]["passed"] for i in paired),
        "both_failed": sum(not before[i]["passed"] and not after[i]["passed"] for i in paired),
        "unpaired": len(before.keys() ^ after.keys()),
    }


def read_results(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_results(view: Path, subset: str, candidate: str) -> list[dict]:
    """Every non-baseline test's result for a candidate in a scorer view."""
    from olmocr.bench.tests import load_tests

    tests = [test for test in load_tests(str(view / f"{subset}.jsonl")) if test.type != "baseline"]

    def read_page(pdf: str, page: int) -> str | None:
        path = view / candidate / subset / page_name(pdf, page)
        return path.read_text(encoding="utf-8") if path.exists() else None

    return evaluate(tests, read_page)


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
    parser.add_argument("--against", help="list tests fixed and broken relative to this candidate")
    parser.add_argument("subset")
    parser.add_argument("candidates", nargs="+")
    options = parser.parse_args()

    view_name = f"{options.subset}-{options.restrict}" if options.restrict else options.subset
    results_dir = options.bench / "results" / view_name
    candidates = list(options.candidates)
    if options.against and options.against not in candidates:
        if not (results_dir / f"{options.against}.jsonl").exists():
            candidates.append(options.against)

    view = build_view(
        options.bench,
        options.subset,
        options.restrict,
        candidates,
        markdown=options.markdown,
        figure_text=options.figure_text,
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
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

        results = test_results(view, options.subset, candidate)
        (results_dir / f"{candidate}.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in results)
        )
        passed = sum(entry["passed"] for entry in results)
        if (counted := PASSED.search(summary)) and int(counted.group(1)) != passed:
            print(f"  ! per-test results pass {passed}, the scorer {counted.group(1)}")

    if options.against:
        base = read_results(results_dir / f"{options.against}.jsonl")
        for candidate in options.candidates:
            if candidate == options.against:
                continue
            change = compare(base, read_results(results_dir / f"{candidate}.jsonl"))
            print(
                f"\n  {candidate} against {options.against}: {len(change['fixed'])} fixed, "
                f"{len(change['broken'])} broken, {change['both_passed']} pass in both, "
                f"{change['both_failed']} fail in both"
                + (f", {change['unpaired']} not in both" if change["unpaired"] else "")
            )
            for label in ("fixed", "broken"):
                for entry in change[label]:
                    print(f"    {label:<7}{entry['type']:<8}{entry['pdf']}  {entry['id']}")


if __name__ == "__main__":
    main()
