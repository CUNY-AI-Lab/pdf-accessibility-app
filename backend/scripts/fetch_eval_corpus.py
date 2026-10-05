"""Build the evaluation corpus from ``eval/corpus.json``.

Writes the layout the bench and scoring scripts read, under ``--bench``
(default ``data/eval/olmocr-bench``): each subset's tests as
``<subset>.jsonl`` and its PDFs in ``bench_data/pdfs/<subset>/``.

- olmOCR-Bench subsets come from the dataset at the pinned revision. A subset
  with ``sample`` keeps that many PDFs, drawn as they were in September 2026:
  ``random.Random(seed).sample`` over the sorted PDFs that have a non-baseline
  test, with all of a chosen PDF's tests.
- ``old_print`` pages are Internet Archive page images, each embedded
  unchanged in a one-page PDF at the scan's resolution.
- Round-trip sets (``gold_rt``, ``cuny_rt``) are prepared by
  ``prepare_gold_roundtrip.py`` from their gold PDFs; a set with a
  ``scan_subset`` also gets an image-only variant (``cuny_rt_scan``) scored
  against the same gold.

Every file not read from a pinned dataset revision comes from the Lab's copy,
the private repository CUNY-AI-Lab/pdf-accessibility-eval-data, and is
checked against its sha256 here; the manifest's origin URLs record where each
came from. Adobe's results, kept there because they cost free-tier quota to
remake, are restored as the ``adobe_ocr_autotag`` candidate. Downloads and
the Lab's copy are kept in ``--sources`` (default ``data/eval/sources``), so a
rebuild fetches nothing new. The structure scorer's opendataloader-bench is
checked out there at its pinned commit too.

    uv run python scripts/fetch_eval_corpus.py [--subsets old_print gold_rt]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
from synthetic_scan import image_pdf, synthetic_scan

BACKEND = Path(__file__).resolve().parent.parent
MANIFEST = BACKEND / "eval" / "corpus.json"
USER_AGENT = "CUNY AI Lab PDF accessibility evaluation (ailab@gc.cuny.edu)"
LAB_COPY = "https://github.com/CUNY-AI-Lab/pdf-accessibility-eval-data"
ADOBE_CANDIDATE = "adobe_ocr_autotag"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(client: httpx.Client, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with partial.open("wb") as file:
            for chunk in response.iter_bytes():
                file.write(chunk)
    partial.rename(dest)


def cached(client: httpx.Client, url: str, dest: Path) -> Path:
    """``dest``, downloaded from ``url`` unless already there."""
    if not dest.exists():
        download(client, url, dest)
    return dest


def lab_copy(sources: Path) -> Path:
    """A current checkout of the Lab's copy."""
    checkout = sources / "lab-copy"
    if checkout.exists():
        subprocess.run(["git", "-C", str(checkout), "pull", "--quiet", "--ff-only"], check=True)
    else:
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", LAB_COPY, str(checkout)], check=True
        )
    return checkout


def checked(path: Path, expected: str) -> Path:
    if (actual := sha256(path)) != expected:
        raise SystemExit(f"{path}: sha256 {actual}, expected {expected}")
    return path


def write_tests(bench: Path, subset: str, tests: list[dict]) -> None:
    (bench / f"{subset}.jsonl").write_text("".join(json.dumps(test) + "\n" for test in tests))


def build_olmocr(
    client: httpx.Client, spec: dict, wanted: set[str], bench: Path, sources: Path
) -> None:
    base = f"https://huggingface.co/datasets/{spec['dataset']}/resolve/{spec['revision']}"
    cache = sources / "olmocr-bench" / spec["revision"]
    for subset in spec["subsets"]:
        name = subset["name"]
        if name not in wanted:
            continue
        tests_file = cached(client, f"{base}/{subset['tests']}", cache / subset["tests"])
        tests = [json.loads(line) for line in tests_file.read_text().splitlines()]
        if "sample" in subset:
            pdfs = sorted({test["pdf"] for test in tests if test["type"] != "baseline"})
            chosen = set(random.Random(spec["seed"]).sample(pdfs, subset["sample"]))
            tests = [
                dict(test, pdf=f"{name}/{Path(test['pdf']).name}")
                for test in tests
                if test["pdf"] in chosen
            ]
        else:
            chosen = {test["pdf"] for test in tests}
        out = bench / "bench_data" / "pdfs" / name
        out.mkdir(parents=True, exist_ok=True)
        for pdf in sorted(chosen):
            source = cached(
                client, f"{base}/bench_data/pdfs/{pdf}", cache / "bench_data" / "pdfs" / pdf
            )
            if subset.get("scan"):
                synthetic_scan(source, out / Path(pdf).name, name=f"{name}/{Path(pdf).name}")
            else:
                shutil.copyfile(source, out / Path(pdf).name)
        write_tests(bench, name, tests)
        print(f"{name}: {len(chosen)} PDFs, {len(tests)} tests")


def build_old_print(spec: dict, bench: Path, lab: Path) -> None:
    out = bench / "bench_data" / "pdfs" / "old_print"
    out.mkdir(parents=True, exist_ok=True)
    for page in spec["pages"]:
        image = checked(lab / "old_print" / f"{page['id']}.jpg", page["sha256"])
        image_pdf([image.read_bytes()], out / f"{page['id']}.pdf", ppi=page["ppi"])
    tests = [
        json.loads(line) for line in (BACKEND / "eval" / spec["tests"]).read_text().splitlines()
    ]
    write_tests(bench, "old_print", tests)
    print(f"old_print: {len(spec['pages'])} PDFs, {len(tests)} tests")


def build_roundtrip(spec: dict, wanted: set[str], bench: Path, lab: Path) -> None:
    """A round-trip set from its gold PDFs, and its image-only variant when
    the set names one."""
    pairs = [
        f"{document['name']}={checked(lab / spec['subset'] / document['file'], document['sha256'])}"
        for document in spec["documents"]
    ]
    prepare = [sys.executable, str(BACKEND / "scripts" / "prepare_gold_roundtrip.py")]
    if spec["subset"] in wanted:
        prepare_args = ["--bench", str(bench), "--subset", spec["subset"], *pairs]
        subprocess.run([*prepare, *prepare_args], check=True, cwd=BACKEND)
    if (scan := spec.get("scan_subset")) in wanted:
        prepare_args = ["--bench", str(bench), "--subset", scan, "--scan", *pairs]
        subprocess.run([*prepare, *prepare_args], check=True, cwd=BACKEND)


def fetch_scorer(spec: dict, sources: Path) -> None:
    """opendataloader-bench at its pinned commit, for score_structure.py."""
    checkout = sources / "opendataloader-bench"
    if not checkout.exists():
        subprocess.run(["git", "clone", "--quiet", spec["repo"], str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--quiet", spec["commit"]], check=True)


def restore_adobe(wanted: set[str], bench: Path, lab: Path) -> None:
    """Adobe's results for these subsets, as the scorers read them, from the
    Lab's copy (``adobe/<subset>/``); they cost free-tier quota to remake."""
    for results in sorted((lab / "adobe").glob("*/")):
        if results.name in wanted:
            out = bench / "bench_data" / ADOBE_CANDIDATE / results.name
            shutil.copytree(results, out, dirs_exist_ok=True)
            print(
                f"{ADOBE_CANDIDATE}/{results.name}: {len(list(results.glob('*.tagged.pdf')))} pages"
            )


def main() -> None:
    corpus = json.loads(MANIFEST.read_text())
    roundtrip_names = [
        name
        for spec in corpus["roundtrip"]
        for name in (spec["subset"], spec.get("scan_subset"))
        if name
    ]
    names = [
        *(subset["name"] for subset in corpus["olmocr_bench"]["subsets"]),
        "old_print",
        *roundtrip_names,
    ]
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench", type=Path, default=BACKEND / "data" / "eval" / "olmocr-bench")
    parser.add_argument("--sources", type=Path, default=BACKEND / "data" / "eval" / "sources")
    parser.add_argument("--subsets", nargs="+", choices=names, default=names)
    options = parser.parse_args()
    wanted = set(options.subsets)

    with httpx.Client(
        timeout=120, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        build_olmocr(client, corpus["olmocr_bench"], wanted, options.bench, options.sources)
    fetch_scorer(corpus["scorers"]["opendataloader_bench"], options.sources)
    lab = lab_copy(options.sources)
    if "old_print" in wanted:
        build_old_print(corpus["old_print"], options.bench, lab)
    for spec in corpus["roundtrip"]:
        build_roundtrip(spec, wanted, options.bench, lab)
    restore_adobe(wanted, options.bench, lab)


if __name__ == "__main__":
    main()
