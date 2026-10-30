"""OCR-stage candidates from the local-OCR plugin's side records.

    ocr_stage.py <sidecar run> <subset> [--guard CONF:SIM ...]

Writes, for each page the run recorded, ``<stem>_pg1_repeat1.md`` (the text
score_olmocr.py reads for an OCR-stage candidate) for:
  stage_tess_<run>       Tesseract's words, in hOCR order
  stage_model_<run>      the model's natural text
  stage_guard<C>_<S>_<run>  Tesseract's words with the guard applied offline
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rapidfuzz.distance import Levenshtein

EXP = Path(__file__).resolve().parent
BENCH = EXP.parent / "eval/olmocr-bench/bench_data"
SIDECARS = EXP.parent / "eval/cache/ocr-sidecar"
sys.path.insert(0, str(EXP))
source = (EXP / "ocr_local_plugin.py").read_text()
namespace: dict = {}
exec(
    "import unicodedata\nfrom rapidfuzz.distance import Levenshtein\n"
    "from rapidfuzz.process import cdist\nGAP = 0.6\n"
    + source[source.index("def fold"):source.index("def words_of")],
    namespace,
)
align, fold = namespace["align"], namespace["fold"]


def guarded(words: list[list], model_text: str, conf_limit: float, sim_limit: float) -> str:
    texts = [w[0] for w in words]
    model_words = model_text.split()
    out = list(texts)
    for i, j in align(texts, model_words):
        if i is None or j is None:
            continue
        confidence = words[i][1] if words[i][1] is not None else 1.0
        similarity = 1 - Levenshtein.normalized_distance(fold(texts[i]), fold(model_words[j]))
        if confidence < conf_limit or similarity >= sim_limit:
            out[i] = model_words[j]
    return " ".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run")
    parser.add_argument("subset")
    parser.add_argument("--guard", nargs="*", default=[])
    options = parser.parse_args()
    records = sorted((SIDECARS / options.run / options.subset).glob("*.json"))
    candidates = {f"stage_tess_{options.run}": [], f"stage_model_{options.run}": []}
    for spec in options.guard:
        candidates[f"stage_guard{spec.replace(':', '_')}_{options.run}"] = []
    for name in candidates:
        (BENCH / name / options.subset).mkdir(parents=True, exist_ok=True)
    missing = 0
    for path in records:
        record = json.loads(path.read_text())
        stem = path.stem
        words = record["tesseract_words"]
        model_text = record.get("model_text")
        if model_text is None:
            missing += 1
            model_text = " ".join(w[0] for w in words)
        texts = {
            f"stage_tess_{options.run}": record["tesseract_text"],
            f"stage_model_{options.run}": model_text,
        }
        for spec in options.guard:
            conf, sim = (float(v) for v in spec.split(":"))
            texts[f"stage_guard{spec.replace(':', '_')}_{options.run}"] = guarded(
                words, model_text, conf, sim)
        for name, text in texts.items():
            (BENCH / name / options.subset / f"{stem}_pg1_repeat1.md").write_text(text)
    print(f"{options.subset}: {len(records)} pages, {missing} without a model answer; "
          + " ".join(candidates))


if __name__ == "__main__":
    main()
