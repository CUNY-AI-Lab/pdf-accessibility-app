"""Replace Docling's table structure with a small local table model's, then re-tag.

    table_vlm.py --bench <bench_data> --source pipe_8a81fdc --subset tables_s60 \
        --model granite|paddle --out <candidate>_inputs

For every table Docling found on a captured page (``<stem>.tag-inputs``), the
table's region of the page the tagger was given is rendered and read by the
model as OTSL (rows of cells with spans and header marks). The model's cells
are matched to pdfium's words in the region (the PDF's own text, or the OCR
layer on a scan) by aligning the two word sequences; each row's and column's
extent comes from the words matched to it, and each cell's box is its row band
by its column band. The table's cells are replaced in a copy of structure.json
written to ``<bench>/<out>/<subset>/<stem>.tag-inputs/`` (input.pdf and
arguments.json linked), ready for retag_bench.py. A table the model's answer
cannot be parsed or matched for keeps Docling's cells. Model answers are
cached by crop image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
import unicodedata
from pathlib import Path

import pypdfium2 as pdfium
from rapidfuzz.distance import Levenshtein
from rapidfuzz.process import cdist

EXP = Path(__file__).resolve().parent
MODELS = {
    "granite": {
        "path": str(Path.home() / ".omlx/models/ibm-granite/granite-docling-258M-mlx"),
        "prompt": "Convert table to OTSL.",
    },
    "paddle": {"path": "mlx-community/PaddleOCR-VL-1.5-bf16", "prompt": "Table Recognition:"},
}
SCALE = 2.0  # 144 ppi
PAD = 4.0  # points around Docling's table box
GAP = 0.6
_loaded: dict = {}


# --- geometry -----------------------------------------------------------------


def frame_of(page: pdfium.PdfPage) -> dict:
    media, crop = page.get_mediabox(), page.get_cropbox()
    return {
        "rotation": page.get_rotation() % 360,
        "left": max(media[0], crop[0]),
        "bottom": max(media[1], crop[1]),
        "right": min(media[2], crop[2]),
        "top": min(media[3], crop[3]),
    }


def to_docling(frame: dict, x: float, y: float) -> tuple[float, float]:
    """A user-space point in Docling's frame (the inverse of page_frame)."""
    rotation = frame["rotation"]
    if rotation == 90:
        return y - frame["bottom"], frame["right"] - x
    if rotation == 180:
        return frame["right"] - x, frame["top"] - y
    if rotation == 270:
        return frame["top"] - y, x - frame["left"]
    return x - frame["left"], y - frame["bottom"]


def page_words(page: pdfium.PdfPage) -> list[dict]:
    """pdfium's words (runs of characters without a space or line break),
    with boxes in Docling's frame."""
    frame = frame_of(page)
    textpage = page.get_textpage()
    words: list[dict] = []
    current = None
    for index in range(textpage.count_chars()):
        char = textpage.get_text_range(index, 1)
        if not char or char.isspace() or char in "￾\x02":
            current = None
            continue
        left, bottom, right, top = textpage.get_charbox(index)
        (x0, y0), (x1, y1) = to_docling(frame, left, bottom), to_docling(frame, right, top)
        box = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        if current is not None:
            # A gap wider than a fifth of the glyph's height ends a word, as
            # a PDF may space words by position alone.
            previous = current["box"]
            gap = box[0] - previous[2]
            if gap > 0.2 * max(box[3] - box[1], 1.0) or box[3] < previous[1] or box[1] > previous[3]:
                current = None
        if current is None:
            current = {"text": char, "box": box}
            words.append(current)
        else:
            current["text"] += char
            b = current["box"]
            current["box"] = [min(b[0], box[0]), min(b[1], box[1]), max(b[2], box[2]),
                              max(b[3], box[3])]
    return words


def inside(word_box: list[float], area: dict) -> bool:
    """At least half the word inside the area (l, b, r, t)."""
    l, b, r, t = word_box
    w, h = max(r - l, 1e-6), max(t - b, 1e-6)
    ix = max(0.0, min(r, area["r"]) - max(l, area["l"])) / w
    iy = max(0.0, min(t, area["t"]) - max(b, area["b"])) / h
    return ix * iy >= 0.5


# --- model --------------------------------------------------------------------


def crop(page: pdfium.PdfPage, bbox: dict):
    width, height = page.get_size()
    image = page.render(scale=SCALE).to_pil().convert("RGB")
    box = (
        max(0, (bbox["l"] - PAD) * SCALE),
        max(0, (height - bbox["t"] - PAD) * SCALE),
        min(image.width, (bbox["r"] + PAD) * SCALE),
        min(image.height, (height - bbox["b"] + PAD) * SCALE),
    )
    return image.crop(tuple(round(v) for v in box))


def read_table(model_kind: str, image, cache: Path) -> tuple[str, float, bool]:
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    key = hashlib.sha256(model_kind.encode() + buffer.getvalue()).hexdigest()
    path = cache / f"{key}.json"
    if path.exists():
        record = json.loads(path.read_text())
        return record["text"], record["seconds"], True
    from mlx_vlm import generate, load

    spec = MODELS[model_kind]
    if model_kind not in _loaded:
        _loaded[model_kind] = load(spec["path"])
    model, processor = _loaded[model_kind]
    from mlx_vlm.prompt_utils import apply_chat_template

    prompt = apply_chat_template(processor, model.config, spec["prompt"], num_images=1)
    tmp = cache / f"{key}.png"
    image.save(tmp)
    started = time.monotonic()
    result = generate(model, processor, prompt, image=[str(tmp)], max_tokens=4096,
                      temperature=0.0, verbose=False, skip_special_tokens=False)
    seconds = time.monotonic() - started
    tmp.unlink()
    record = {"text": result.text, "seconds": round(seconds, 2),
              "tokens": result.generation_tokens, "prompt_tokens": result.prompt_tokens}
    path.write_text(json.dumps(record))
    return result.text, seconds, False


# --- OTSL ---------------------------------------------------------------------

TOKEN = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|ched|rhed|srow|nl)>")


def parse_otsl(text: str) -> list[list[dict]] | None:
    """Rows of OTSL tokens, each {"kind", "text"}; None when there are none."""
    text = re.sub(r"<loc_\d+>", "", text)
    text = re.sub(r"</?(otsl|doctag|table)>|<end_of_utterance>|</s>|<\|[^|]*\|>", "", text)
    rows: list[list[dict]] = [[]]
    position = 0
    matches = list(TOKEN.finditer(text))
    if not matches:
        return None
    for index, match in enumerate(matches):
        kind = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.end():end].strip()
        position = end
        if kind == "nl":
            rows.append([])
            continue
        rows[-1].append({"kind": kind, "text": content})
    del position
    rows = [row for row in rows if row]
    return rows or None


def grid_cells(rows: list[list[dict]]) -> list[dict]:
    """Cells with row, col, spans and header marks from OTSL rows."""
    width = max(len(row) for row in rows)
    grid = [row + [{"kind": "ecel", "text": ""}] * (width - len(row)) for row in rows]
    cells = []
    for r, row in enumerate(grid):
        for c, token in enumerate(row):
            if token["kind"] in ("lcel", "ucel", "xcel"):
                continue
            col_span = 1
            while c + col_span < width and grid[r][c + col_span]["kind"] in ("lcel", "xcel"):
                col_span += 1
            row_span = 1
            while r + row_span < len(grid) and grid[r + row_span][c]["kind"] in ("ucel", "xcel"):
                row_span += 1
            cells.append({
                "row": r, "col": c, "row_span": row_span, "col_span": col_span,
                "text": token["text"],
                "column_header": token["kind"] == "ched",
                "row_header": token["kind"] == "rhed",
                "section": token["kind"] == "srow",
            })
    return cells


# --- alignment ----------------------------------------------------------------


def fold(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word)
    letters = "".join(ch for ch in decomposed if ch.isalnum() and not unicodedata.combining(ch))
    return letters.lower() or word


def align(a: list[str], b: list[str]) -> list[tuple[int | None, int | None]]:
    n, m = len(a), len(b)
    if not n or not m:
        return []
    cost = cdist([fold(w) for w in a], [fold(w) for w in b], scorer=Levenshtein.normalized_distance)
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0] = i * GAP
    for j in range(1, m + 1):
        score[0][j] = j * GAP
    for i in range(1, n + 1):
        row, prev, costs = score[i], score[i - 1], cost[i - 1]
        for j in range(1, m + 1):
            row[j] = min(prev[j - 1] + costs[j - 1], prev[j] + GAP, row[j - 1] + GAP)
    pairs = []
    i, j = n, m
    while i and j:
        if abs(score[i][j] - (score[i - 1][j - 1] + cost[i - 1][j - 1])) < 1e-9:
            pairs.append((i - 1, j - 1, cost[i - 1][j - 1]))
            i, j = i - 1, j - 1
        elif abs(score[i][j] - (score[i - 1][j] + GAP)) < 1e-9:
            i -= 1
        else:
            j -= 1
    return pairs


def reading_order(words: list[dict]) -> list[dict]:
    """Words in rows (by vertical overlap), top to bottom, left to right."""
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: -(w["box"][1] + w["box"][3]) / 2):
        middle = (word["box"][1] + word["box"][3]) / 2
        for line in lines:
            b, t = line[0]["box"][1], line[0]["box"][3]
            if b <= middle <= t:
                line.append(word)
                break
        else:
            lines.append([word])
    return [w for line in lines for w in sorted(line, key=lambda w: w["box"][0])]


def bands(extents: dict[int, list[float]], count: int, low: float, high: float,
          descending: bool) -> list[tuple[float, float]] | None:
    """Each row's or column's band between the table's edges: boundaries
    midway between neighbors' matched words, missing ones interpolated."""
    centers: list[float | None] = []
    for index in range(count):
        values = extents.get(index)
        centers.append((values[0] + values[1]) / 2 if values else None)
    known = [i for i, v in enumerate(centers) if v is not None]
    if len(known) < 1:
        return None
    # Interpolate missing centers.
    for i in range(count):
        if centers[i] is None:
            before = max((k for k in known if k < i), default=None)
            after = min((k for k in known if k > i), default=None)
            if before is not None and after is not None:
                centers[i] = centers[before] + (centers[after] - centers[before]) * (
                    (i - before) / (after - before))
            elif before is not None:
                step = (low - centers[before]) if descending else (high - centers[before])
                centers[i] = centers[before] + step * (i - before) / (count - before)
            else:
                step = (high - centers[after]) if descending else (low - centers[after])
                centers[i] = centers[after] + step * (after - i) / (after + 1)
    edges = []
    for i in range(count - 1):
        a, b = extents.get(i), extents.get(i + 1)
        if descending:  # rows: i above i+1; boundary between bottom of i and top of i+1
            if a and b and a[0] >= b[1]:
                edges.append((a[0] + b[1]) / 2)
            else:
                edges.append((centers[i] + centers[i + 1]) / 2)
        else:  # columns: i left of i+1
            if a and b and a[1] <= b[0]:
                edges.append((a[1] + b[0]) / 2)
            else:
                edges.append((centers[i] + centers[i + 1]) / 2)
    if descending:
        bounds = [high, *edges, low]
        return [(bounds[i + 1], bounds[i]) for i in range(count)]
    bounds = [low, *edges, high]
    return [(bounds[i], bounds[i + 1]) for i in range(count)]


MIN_MATCH = 0.0
BOXES = "bands"  # or "tight": each cell's box is the union of its matched words


def rebuild(table: dict, cells: list[dict], words: list[dict]) -> tuple[list[dict], dict] | None:
    bbox = table["bbox"]
    area = {"l": bbox["l"] - PAD, "b": bbox["b"] - PAD, "r": bbox["r"] + PAD, "t": bbox["t"] + PAD}
    region = reading_order([w for w in words if inside(w["box"], area)])
    tokens = [(index, word) for index, cell in enumerate(cells) for word in cell["text"].split()]
    if not region or not tokens:
        return None
    pairs = align([w["text"] for w in region], [t[1] for t in tokens])
    matched = [(i, j) for i, j, cost in pairs if cost <= 0.5]
    rows = max(c["row"] + c["row_span"] for c in cells)
    cols = max(c["col"] + c["col_span"] for c in cells)
    row_ext: dict[int, list[float]] = {}
    col_ext: dict[int, list[float]] = {}
    for i, j in matched:
        cell = cells[tokens[j][0]]
        l, b, r, t = region[i]["box"]
        if cell["row_span"] == 1:
            e = row_ext.setdefault(cell["row"], [b, t])
            e[0], e[1] = min(e[0], b), max(e[1], t)
        if cell["col_span"] == 1:
            e = col_ext.setdefault(cell["col"], [l, r])
            e[0], e[1] = min(e[0], l), max(e[1], r)
    if BOXES == "tight":
        return tight_cells(table, cells, tokens, region, matched, rows, cols)
    row_bands = bands(row_ext, rows, bbox["b"], bbox["t"], descending=True)
    col_bands = bands(col_ext, cols, bbox["l"], bbox["r"], descending=False)
    if row_bands is None or col_bands is None:
        return None
    new_cells = []
    header_rows = set()
    for cell in cells:
        box = {
            "l": col_bands[cell["col"]][0],
            "r": col_bands[cell["col"] + cell["col_span"] - 1][1],
            "t": row_bands[cell["row"]][1],
            "b": row_bands[cell["row"] + cell["row_span"] - 1][0],
        }
        text = " ".join(w["text"] for w in region if inside(w["box"], box))
        if cell["column_header"]:
            header_rows.add(cell["row"])
        new_cells.append({
            "text": text,
            "bbox": box,
            "row": cell["row"],
            "col": cell["col"],
            "row_span": cell["row_span"],
            "col_span": cell["col_span"],
            "column_header": cell["column_header"],
            "row_header": cell["row_header"],
            "is_header": cell["column_header"] or cell["row_header"],
        })
    stats = {"rows": rows, "cols": cols, "cells": len(cells), "words": len(region),
             "tokens": len(tokens), "matched": len(matched)}
    return new_cells, stats


def tight_cells(table, cells, tokens, region, matched, rows, cols):
    """Each cell's box around the words assigned to it: matched words, and
    each unmatched word joined to the nearest matched word on its line (within
    two line heights). A cell with no words gets an empty box no word can
    fall in."""
    owner: dict[int, int] = {i: tokens[j][0] for i, j in matched}
    for i, word in enumerate(region):
        if i in owner:
            continue
        l, b, r, t = word["box"]
        middle, height = (b + t) / 2, max(t - b, 1.0)
        best, distance = None, 2 * height
        for k, cell_index in owner.items():
            kl, kb, kr, kt = region[k]["box"]
            if not kb <= middle <= kt:
                continue
            gap = max(kl - r, l - kr, 0.0)
            if gap < distance:
                best, distance = cell_index, gap
        if best is not None:
            owner[i] = best
    boxes: dict[int, list[list[float]]] = {}
    for i, cell_index in owner.items():
        boxes.setdefault(cell_index, []).append(region[i]["box"])
    corner = {"l": table["bbox"]["l"], "b": table["bbox"]["b"], "r": table["bbox"]["l"],
              "t": table["bbox"]["b"]}
    new_cells = []
    for index, cell in enumerate(cells):
        owned = boxes.get(index)
        if owned:
            box = {"l": min(b[0] for b in owned) - 0.5, "b": min(b[1] for b in owned) - 0.5,
                   "r": max(b[2] for b in owned) + 0.5, "t": max(b[3] for b in owned) + 0.5}
            text = " ".join(region[i]["text"] for i, c in sorted(owner.items()) if c == index)
        else:
            box, text = dict(corner), ""
        new_cells.append({
            "text": text, "bbox": box, "row": cell["row"], "col": cell["col"],
            "row_span": cell["row_span"], "col_span": cell["col_span"],
            "column_header": cell["column_header"], "row_header": cell["row_header"],
            "is_header": cell["column_header"] or cell["row_header"],
        })
    stats = {"rows": rows, "cols": cols, "cells": len(cells), "words": len(region),
             "tokens": len(tokens), "matched": len(matched)}
    return new_cells, stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--subset", required=True)
    parser.add_argument("--model", choices=sorted(MODELS), required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--boxes", choices=("bands", "tight"), default="bands")
    parser.add_argument("--min-match", type=float, default=0.0,
                        help="keep Docling's cells unless this share of words matched")
    options = parser.parse_args()
    global BOXES, MIN_MATCH
    BOXES = options.boxes
    MIN_MATCH = options.min_match
    cache = EXP / "table-cache" / options.model
    cache.mkdir(parents=True, exist_ok=True)
    out_dir = options.bench / options.out / options.subset
    report = []
    inputs_list = sorted((options.bench / options.source / options.subset).glob("*.tag-inputs"))
    for inputs in inputs_list[: options.limit]:
        structure = json.loads((inputs / "structure.json").read_text())
        tables = [e for e in structure["elements"] if e.get("type") == "table" and e.get("bbox")]
        target = out_dir / inputs.name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for name in ("input.pdf", "arguments.json", "capture.json"):
            if (inputs / name).exists():
                (target / name).symlink_to((inputs / name).resolve())
        if tables:
            document = pdfium.PdfDocument(inputs / "input.pdf")
            words_by_page: dict[int, list[dict]] = {}
            for table in tables:
                page_index = int(table.get("page") or 0)
                page = document[page_index]
                if page_index not in words_by_page:
                    words_by_page[page_index] = page_words(page)
                entry = {"page": inputs.name, "docling_cells": len(table.get("cells") or []),
                         "docling_shape": [table.get("num_rows"), table.get("num_cols")]}
                text, seconds, hit = read_table(options.model, crop(page, table["bbox"]), cache)
                entry.update(seconds=round(seconds, 2), cache_hit=hit)
                rows = parse_otsl(text)
                if rows is None:
                    entry["result"] = "no otsl"
                    entry["answer"] = text[:300]
                    report.append(entry)
                    continue
                rebuilt = rebuild(table, grid_cells(rows), words_by_page[page_index])
                if rebuilt is None:
                    entry["result"] = "no match"
                    report.append(entry)
                    continue
                new_cells, stats = rebuilt
                share = stats["matched"] / max(stats["tokens"], 1)
                entry["match_share"] = round(share, 3)
                if share < MIN_MATCH:
                    entry["result"] = "kept docling (low match)"
                    entry.update(stats)
                    report.append(entry)
                    continue
                table["cells"] = new_cells
                table["num_rows"], table["num_cols"] = stats["rows"], stats["cols"]
                entry.update(result="replaced", **stats)
                report.append(entry)
            document.close()
        (target / "structure.json").write_text(json.dumps(structure))
        print(inputs.name, len(tables), "tables", flush=True)
    (options.bench / options.out / f"{options.subset}.report.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in report))
    replaced = sum(entry["result"] == "replaced" for entry in report)
    seconds = [entry["seconds"] for entry in report if not entry["cache_hit"]]
    print(f"{options.subset}: {len(report)} tables, {replaced} replaced; "
          f"model seconds per table {sum(seconds) / max(len(seconds), 1):.2f} "
          f"over {len(seconds)} uncached")


if __name__ == "__main__":
    main()
