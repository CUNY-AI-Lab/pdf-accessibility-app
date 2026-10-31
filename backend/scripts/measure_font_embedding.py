"""Offline font-only repair of frozen pipeline outputs and their original sources.

Run from backend with PYTHONPATH=.; inputs are read-only. --font-dir restricts
lookup to the supplied installed fonts, so macOS Arial cannot hide a missing
Liberation fallback. No OCR, tagging, AI, or release decisions are replayed.
"""

import argparse
import asyncio
import ctypes
import hashlib
import json
from pathlib import Path

import numpy as np
import pikepdf
import pypdfium2 as pdfium

from app.pipeline.fonts import programs
from app.pipeline.fonts.embedding import _embed_system_fonts_sync
from app.pipeline.fonts.resources import _iter_pdf_fonts
from app.pipeline.validator import validate_pdf
from app.services.structure_text import screen_reader_text


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def font_text_contract(path: Path) -> list:
    """Keep encoding dictionaries, character widths and ToUnicode byte-exact."""
    def plain(value):
        if isinstance(value, pikepdf.Stream):
            return digest(value.read_bytes())
        if isinstance(value, pikepdf.Dictionary):
            return {str(k): plain(v) for k, v in value.items()}
        if isinstance(value, pikepdf.Array):
            return [plain(v) for v in value]
        return str(value)

    with pikepdf.open(path) as pdf:
        return [
            {key: plain(font.get(key)) for key in (
                "/BaseFont", "/Encoding", "/ToUnicode", "/Widths", "/FirstChar", "/LastChar",
                "/W", "/DW", "/CIDToGIDMap",
            )}
            for font in _iter_pdf_fonts(pdf)
        ]


def compare_text_and_rendering(before: Path, after: Path, image_dir: Path) -> dict:
    sr_before, sr_after = screen_reader_text(before), screen_reader_text(after)
    metrics = []
    extracted_before, extracted_after = [], []
    max_origin_delta = 0.0
    with pdfium.PdfDocument(before) as a, pdfium.PdfDocument(after) as b:
        if len(a) != len(b):
            raise ValueError("Embedding changed the page count")
        for index in range(len(a)):
            pa, pb = a[index], b[index]
            try:
                ta, tb = pa.get_textpage(), pb.get_textpage()
                try:
                    extracted_before.append(ta.get_text_bounded())
                    extracted_after.append(tb.get_text_bounded())
                    if ta.count_chars() != tb.count_chars():
                        max_origin_delta = float("inf")
                    else:
                        for char in range(ta.count_chars()):
                            ax, ay, bx, by = (ctypes.c_double() for _ in range(4))
                            pdfium.raw.FPDFText_GetCharOrigin(ta, char, ax, ay)
                            pdfium.raw.FPDFText_GetCharOrigin(tb, char, bx, by)
                            max_origin_delta = max(
                                max_origin_delta, abs(ax.value - bx.value), abs(ay.value - by.value),
                            )
                finally:
                    ta.close()
                    tb.close()
                ba, bb = pa.render(scale=1.5), pb.render(scale=1.5)
                try:
                    ia, ib = ba.to_pil().convert("RGB"), bb.to_pil().convert("RGB")
                finally:
                    ba.close()
                    bb.close()
                va, vb = np.asarray(ia).astype(np.int16), np.asarray(ib).astype(np.int16)
                if va.shape != vb.shape:
                    raise ValueError("Embedding changed page dimensions")
                difference = np.abs(va - vb)
                metrics.append({
                    "page": index + 1,
                    "mae_0_255": float(difference.mean()),
                    "pixels_changed_over_16_fraction": float((difference.max(axis=2) > 16).mean()),
                })
                if index == 0:
                    image_dir.mkdir(parents=True, exist_ok=True)
                    ia.save(image_dir / "before.png")
                    ib.save(image_dir / "after.png")
            finally:
                pa.close()
                pb.close()
    return {
        "screen_reader_text_unchanged": sr_before == sr_after,
        "screen_reader_text_sha256_before": digest(sr_before.encode()),
        "screen_reader_text_sha256_after": digest(sr_after.encode()),
        "screen_reader_characters": len(sr_before),
        "pdfium_text_unchanged": extracted_before == extracted_after,
        "font_text_contract_unchanged": font_text_contract(before) == font_text_contract(after),
        "max_character_origin_delta_pt": max_origin_delta,
        "render_scale": 1.5,
        "pages": metrics,
    }


async def measure(args):
    args.output.mkdir(parents=True, exist_ok=True)
    if args.font_dir:
        programs.SYSTEM_FONT_DIRS = tuple(args.font_dir)
        programs._system_font_index.cache_clear()
        programs._system_font_program.cache_clear()
    semaphore = asyncio.Semaphore(4)

    async def validation(path, report_path):
        async with semaphore:
            result = await validate_pdf(path, verapdf_path=args.verapdf)
        if result.error:
            raise RuntimeError(f"{path}: {result.error}")
        report_path.write_text(json.dumps(result.raw_report, indent=2))
        return {
            "compliant": result.compliant,
            "rules": sorted({v.rule_id for v in result.violations if v.severity == "error"}),
        }

    async def one(path, key, outcome=None):
        folder = args.output / key
        folder.mkdir(parents=True, exist_ok=True)
        after = folder / "embedded.pdf"
        before_validation = await validation(path, folder / "before.verapdf.json")
        if any("-7.21.4.1-" in rule for rule in before_validation["rules"]):
            ok, message, stats = _embed_system_fonts_sync(path, after)
        else:
            ok, message, stats = False, "No embedding violation; PDF unchanged", {}
        target = after if ok else path
        after_validation = await validation(target, folder / "after.verapdf.json")
        row = {
            "key": key, "source": str(path), "source_sha256": digest(path.read_bytes()),
            "embedding_changed_pdf": ok, "message": message, "stats": stats,
            "before": before_validation, "after": after_validation,
            "saved_outcome": outcome,
        }
        if ok:
            # PDFium/font caches and structure-text extraction stay on one thread.
            row["fidelity"] = compare_text_and_rendering(path, after, folder)
        (folder / "result.json").write_text(json.dumps(row, indent=2))
        print(key, before_validation, "->", after_validation, flush=True)
        return row

    tasks = []
    for run in sorted(args.runs.glob("*/*.run.json")):
        saved = json.loads(run.read_text())
        subset = run.parent.name
        stem = run.name.removesuffix(".run.json")
        tagged = run.with_name(f"{stem}.tagged.pdf")
        tasks.append(one(tagged, f"tagged/{subset}/{stem}", saved["outcome"]))
        if subset.startswith("cuny_"):
            candidates = [root / subset / f"{stem}.pdf" for root in args.source_root]
            source = next((candidate for candidate in candidates if candidate.is_file()), None)
            if source is None:
                raise FileNotFoundError(f"No source for {subset}/{stem}")
            tasks.append(one(source, f"source/{subset}/{stem}"))
    rows = await asyncio.gather(*tasks)
    report = {
        "font_dirs": [str(p) for p in programs.SYSTEM_FONT_DIRS],
        "font_program_sha256": {
            str(p): digest(p.read_bytes())
            for root in args.font_dir or [] for p in sorted(root.rglob("*.ttf"))
        },
        "verapdf": args.verapdf,
        "rows": rows,
    }
    (args.output / "results.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, action="append", required=True)
    parser.add_argument("--font-dir", type=Path, action="append")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verapdf", default="/opt/homebrew/bin/verapdf")
    asyncio.run(measure(parser.parse_args()))
