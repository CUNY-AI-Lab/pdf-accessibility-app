"""The model's own table HTML in place of each tagged table: an upper bound for
the substitution, with the model's text and structure and no alignment.

    table_direct.py <subset> <model> <base candidate> <out candidate>

Reads the base candidate's scored page text (views/<subset>/<base>/), replaces
the k-th <table> with the k-th Docling table's model answer (from the
table_vlm.py cache) as HTML, and writes ``<stem>_pg1_repeat1.md`` pages.
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

import pypdfium2 as pdfium

EXP = Path(__file__).resolve().parent
sys.path.insert(0, str(EXP))
import table_vlm as T  # noqa: E402

BENCH = EXP.parent / "eval/olmocr-bench"


def to_html(cells: list[dict]) -> str:
    rows: dict[int, list[dict]] = {}
    for cell in cells:
        rows.setdefault(cell["row"], []).append(cell)
    out = ["<table>"]
    for row in sorted(rows):
        out.append("<tr>")
        for cell in sorted(rows[row], key=lambda c: c["col"]):
            tag = "th" if cell["column_header"] or cell["row_header"] else "td"
            spans = "".join(f' {n}="{v}"' for n, v in (("rowspan", cell["row_span"]),
                                                       ("colspan", cell["col_span"])) if v > 1)
            out.append(f"<{tag}{spans}>{html.escape(cell['text'])}</{tag}>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def main() -> None:
    subset, model, base, out = sys.argv[1:5]
    cache = EXP / "table-cache" / model
    out_dir = BENCH / "bench_data" / out / subset
    out_dir.mkdir(parents=True, exist_ok=True)
    replaced = kept = 0
    for inputs in sorted((BENCH / "bench_data/pipe_8a81fdc" / subset).glob("*.tag-inputs")):
        stem = inputs.name.removesuffix(".tag-inputs")
        page_md = BENCH / "views" / subset / base / subset / f"{stem}_pg1_repeat1.md"
        text = page_md.read_text()
        structure = json.loads((inputs / "structure.json").read_text())
        tables = [e for e in structure["elements"] if e.get("type") == "table" and e.get("bbox")]
        answers = []
        if tables:
            document = pdfium.PdfDocument(inputs / "input.pdf")
            for table in tables:
                answer, _, hit = T.read_table(model, T.crop(document[int(table.get("page") or 0)],
                                                            table["bbox"]), cache)
                rows = T.parse_otsl(answer)
                answers.append(to_html(T.grid_cells(rows)) if rows else None)
            document.close()
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
    print(f"{subset}: {replaced} tables replaced by the model's HTML, {kept} kept")


if __name__ == "__main__":
    main()
