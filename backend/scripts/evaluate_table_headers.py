"""Compare offline re-tags with author-tagged round-trip table exports.

Inputs are structure_text Markdown exports, fidelity no-header JSON reports,
and captured structure.json. All source paths are read-only. Normalize Unicode,
case, punctuation and spaces; match tables within a document by text similarity
(>=0.5). Report unmatched tables separately. Join split leading header rows.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

from rapidfuzz.fuzz import ratio


def normalized(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


class Tables(HTMLParser):
    def __init__(self, markdown: str):
        super().__init__()
        self.tables: list[list[dict]] = []
        self.row, self.col = -1, 0
        self.occupied: set[tuple[int, int]] = set()
        self.cell: dict | None = None
        self.feed(markdown)

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
            self.row, self.occupied = -1, set()
        elif tag == "tr":
            self.row, self.col = self.row + 1, 0
        elif tag in {"th", "td"}:
            attrs = dict(attrs)
            while (self.row, self.col) in self.occupied:
                self.col += 1
            self.cell = dict(role=tag, text="", row=self.row, col=self.col)
            width, height = int(attrs.get("colspan", 1)), int(attrs.get("rowspan", 1))
            self.occupied.update((r, c) for r in range(self.row, self.row + height)
                                 for c in range(self.col, self.col + width))
            self.col += width
            self.tables[-1].append(self.cell)

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data

    def handle_endtag(self, tag):
        if tag in {"th", "td"}:
            self.cell = None


def signature(table):
    return " ".join(normalized(c["text"]) for c in table if normalized(c["text"]))


def header_groups(table):
    groups = {}
    for row in sorted({c["row"] for c in table}):
        cells = [c for c in table if c["row"] == row and normalized(c["text"])]
        if any(c["role"] != "th" for c in cells):
            break
        for cell in cells:
            groups.setdefault(cell["col"], []).append(cell["text"])
    return {col: normalized(" ".join(parts)) for col, parts in groups.items()}


def compare(before: Path, after: Path, gold: Path, captures: Path) -> dict:
    counts = {v: dict(inferred_correct=0, inferred_incorrect=0, inferred_unmatched=0,
                      header_tables_found=0, header_tables_total=0,
                      header_cells_found=0, header_cells_total=0) for v in ("before", "after")}
    records, holds = [], {v: [] for v in counts}
    for file in sorted(before.glob("*/*.md")):
        subset, doc = file.parent.name, file.stem
        key = f"{subset}/{doc}"
        candidates = {}
        for version, directory in (("before", before), ("after", after)):
            candidates[version] = Tables((directory / subset / file.name).read_text()).tables
            if json.loads((directory / subset / f"{doc}.holds.json").read_text()):
                holds[version].append(key)
        structure = json.loads((captures / subset / f"{doc}.tag-inputs" / "structure.json").read_text())
        extracted = [e for e in structure["elements"] if e.get("type") == "table"
                     and e.get("num_rows", 0) > 0 and e.get("num_cols", 0) > 0]
        assert len(extracted) == len(candidates["before"]) == len(candidates["after"]), key
        reference = gold / subset / file.name
        references = Tables(reference.read_text()).tables if reference.exists() else []
        found_tables, found_cells = {v: set() for v in counts}, {v: set() for v in counts}
        for index, (old, new, original) in enumerate(zip(candidates["before"], candidates["after"], extracted, strict=True)):
            old_headers = {(c["row"], c["col"]) for c in old if c["role"] == "th"}
            added = [c for c in new if c["role"] == "th" and (c["row"], c["col"]) not in old_headers]
            scores = [ratio(signature(new), signature(ref)) / 100 for ref in references]
            best = max(range(len(scores)), key=scores.__getitem__) if scores else None
            matched = best is not None and scores[best] >= 0.5
            ref = references[best] if matched else []
            groups = header_groups(ref)
            gold_headers = {normalized(c["text"]) for c in ref if c["role"] == "th" and normalized(c["text"])}

            def correct(cell):
                value = normalized(cell["text"])
                return value in gold_headers or (cell["row"] == 0 and value == groups.get(cell["col"]))

            original_headers = {(c.get("row", 0), c.get("col", 0)) for c in original.get("cells", [])
                                if c.get("is_header") or c.get("row_header") or c.get("column_header")}
            evaluations = {}
            for version, table in (("before", old), ("after", new)):
                inferred = [c for c in table if c["role"] == "th" and normalized(c["text"])
                            and (c["row"], c["col"]) not in original_headers]
                evaluations[version] = dict(correct=sum(correct(c) for c in inferred) if matched else 0,
                                            incorrect=sum(not correct(c) for c in inferred) if matched else 0,
                                            unmatched=len(inferred) if not matched else 0)
                if reference.exists():
                    for kind, number in evaluations[version].items():
                        counts[version][f"inferred_{kind}"] += number
                if not matched:
                    continue
                headers = [c for c in table if c["role"] == "th" and normalized(c["text"])]
                if headers and gold_headers:
                    found_tables[version].add(best)
                for ci, cell in enumerate(ref):
                    value = normalized(cell["text"])
                    if cell["role"] != "th" or not value:
                        continue
                    if any(normalized(c["text"]) == value or (
                        c["row"] == 0 and cell["col"] == c["col"]
                        and normalized(c["text"]) == groups.get(c["col"])
                        and value in normalized(c["text"])) for c in headers):
                        found_cells[version].add((best, ci))
            records.append(dict(document=key, table=index + 1, matched_gold_table=best + 1 if matched else None,
                                similarity=max(scores, default=0), added=added, inference=evaluations))
        if reference.exists():
            for version in counts:
                counts[version]["header_tables_total"] += sum(any(c["role"] == "th" and normalized(c["text"]) for c in t) for t in references)
                counts[version]["header_tables_found"] += len(found_tables[version])
                counts[version]["header_cells_total"] += sum(c["role"] == "th" and bool(normalized(c["text"])) for t in references for c in t)
                counts[version]["header_cells_found"] += len(found_cells[version])
    return dict(counts=counts, held_before=holds["before"], held_after=holds["after"],
                cleared=sorted(set(holds["before"]) - set(holds["after"])), tables=records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("before", "after", "gold", "captures", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.before, args.after, args.gold, args.captures)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "tables"}, indent=2))


if __name__ == "__main__":
    main()
