"""Prepare a structure round-trip set from well-tagged gold PDFs.

For each ``name=path`` pair, writes the gold PDF's tags stripped (the pipeline's
input) to ``<bench>/bench_data/pdfs/<subset>/<name>.pdf``, and to
``<bench>/<subset>/`` the gold structure tree as Markdown (``<name>.md``, what
``score_structure.py`` compares against) and the gold PDF itself (``<name>.pdf``,
for ``score_semantics.py``). With ``--scan`` the input is instead an
image-only scan of the gold PDF (``synthetic_scan.py``), so the round trip
also tests recognition.

    uv run python scripts/prepare_gold_roundtrip.py --bench data/eval/olmocr-bench \\
        ref_book_chapter=../gold/PDFUA-Ref-2-08_BookChapter.pdf ...
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.services.structure_text import screen_reader_text
from scripts.strip_accessibility import strip_accessibility
from scripts.synthetic_scan import synthetic_scan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--subset", default="gold_rt")
    parser.add_argument("--scan", action="store_true", help="input an image-only scan")
    parser.add_argument("gold", nargs="+", help="name=path of a well-tagged PDF")
    options = parser.parse_args()

    stripped_dir = options.bench / "bench_data" / "pdfs" / options.subset
    gold_dir = options.bench / options.subset
    gold_dir.mkdir(parents=True, exist_ok=True)
    for pair in options.gold:
        name, _, path = pair.partition("=")
        source = Path(path)
        if options.scan:
            synthetic_scan(source, stripped_dir / f"{name}.pdf", name=name)
        else:
            strip_accessibility(input_path=source, output_path=stripped_dir / f"{name}.pdf")
        (gold_dir / f"{name}.pdf").write_bytes(source.read_bytes())
        markdown = screen_reader_text(source, markdown=True)
        (gold_dir / f"{name}.md").write_text(markdown, encoding="utf-8")
        headings = sum(1 for line in markdown.splitlines() if line.startswith("#"))
        print(
            f"{name}: {len(markdown.split())} words, {headings} headings, "
            f"{markdown.count('<table>')} tables"
        )


if __name__ == "__main__":
    main()
