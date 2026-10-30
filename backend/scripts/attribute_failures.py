"""Find the stage of the pipeline at which each failed olmOCR-Bench test was lost.

For each test a captured candidate failed (``score_olmocr.py`` keeps the
per-test results), the check is repeated on what each stage handed the next,
from the page's ``<stem>.tag-inputs/`` (``pipeline_bench.py``): the text
layer of ``input.pdf``, the PDF the tagger was given (the OCR step's text on
a scan, the PDF's own text on a born-digital page), as pdfium reads the whole
page; Docling's text, ``structure.json``'s elements in their order without
artifacts and figures, tables as HTML with ``--markdown``; and the tagged
output, as it was scored. Each failure goes to one bucket, in this order:

- ``characters``: the tagged output has every word of the test in the right
  order, and the test fails on punctuation, accents, case or spacing alone
  (an ellipsis character where the test has three dots).
- ``tagger``: Docling's text passes the test, or has its words in order, and
  the tagged output does not.
- ``ocr``, ``font repair`` or ``pdf text``: a word the test needs is not in
  the text layer at all (in any order, spacing and punctuation aside):
  ``ocr`` on a page the OCR step recognized; on a born-digital page,
  ``font repair`` when the source PDF has the word and the PDF the pipeline
  rewrote to repair its fonts, then tagged again, does not, and ``pdf text``
  when the source PDF lacks it too (a font without a usable text mapping).
- ``docling``: the text layer has the words and Docling's text fails: order,
  a running head not marked as one, a table it did not find.
- ``no output``: the pipeline produced nothing for the page.

An absent test can only be the tagger's or Docling's. Each subset's
attributions and their counts by test type are written beside its results,
as ``<candidate>.attribution.jsonl`` and ``<candidate>.attribution-summary.json``.
The buckets are a guide to where to look, not a proof.

    uv run --with "olmocr[bench]==0.4.27" python scripts/attribute_failures.py \\
        --bench data/eval/olmocr-bench --candidate pipe_v3 multi_column tables --markdown
"""

from __future__ import annotations

import argparse
import html
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from app.pipeline.pdfium_text import PdfiumText
from app.services.structure_text import screen_reader_text

BUCKETS = ("ocr", "font repair", "pdf text", "docling", "tagger", "characters", "no output")
# Wider than any page: the whole page's words, in pdfium's order.
WHOLE_PAGE = {"l": -1e9, "b": -1e9, "r": 1e9, "t": 1e9}
TABLE_FIELDS = ("cell", "up", "down", "left", "right", "top_heading", "left_heading")
SKIPPED = {"artifact", "figure"}
# Accents a PDF may draw as letters of their own beside the letter (ˆ, ˜).
SPACING_MODIFIERS = {chr(code) for code in range(0x02B0, 0x0300)}


def text_layer(pdf: Path) -> str:
    """Every page's text as pdfium reads it, page after page."""
    with PdfiumText(pdf) as pages:
        return "\n".join(
            pages.in_boxes(index, [WHOLE_PAGE]) for index in range(len(pages.document))
        )


def table_html(element: dict) -> str:
    """A Docling table as an HTML table, header cells as ``th``, with spans."""
    rows: dict[int, list[dict]] = {}
    for cell in element.get("cells") or []:
        rows.setdefault(int(cell.get("row", 0)), []).append(cell)
    out = ["<table>"]
    for row in sorted(rows):
        out.append("<tr>")
        for cell in sorted(rows[row], key=lambda cell: int(cell.get("col", 0))):
            tag = "th" if cell.get("is_header") else "td"
            spans = "".join(
                f' {name}="{int(cell.get(key) or 1)}"'
                for name, key in (("rowspan", "row_span"), ("colspan", "col_span"))
                if int(cell.get(key) or 1) > 1
            )
            out.append(f"<{tag}{spans}>{html.escape(str(cell.get('text') or ''))}</{tag}>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def docling_text(structure: dict, *, markdown: bool) -> str:
    """Docling's text in the structure's order, read as the tagged output is
    scored: artifacts and figures left out, each element once."""
    blocks = []
    for element in structure.get("elements") or []:
        kind = element.get("type")
        if kind in SKIPPED or element.get("continuation"):
            continue
        if kind == "table":
            if markdown:
                blocks.append(table_html(element))
            else:
                cells = sorted(
                    element.get("cells") or [],
                    key=lambda cell: (int(cell.get("row", 0)), int(cell.get("col", 0))),
                )
                blocks.append("\n".join(str(cell.get("text") or "") for cell in cells))
            continue
        text = " ".join(str(element.get("text") or "").split())
        if not text:
            continue
        if markdown and kind == "heading":
            text = f"{'#' * max(1, min(6, int(element.get('level') or 1)))} {text}"
        elif markdown and kind == "list_item":
            text = f"- {text}"
        blocks.append(text)
    return "\n\n".join(blocks)


def fold(text: str) -> str:
    """Letters and digits only, lowercase, without accents: what is left of a
    text when punctuation, spacing, case and diacritics no longer count."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(
        char
        for char in decomposed.casefold()
        if char.isalnum() and not unicodedata.combining(char) and char not in SPACING_MODIFIERS
    )


def passages(test: dict) -> list[str]:
    """The passages a test needs, wherever it needs them."""
    kind = test.get("type")
    if kind in ("present", "absent"):
        return [test["text"]]
    if kind == "order":
        return [test["before"], test["after"]]
    if kind == "table":
        return [test[field] for field in TABLE_FIELDS if test.get(field)]
    return []


def words_in_order(test: dict, text: str) -> bool | None:
    """Whether the text has a present test's passage, or an order test's two
    passages in order, once punctuation, spacing, case and accents are set
    aside; None for other tests."""
    folded = fold(text)
    if test.get("type") == "present":
        return fold(test["text"]) in folded
    if test.get("type") == "order":
        before, after = folded.find(fold(test["before"])), folded.rfind(fold(test["after"]))
        return 0 <= before < after
    return None


def layer_has_words(test: dict, layer: str) -> bool | None:
    """Whether every word a test needs is somewhere in the text layer, in any
    order: pdfium may read a page's lines interleaved with its line numbers,
    even inside a hyphenated word. None for a test whose passage must be
    absent."""
    if test.get("type") in ("absent", "math", "baseline"):
        return None
    folded = fold(layer)
    without_numbers = re.sub(r"\d+", "", folded)
    return all(
        fold(word) in folded or fold(word) in without_numbers
        for passage in passages(test)
        for word in re.findall(r"\w+", passage)
    )


def bucket(
    *,
    tagged_in_order: bool | None,
    docling_passes: bool,
    docling_in_order: bool | None,
    layer_words: bool | None,
    source_words: bool | None = None,
    ocred: bool,
) -> str:
    """Where a failed test was lost, from the checks on each stage's text
    (``source_words``: whether the source PDF's own text has the words)."""
    if tagged_in_order:
        return "characters"
    if docling_passes or docling_in_order:
        return "tagger"
    if layer_words is False:
        if ocred:
            return "ocr"
        return "font repair" if source_words else "pdf text"
    return "docling"


def was_ocred(run_record: Path) -> bool:
    """Whether the OCR step recognized the page, from its ``run.json``; a
    capture without one is taken as recognized."""
    if not run_record.exists():
        return True
    steps = json.loads(run_record.read_text()).get("steps") or []
    return any(step["step"] == "ocr" and step["status"] == "complete" for step in steps)


def summarize(attributions: list[dict]) -> dict[str, dict[str, Counter]]:
    """Failures by subset and test type, counted by bucket."""
    summary: dict[str, dict[str, Counter]] = {}
    for entry in attributions:
        by_type = summary.setdefault(entry["subset"], {})
        by_type.setdefault("all", Counter())[entry["bucket"]] += 1
        by_type.setdefault(entry["type"], Counter())[entry["bucket"]] += 1
    return summary


def attribute_subset(bench: Path, candidate: str, subset: str, *, markdown: bool) -> list[dict]:
    from olmocr.bench.tests import load_single_test

    results_file = bench / "results" / subset / f"{candidate}.jsonl"
    failed = {
        entry["id"]
        for entry in map(json.loads, results_file.read_text().splitlines())
        if not entry["passed"]
    }
    all_tests = map(json.loads, (bench / f"{subset}.jsonl").read_text().splitlines())
    tests = [test for test in all_tests if test["id"] in failed]
    pages = bench / "bench_data" / candidate / subset
    texts: dict[str, tuple[str, str, str]] = {}
    attributions = []
    for raw in tests:
        stem = Path(raw["pdf"]).stem
        inputs = pages / f"{stem}.tag-inputs"
        entry = {"subset": subset, "id": raw["id"], "pdf": raw["pdf"], "type": raw["type"]}
        if not inputs.exists():
            attributions.append({**entry, "bucket": "no output"})
            continue
        if stem not in texts:
            structure = json.loads((inputs / "structure.json").read_text())
            texts[stem] = (
                text_layer(inputs / "input.pdf"),
                docling_text(structure, markdown=markdown),
                screen_reader_text(pages / f"{stem}.tagged.pdf", markdown=markdown),
            )
        layer, docling, tagged = texts[stem]
        try:
            docling_passes, _ = load_single_test(raw).run(docling)
        except Exception:  # noqa: BLE001 - olmOCR-Bench counts it as a failure
            docling_passes = False
        checks = {
            "tagged_in_order": words_in_order(raw, tagged),
            "docling_passes": bool(docling_passes),
            "docling_in_order": words_in_order(raw, docling),
            "layer_words": layer_has_words(raw, layer),
        }
        ocred = was_ocred(pages / f"{stem}.run.json")
        if checks["layer_words"] is False and not ocred:
            source = bench / "bench_data" / "pdfs" / raw["pdf"]
            checks["source_words"] = layer_has_words(raw, text_layer(source))
        attributions.append({**entry, **checks, "bucket": bucket(**checks, ocred=ocred)})
    return attributions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--markdown", action="store_true", help="read Docling's tables as HTML")
    parser.add_argument("subsets", nargs="+")
    options = parser.parse_args()

    attributions = []
    for subset in options.subsets:
        found = attribute_subset(
            options.bench, options.candidate, subset, markdown=options.markdown
        )
        out = options.bench / "results" / subset
        (out / f"{options.candidate}.attribution.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in found)
        )
        (out / f"{options.candidate}.attribution-summary.json").write_text(
            json.dumps(summarize(found).get(subset, {}), indent=1)
        )
        attributions.extend(found)

    summary = summarize(attributions)
    print(f"{'subset':<26}{'type':<9}{'failed':>7}" + "".join(f"{b:>13}" for b in BUCKETS))
    for subset, by_type in summary.items():
        for kind, counts in sorted(by_type.items(), key=lambda item: item[0] != "all"):
            row = f"{subset if kind == 'all' else '':<26}{kind:<9}{sum(counts.values()):>7}"
            print(row + "".join(f"{counts.get(b, 0):>13}" for b in BUCKETS))


if __name__ == "__main__":
    main()
