"""The model's own table HTML in place of each tagged table: the upper bound
of the table-model step, with the model's text and grid and no alignment.

    table_model_upper_bound.py <subset> <base candidate> <out candidate> [--cache DIR]

Reads what a screen reader hears from the base candidate's tagged pages, replaces
the k-th <table> with the k-th Docling table's cached model answer as HTML,
and writes ``<stem>_pg1_repeat1.md`` pages under bench_data/<out>/<subset>/.
Run from backend/.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

from app.pipeline.table_model import AnswerCache, parse_otsl, prepare_tables
from app.services.structure_text import screen_reader_text

BENCH = Path("data/eval/olmocr-bench")


def to_html(grid) -> str:
    rows: dict[int, list] = {}
    for cell in grid.cells:
        rows.setdefault(cell.row, []).append(cell)
    out = ["<table>"]
    for row in sorted(rows):
        out.append("<tr>")
        for cell in sorted(rows[row], key=lambda c: c.col):
            spans = "".join(f' {n}="{v}"' for n, v in (("rowspan", cell.row_span),
                                                       ("colspan", cell.col_span)) if v > 1)
            out.append(f"<td{spans}>{html.escape(cell.text)}</td>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("subset")
    parser.add_argument("base")
    parser.add_argument("out")
    parser.add_argument("--cache", type=Path, default=Path("data/eval/cache/table-model"))
    parser.add_argument("--model", default="paddleocr-vl-1.5")
    options = parser.parse_args()
    cache = AnswerCache(options.cache, options.model)
    out_dir = BENCH / "bench_data" / options.out / options.subset
    out_dir.mkdir(parents=True, exist_ok=True)
    replaced = kept = 0
    for inputs in sorted((BENCH / "bench_data/pipe_8a81fdc" / options.subset).glob("*.tag-inputs")):
        stem = inputs.name.removesuffix(".tag-inputs")
        tagged = BENCH / "bench_data" / options.base / options.subset / f"{stem}.tagged.pdf"
        text = screen_reader_text(tagged, markdown=True, figure_text=False)
        structure = json.loads((inputs / "structure.json").read_text())
        answers = []
        for prep in prepare_tables(inputs / "input.pdf", structure.get("elements") or []):
            answer = cache.get(prep.png)
            grid = parse_otsl(answer.text) if answer else None
            answers.append(to_html(grid) if grid else None)
        found = list(re.finditer(r"<table>.*?</table>", text, re.DOTALL))
        pieces, last = [], 0
        for index, match in enumerate(found):
            pieces.append(text[last:match.start()])
            if index < len(answers) and answers[index]:
                pieces.append(answers[index])
                replaced += 1
            else:
                pieces.append(match.group(0))
                kept += 1
            last = match.end()
        pieces.append(text[last:])
        (out_dir / f"{stem}_pg1_repeat1.md").write_text("".join(pieces))
    print(f"{options.subset}: {replaced} tables replaced by the model's HTML, {kept} kept")


if __name__ == "__main__":
    main()
