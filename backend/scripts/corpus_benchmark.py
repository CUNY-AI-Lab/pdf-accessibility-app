#!/usr/bin/env python3
"""Run structure/tagging/validation benchmarks across a local PDF corpus."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import pikepdf

from app.config import get_settings
from app.pipeline.ocr import run_ocr
from app.pipeline.structure import extract_structure
from app.pipeline.tagger import tag_pdf
from app.pipeline.validator import validate_pdf


ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT_DIR / "data" / "benchmarks"
SCAN_ROOTS = [
    Path("/Users/stephenzweibel/Downloads"),
    Path("/Users/stephenzweibel/Documents"),
    Path("/Users/stephenzweibel/Desktop"),
]
EXCLUDE_PARTS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    ".trash",
    "Library",
}
EXCLUDE_NAME_PATTERNS = (
    "_tagged.pdf",
    "_fontfix_tagged.pdf",
    "_fontfix_ocred.pdf",
    ".gsfix.pdf",
    ".repaired.pdf",
    "repaired_input.pdf",
    "accessible_",
)


@dataclass
class DocMetrics:
    source_path: str
    file_size_bytes: int
    pages: int
    link_annots_in_source: int
    structure_secs: float
    tagging_secs: float
    validation_secs: float
    total_secs: float
    structure_ok: bool
    tag_ok: bool
    validation_ok: bool
    error: str
    baseline_validator: str
    baseline_compliant: bool
    baseline_validation_errors: int
    baseline_validation_warnings: int
    validator: str
    compliant: bool
    validation_errors: int
    validation_warnings: int
    validation_errors_reduced: int
    validation_warnings_reduced: int
    font_lane_attempted: bool
    font_lane_applied: bool
    font_lane_first_errors: int
    font_lane_first_warnings: int
    font_lane_second_errors: int
    font_lane_second_warnings: int
    elements_total: int
    elements_headings: int
    elements_figures: int
    elements_tables: int
    elements_list_items: int
    tags_total: int
    struct_elems_created: int
    headings_tagged: int
    figures_tagged: int
    decorative_figures_artifacted: int
    tables_tagged: int
    lists_tagged: int
    links_tagged: int
    bookmarks_added: int
    heading_coverage: float
    figure_coverage: float
    table_coverage: float
    list_coverage: float
    link_coverage: float


def _should_skip(path: Path) -> bool:
    lowered = [part.lower() for part in path.parts]
    if any(ex.lower() in lowered for ex in EXCLUDE_PARTS):
        return True

    name = path.name.lower()
    if any(pattern in name for pattern in EXCLUDE_NAME_PATTERNS):
        return True

    return False


def discover_pdfs(exclude_wac: bool = False) -> list[Path]:
    found: dict[str, Path] = {}
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.pdf"):
            if _should_skip(path):
                continue
            key = str(path.resolve())
            found[key] = path

    wac = ROOT_DIR / "test_wac.pdf"
    if wac.exists() and not exclude_wac:
        found[str(wac.resolve())] = wac

    # most-recent first
    ordered = sorted(
        found.values(),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    return ordered


def _safe_ratio(num: int, denom: int) -> float:
    if denom <= 0:
        return 1.0
    return num / denom


def _violation_count(validation, severity: str) -> int:
    total = 0
    for violation in validation.violations:
        if severity == "error" and violation.severity != "error":
            continue
        if severity != "error" and violation.severity == "error":
            continue
        count = violation.count if isinstance(violation.count, int) and violation.count > 0 else 1
        total += count
    return total


def _font_only_errors(validation) -> bool:
    errors = [v for v in validation.violations if v.severity == "error"]
    if not errors:
        return False
    return all("-7.21." in str(v.rule_id) for v in errors)


def _count_source_links(path: Path) -> tuple[int, int]:
    try:
        with pikepdf.open(str(path)) as pdf:
            total = 0
            for page in pdf.pages:
                annots = page.get("/Annots")
                if not isinstance(annots, pikepdf.Array):
                    continue
                for annot in annots:
                    try:
                        if annot.get("/Subtype") == pikepdf.Name("/Link"):
                            total += 1
                    except Exception:
                        continue
            return len(pdf.pages), total
    except Exception:
        return -1, 0


async def benchmark_one(pdf_path: Path, run_dir: Path, settings) -> DocMetrics:
    started = time.perf_counter()
    structure_secs = 0.0
    tagging_secs = 0.0
    validation_secs = 0.0
    structure_ok = False
    tag_ok = False
    validation_ok = False
    error = ""
    baseline_validator = "unknown"
    baseline_compliant = False
    baseline_validation_errors = 0
    baseline_validation_warnings = 0
    validator = "unknown"
    compliant = False
    validation_errors = 0
    validation_warnings = 0
    validation_errors_reduced = 0
    validation_warnings_reduced = 0
    font_lane_attempted = False
    font_lane_applied = False
    font_lane_first_errors = 0
    font_lane_first_warnings = 0
    font_lane_second_errors = 0
    font_lane_second_warnings = 0

    pages, source_links = _count_source_links(pdf_path)
    size = pdf_path.stat().st_size if pdf_path.exists() else 0

    elements_total = 0
    elements_headings = 0
    elements_figures = 0
    elements_tables = 0
    elements_list_items = 0

    tags_total = 0
    struct_elems_created = 0
    headings_tagged = 0
    figures_tagged = 0
    decorative_figures_artifacted = 0
    tables_tagged = 0
    lists_tagged = 0
    links_tagged = 0
    bookmarks_added = 0

    structure = None
    tagged_path = run_dir / f"{pdf_path.stem}_tagged.pdf"
    tag_input_path = pdf_path

    try:
        baseline_validation = await validate_pdf(
            pdf_path=pdf_path,
            verapdf_path=settings.verapdf_path,
            flavour=settings.verapdf_flavour,
        )
        baseline_compliant = baseline_validation.compliant
        baseline_validation_errors = _violation_count(baseline_validation, "error")
        baseline_validation_warnings = _violation_count(baseline_validation, "warning")
        baseline_validator = (
            "veraPDF"
            if baseline_validation.raw_report.get("report")
            else baseline_validation.raw_report.get("validator", "unknown")
        )

        t0 = time.perf_counter()
        structure = await extract_structure(pdf_path, run_dir)
        structure_secs = time.perf_counter() - t0
        structure_ok = True
        if structure.processed_pdf_path:
            tag_input_path = structure.processed_pdf_path

        elements = structure.document_json.get("elements", [])
        elements_total = len(elements)
        elements_headings = sum(1 for e in elements if e.get("type") == "heading")
        elements_figures = sum(1 for e in elements if e.get("type") == "figure")
        elements_tables = sum(1 for e in elements if e.get("type") == "table")
        elements_list_items = sum(1 for e in elements if e.get("type") == "list_item")

        t1 = time.perf_counter()
        tagging = await tag_pdf(
            input_path=tag_input_path,
            output_path=tagged_path,
            structure_json=structure.document_json,
            alt_texts=[],
            original_filename=pdf_path.name,
        )
        tagging_secs = time.perf_counter() - t1
        tag_ok = True

        tags_total = tagging.tags_added
        struct_elems_created = tagging.struct_elems_created
        headings_tagged = tagging.headings_tagged
        figures_tagged = tagging.figures_tagged
        decorative_figures_artifacted = tagging.decorative_figures_artifacted
        tables_tagged = tagging.tables_tagged
        lists_tagged = tagging.lists_tagged
        links_tagged = tagging.links_tagged
        bookmarks_added = tagging.bookmarks_added

        t2 = time.perf_counter()
        validation = await validate_pdf(
            pdf_path=tagged_path,
            verapdf_path=settings.verapdf_path,
            flavour=settings.verapdf_flavour,
        )
        validation_secs = time.perf_counter() - t2
        validation_ok = True

        selected_validation = validation
        font_lane_first_errors = _violation_count(validation, "error")
        font_lane_first_warnings = _violation_count(validation, "warning")
        font_lane_second_errors = font_lane_first_errors
        font_lane_second_warnings = font_lane_first_warnings

        if not validation.compliant and _font_only_errors(validation):
            font_lane_attempted = True
            fontfix_input = run_dir / f"{pdf_path.stem}_fontfix_ocred.pdf"
            ocr_result = await run_ocr(
                input_path=tag_input_path,
                output_path=fontfix_input,
                language=settings.ocr_language,
                mode="redo",
            )
            if ocr_result.success:
                fontfix_tagged = run_dir / f"{pdf_path.stem}_fontfix_tagged.pdf"
                fontfix_tagging = await tag_pdf(
                    input_path=ocr_result.output_path,
                    output_path=fontfix_tagged,
                    structure_json=structure.document_json,
                    alt_texts=[],
                    original_filename=pdf_path.name,
                )
                candidate_validation = await validate_pdf(
                    pdf_path=fontfix_tagging.output_path,
                    verapdf_path=settings.verapdf_path,
                    flavour=settings.verapdf_flavour,
                )
                font_lane_second_errors = _violation_count(candidate_validation, "error")
                font_lane_second_warnings = _violation_count(candidate_validation, "warning")

                improved = (
                    candidate_validation.compliant
                    or font_lane_second_errors < font_lane_first_errors
                    or (
                        font_lane_second_errors == font_lane_first_errors
                        and font_lane_second_warnings < font_lane_first_warnings
                    )
                )
                if improved:
                    selected_validation = candidate_validation
                    tags_total = fontfix_tagging.tags_added
                    struct_elems_created = fontfix_tagging.struct_elems_created
                    headings_tagged = fontfix_tagging.headings_tagged
                    figures_tagged = fontfix_tagging.figures_tagged
                    decorative_figures_artifacted = fontfix_tagging.decorative_figures_artifacted
                    tables_tagged = fontfix_tagging.tables_tagged
                    lists_tagged = fontfix_tagging.lists_tagged
                    links_tagged = fontfix_tagging.links_tagged
                    bookmarks_added = fontfix_tagging.bookmarks_added
                    font_lane_applied = True

        compliant = selected_validation.compliant
        validation_errors = _violation_count(selected_validation, "error")
        validation_warnings = _violation_count(selected_validation, "warning")
        validation_errors_reduced = baseline_validation_errors - validation_errors
        validation_warnings_reduced = baseline_validation_warnings - validation_warnings
        validator = (
            "veraPDF"
            if selected_validation.raw_report.get("report")
            else selected_validation.raw_report.get("validator", "unknown")
        )
    except Exception as exc:
        error = re.sub(r"\s+", " ", str(exc)).strip()

    total_secs = time.perf_counter() - started
    return DocMetrics(
        source_path=str(pdf_path),
        file_size_bytes=size,
        pages=pages,
        link_annots_in_source=source_links,
        structure_secs=round(structure_secs, 3),
        tagging_secs=round(tagging_secs, 3),
        validation_secs=round(validation_secs, 3),
        total_secs=round(total_secs, 3),
        structure_ok=structure_ok,
        tag_ok=tag_ok,
        validation_ok=validation_ok,
        error=error,
        baseline_validator=baseline_validator,
        baseline_compliant=baseline_compliant,
        baseline_validation_errors=baseline_validation_errors,
        baseline_validation_warnings=baseline_validation_warnings,
        validator=validator,
        compliant=compliant,
        validation_errors=validation_errors,
        validation_warnings=validation_warnings,
        validation_errors_reduced=validation_errors_reduced,
        validation_warnings_reduced=validation_warnings_reduced,
        font_lane_attempted=font_lane_attempted,
        font_lane_applied=font_lane_applied,
        font_lane_first_errors=font_lane_first_errors,
        font_lane_first_warnings=font_lane_first_warnings,
        font_lane_second_errors=font_lane_second_errors,
        font_lane_second_warnings=font_lane_second_warnings,
        elements_total=elements_total,
        elements_headings=elements_headings,
        elements_figures=elements_figures,
        elements_tables=elements_tables,
        elements_list_items=elements_list_items,
        tags_total=tags_total,
        struct_elems_created=struct_elems_created,
        headings_tagged=headings_tagged,
        figures_tagged=figures_tagged,
        decorative_figures_artifacted=decorative_figures_artifacted,
        tables_tagged=tables_tagged,
        lists_tagged=lists_tagged,
        links_tagged=links_tagged,
        bookmarks_added=bookmarks_added,
        heading_coverage=round(_safe_ratio(headings_tagged, elements_headings), 3),
        figure_coverage=round(_safe_ratio(figures_tagged, elements_figures), 3),
        table_coverage=round(_safe_ratio(tables_tagged, elements_tables), 3),
        list_coverage=round(_safe_ratio(lists_tagged, elements_list_items), 3),
        link_coverage=round(_safe_ratio(links_tagged, source_links), 3),
    )


def write_outputs(output_dir: Path, rows: list[DocMetrics]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "corpus_summary.csv"
    json_path = output_dir / "corpus_summary.json"
    md_path = output_dir / "corpus_report.md"

    dict_rows = [asdict(r) for r in rows]
    fields = list(dict_rows[0].keys()) if dict_rows else []

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(dict_rows)

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(dict_rows, f, indent=2)

    total = len(rows)
    completed = [r for r in rows if r.error == ""]
    failed = [r for r in rows if r.error != ""]
    compliant = [r for r in completed if r.compliant]
    font_attempted = [r for r in completed if r.font_lane_attempted]
    font_applied = [r for r in completed if r.font_lane_applied]
    font_improved = [
        r
        for r in completed
        if r.font_lane_attempted
        and (
            r.font_lane_second_errors < r.font_lane_first_errors
            or (
                r.font_lane_second_errors == r.font_lane_first_errors
                and r.font_lane_second_warnings < r.font_lane_first_warnings
            )
        )
    ]
    link_gaps = sorted(
        [r for r in completed if r.link_annots_in_source > 0 and r.link_coverage < 1.0],
        key=lambda r: r.link_coverage,
    )
    heading_gaps = sorted(
        [r for r in completed if r.elements_headings > 0 and r.heading_coverage < 1.0],
        key=lambda r: r.heading_coverage,
    )

    with md_path.open("w", encoding="utf-8") as f:
        f.write("# Corpus Accessibility Benchmark Report\n\n")
        f.write(f"Generated: {datetime.now().isoformat()}\n\n")
        f.write("## Summary\n\n")
        baseline_errors = sum(r.baseline_validation_errors for r in completed)
        baseline_warnings = sum(r.baseline_validation_warnings for r in completed)
        remediated_errors = sum(r.validation_errors for r in completed)
        remediated_warnings = sum(r.validation_warnings for r in completed)
        error_delta = baseline_errors - remediated_errors
        warning_delta = baseline_warnings - remediated_warnings
        f.write(f"- PDFs processed: {total}\n")
        f.write(f"- Successful runs: {len(completed)}\n")
        f.write(f"- Failed runs: {len(failed)}\n")
        f.write(f"- Compliant outputs: {len(compliant)} / {len(completed) if completed else 0}\n")
        f.write(
            f"- Validation errors before/after: {baseline_errors} -> {remediated_errors} "
            f"(delta {error_delta:+d})\n"
        )
        f.write(
            f"- Validation warnings before/after: {baseline_warnings} -> {remediated_warnings} "
            f"(delta {warning_delta:+d})\n"
        )
        f.write(
            f"- Font lane attempted/applied: {len(font_attempted)} / {len(font_applied)} "
            f"(improved {len(font_improved)})\n"
        )
        f.write("\n## Top Link Coverage Gaps\n\n")
        if link_gaps:
            for row in link_gaps[:10]:
                f.write(
                    f"- {Path(row.source_path).name}: "
                    f"{row.links_tagged}/{row.link_annots_in_source} ({row.link_coverage:.3f})\n"
                )
        else:
            f.write("- None\n")
        f.write("\n## Top Heading Coverage Gaps\n\n")
        if heading_gaps:
            for row in heading_gaps[:10]:
                f.write(
                    f"- {Path(row.source_path).name}: "
                    f"{row.headings_tagged}/{row.elements_headings} ({row.heading_coverage:.3f})\n"
                )
        else:
            f.write("- None\n")
        f.write("\n## Failed Files\n\n")
        if failed:
            for row in failed:
                f.write(f"- {row.source_path}: {row.error}\n")
        else:
            f.write("- None\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exclude-wac",
        action="store_true",
        help="Skip backend/test_wac.pdf from discovery.",
    )
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_ROOT / f"corpus_{ts}"
    run_dir = out_dir / "tagged_outputs"
    run_dir.mkdir(parents=True, exist_ok=True)

    pdfs = discover_pdfs(exclude_wac=args.exclude_wac)
    print(f"Discovered {len(pdfs)} PDFs")
    if not pdfs:
        return

    rows: list[DocMetrics] = []
    settings = get_settings()
    for idx, pdf in enumerate(pdfs, start=1):
        print(f"[{idx}/{len(pdfs)}] {pdf}")
        doc_dir = run_dir / f"{idx:03d}"
        doc_dir.mkdir(parents=True, exist_ok=True)
        row = await benchmark_one(pdf, doc_dir, settings)
        rows.append(row)
        status = "OK" if row.error == "" else "FAIL"
        print(
            f"  -> {status} | compliant={row.compliant} | "
            f"h={row.headings_tagged}/{row.elements_headings} "
            f"l={row.links_tagged}/{row.link_annots_in_source} "
            f"time={row.total_secs:.2f}s"
        )

    write_outputs(out_dir, rows)
    print(f"\nWrote benchmark outputs to: {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
