"""Replay captured tagger inputs locally, without OCR, Docling, or model calls.

Read the frozen outcome directory; write PDFs and title evidence to a separate
directory. Accuracy judgments belong in the accompanying first-page review,
not in an automatic string-match score.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path

import pikepdf

from app.pipeline.document_title import select_document_title
from app.pipeline.tagger import tag_pdf


def _titles(path: Path) -> dict[str, str]:
    with pikepdf.open(path) as pdf:
        info = str(pdf.docinfo.get("/Title", ""))
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
            xmp = str(meta.get("dc:title", ""))
        return {"info": info, "xmp": xmp}


async def evaluate(source: Path, destination: Path) -> None:
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Use a separate output directory outside the frozen outcomes")
    rows = []
    for run_path in sorted(source.glob("*/*.run.json")):
        name = run_path.name.removesuffix(".run.json")
        inputs = run_path.parent / f"{name}.tag-inputs"
        structure = json.loads((inputs / "structure.json").read_text())
        arguments = json.loads((inputs / "arguments.json").read_text())
        output_dir = destination / run_path.parent.name
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{name}.tagged.pdf"
        decision = select_document_title(inputs / "input.pdf", structure, arguments["original_filename"])
        await tag_pdf(inputs / "input.pdf", output, structure, **arguments)
        after = _titles(output)
        assert after["info"] == after["xmp"] == decision["title"], output
        run = json.loads(run_path.read_text())
        rows.append({
            "set": run_path.parent.name, "document": name,
            "before": _titles(run_path.parent / f"{name}.tagged.pdf"), "after": after,
            "captured_structure_title": json.loads((inputs / "structure.json").read_text()).get("title"),
            "decision": decision, "saved_status": run.get("status"),
            "saved_outcome": run.get("outcome"),
            "input_sha256": hashlib.sha256((inputs / "input.pdf").read_bytes()).hexdigest(),
            "structure_sha256": hashlib.sha256((inputs / "structure.json").read_bytes()).hexdigest(),
            "run_sha256": hashlib.sha256(run_path.read_bytes()).hexdigest(),
            "pipeline_rewrote_after_tagging": (inputs / "rewritten-after-tagging").exists(),
        })
        (destination / "measurements.json").write_text(json.dumps(rows, indent=2) + "\n")
        print(f"{len(rows)} {run_path.parent.name}/{name}: {after['info']}", flush=True)
    print(f"{len(rows)} PDFs replayed; Info/XMP agree on every title. No outcomes rescored.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcomes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(evaluate(args.outcomes.resolve(), args.output.resolve()))


if __name__ == "__main__":
    main()
