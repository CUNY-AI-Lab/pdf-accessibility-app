#!/usr/bin/env python3
"""Run gold-to-stripped round-trip benchmarks across a manifest corpus."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.pipeline.structure import extract_structure
from app.services.job_manager import JobManager
from app.services.roundtrip_compare import (
    compare_roundtrip_pdfs,
    load_roundtrip_manifest,
    render_roundtrip_markdown,
)
from scripts.corpus_benchmark import (
    BENCHMARK_PROFILE_ASSISTIVE_CORE,
    BENCHMARK_PROFILE_CHOICES,
    OUTPUT_ROOT,
    _init_workflow_db,
    benchmark_runner_for_profile,
)
from scripts.strip_accessibility import strip_accessibility

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_ROOT = ROOT_DIR / "benchmark_manifests" / "roundtrip_corpus"
STRUCTURE_CACHE_ROOT = ROOT_DIR / "data" / "benchmark_structure_cache"


@dataclass
class RoundtripCorpusRow:
    case_id: str
    document_family: str
    product_priority: str
    workflow_tags: str
    manifest_path: str
    gold_pdf: str
    stripped_pdf: str
    candidate_pdf: str
    workflow_final_status: str
    workflow_compliant: bool
    workflow_fidelity_passed: bool
    recoverable_total: int
    recoverable_passed: int
    recoverable_failed: int
    recoverable_invalid: int
    hidden_total: int
    hidden_passed: int
    hidden_failed: int
    hidden_invalid: int
    raw_text_similarity: float | None
    structure_transcript_similarity: float | None
    reading_order_hit_rate: float | None
    reading_order_order_rate: float | None
    field_match_rate: float | None
    link_match_rate: float | None
    bookmark_match_rate: float | None
    recoverable_bookmark_match_rate: float | None
    higher_order_bookmark_match_rate: float | None
    page_count_match: bool
    document_lang_match: bool
    title_match: bool
    error: str


UNIVERSAL_INVARIANTS: tuple[tuple[str, Callable[[RoundtripCorpusRow], bool]], ...] = (
    ("page_count_preserved", lambda row: row.page_count_match),
    (
        "raw_text_preserved",
        lambda row: row.raw_text_similarity is not None and row.raw_text_similarity >= 0.99,
    ),
    (
        "reading_order_preserved",
        lambda row: row.reading_order_hit_rate is not None
        and row.reading_order_hit_rate >= 0.99
        and row.reading_order_order_rate is not None
        and row.reading_order_order_rate >= 0.99,
    ),
    ("document_language_restored", lambda row: row.document_lang_match),
    ("title_restored", lambda row: row.title_match),
)


def resolve_workflow_runner(profile: str):
    return benchmark_runner_for_profile(profile)


def discover_manifests(*, roots: list[Path], explicit: list[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for path in explicit:
        resolved = path.expanduser().resolve()
        if resolved.is_file():
            found[str(resolved)] = resolved

    for root in roots:
        resolved_root = root.expanduser().resolve()
        if not resolved_root.exists():
            continue
        if resolved_root.is_file():
            found[str(resolved_root)] = resolved_root
            continue
        for path in resolved_root.rglob("*.roundtrip.json"):
            found[str(path.resolve())] = path.resolve()

    return sorted(found.values())


def _resolve_manifest_path(manifest_path: Path, raw_path: str) -> Path:
    path = Path(str(raw_path)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (manifest_path.parent / path).resolve()


def _load_structure_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else None


async def _load_or_extract_gold_structure(
    *,
    case_id: str,
    gold_pdf: Path,
) -> dict[str, Any]:
    cache_dir = STRUCTURE_CACHE_ROOT / case_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{gold_pdf.stem}.structure.json"
    cached = _load_structure_json(cache_path)
    if cached is not None:
        return cached

    extract_dir = cache_dir / f"{gold_pdf.stem}_extract"
    result = await extract_structure(
        gold_pdf,
        extract_dir,
        include_figure_images=False,
    )
    cache_path.write_text(json.dumps(result.document_json, indent=2, sort_keys=True) + "\n")
    return result.document_json


def _row_from_report(
    *,
    case_id: str,
    manifest: dict[str, Any],
    manifest_path: Path,
    gold_pdf: Path,
    stripped_pdf: Path,
    candidate_pdf: Path,
    workflow_metrics,
    report: dict[str, Any],
    error: str = "",
) -> RoundtripCorpusRow:
    recoverable = report.get("assertions", {}).get("recoverable", {})
    hidden = report.get("assertions", {}).get("hidden_semantics", {})
    structure = report.get("comparisons", {}).get("structure", {})
    fields = report.get("comparisons", {}).get("fields", {})
    links = report.get("comparisons", {}).get("links", {})
    bookmarks = report.get("comparisons", {}).get("bookmarks", {})
    metadata = report.get("metadata", {})
    reading_order = structure.get("reading_order", {})
    workflow_tags = manifest.get("workflow_tags") if isinstance(manifest, dict) else []
    if not isinstance(workflow_tags, list):
        workflow_tags = []
    return RoundtripCorpusRow(
        case_id=case_id,
        document_family=str(manifest.get("document_family") or "unspecified"),
        product_priority=str(manifest.get("product_priority") or "unspecified"),
        workflow_tags=",".join(str(tag).strip() for tag in workflow_tags if str(tag).strip()),
        manifest_path=str(manifest_path),
        gold_pdf=str(gold_pdf),
        stripped_pdf=str(stripped_pdf),
        candidate_pdf=str(candidate_pdf),
        workflow_final_status=str(getattr(workflow_metrics, "final_status", "unknown")),
        workflow_compliant=bool(getattr(workflow_metrics, "compliant", False)),
        workflow_fidelity_passed=bool(getattr(workflow_metrics, "fidelity_passed", False)),
        recoverable_total=int(recoverable.get("total", 0) or 0),
        recoverable_passed=int(recoverable.get("passed", 0) or 0),
        recoverable_failed=int(recoverable.get("failed", 0) or 0),
        recoverable_invalid=int(recoverable.get("invalid", 0) or 0),
        hidden_total=int(hidden.get("total", 0) or 0),
        hidden_passed=int(hidden.get("passed", 0) or 0),
        hidden_failed=int(hidden.get("failed", 0) or 0),
        hidden_invalid=int(hidden.get("invalid", 0) or 0),
        raw_text_similarity=metadata.get("raw_text_similarity"),
        structure_transcript_similarity=structure.get("transcript_similarity"),
        reading_order_hit_rate=reading_order.get("hit_rate"),
        reading_order_order_rate=reading_order.get("order_rate"),
        field_match_rate=fields.get("named_field_match_rate"),
        link_match_rate=links.get("descriptive_link_match_rate"),
        bookmark_match_rate=bookmarks.get("bookmark_match_rate"),
        recoverable_bookmark_match_rate=bookmarks.get("recoverable_bookmark_match_rate"),
        higher_order_bookmark_match_rate=bookmarks.get("higher_order_bookmark_match_rate"),
        page_count_match=bool(metadata.get("page_count_match", False)),
        document_lang_match=bool(metadata.get("document_lang_match", False)),
        title_match=bool(metadata.get("title_match", False)),
        error=error,
    )


def _row_with_error(
    *,
    case_id: str,
    manifest: dict[str, Any],
    manifest_path: Path,
    gold_pdf: Path,
    stripped_pdf: Path,
    candidate_pdf: Path,
    workflow_metrics,
    error: str,
) -> RoundtripCorpusRow:
    workflow_tags = manifest.get("workflow_tags") if isinstance(manifest, dict) else []
    if not isinstance(workflow_tags, list):
        workflow_tags = []
    return RoundtripCorpusRow(
        case_id=case_id,
        document_family=str(manifest.get("document_family") or "unspecified"),
        product_priority=str(manifest.get("product_priority") or "unspecified"),
        workflow_tags=",".join(str(tag).strip() for tag in workflow_tags if str(tag).strip()),
        manifest_path=str(manifest_path),
        gold_pdf=str(gold_pdf),
        stripped_pdf=str(stripped_pdf),
        candidate_pdf=str(candidate_pdf),
        workflow_final_status=str(getattr(workflow_metrics, "final_status", "unknown")),
        workflow_compliant=bool(getattr(workflow_metrics, "compliant", False)),
        workflow_fidelity_passed=bool(getattr(workflow_metrics, "fidelity_passed", False)),
        recoverable_total=0,
        recoverable_passed=0,
        recoverable_failed=0,
        recoverable_invalid=0,
        hidden_total=0,
        hidden_passed=0,
        hidden_failed=0,
        hidden_invalid=0,
        raw_text_similarity=None,
        structure_transcript_similarity=None,
        reading_order_hit_rate=None,
        reading_order_order_rate=None,
        field_match_rate=None,
        link_match_rate=None,
        bookmark_match_rate=None,
        recoverable_bookmark_match_rate=None,
        higher_order_bookmark_match_rate=None,
        page_count_match=False,
        document_lang_match=False,
        title_match=False,
        error=error,
    )


def _mean(values: list[float | None]) -> float | None:
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return round(sum(usable) / len(usable), 4)


def _group_rows(rows: list[RoundtripCorpusRow], attr: str) -> dict[str, list[RoundtripCorpusRow]]:
    groups: dict[str, list[RoundtripCorpusRow]] = defaultdict(list)
    for row in rows:
        groups[str(getattr(row, attr) or "unspecified")].append(row)
    return dict(sorted(groups.items()))


def _invariant_summary(rows: list[RoundtripCorpusRow]) -> list[tuple[str, int, int]]:
    summary: list[tuple[str, int, int]] = []
    for label, predicate in UNIVERSAL_INVARIANTS:
        passed = sum(1 for row in rows if predicate(row))
        summary.append((label, passed, len(rows)))
    return summary


def write_outputs(out_dir: Path, rows: list[RoundtripCorpusRow]) -> None:
    csv_path = out_dir / "roundtrip_corpus_summary.csv"
    json_path = out_dir / "roundtrip_corpus_summary.json"
    md_path = out_dir / "roundtrip_corpus_report.md"

    fieldnames = list(RoundtripCorpusRow.__dataclass_fields__.keys())
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))

    json_path.write_text(json.dumps([asdict(row) for row in rows], indent=2, sort_keys=True) + "\n")

    completed = [row for row in rows if not row.error]
    failed = [row for row in rows if row.error]
    recoverable_ready = [
        row
        for row in completed
        if row.recoverable_total > 0 and row.recoverable_failed == 0 and row.recoverable_invalid == 0
    ]
    family_groups = _group_rows(rows, "document_family")
    priority_groups = _group_rows(rows, "product_priority")
    primary_rows = priority_groups.get("primary", [])

    with md_path.open("w") as fh:
        fh.write("# Round-Trip Corpus Report\n\n")
        fh.write(f"- Cases: {len(rows)}\n")
        fh.write(f"- Successful comparisons: {len(completed)}\n")
        fh.write(f"- Failed cases: {len(failed)}\n")
        fh.write(f"- Recoverable-clean cases: {len(recoverable_ready)}\n\n")

        fh.write("## Universal Invariants\n\n")
        if rows:
            fh.write("Across all cases:\n")
            for label, passed, total in _invariant_summary(rows):
                fh.write(f"- {label}: {passed}/{total}\n")
            if primary_rows:
                fh.write("\nAcross primary-product cases:\n")
                for label, passed, total in _invariant_summary(primary_rows):
                    fh.write(f"- {label}: {passed}/{total}\n")
            fh.write("\n")
        else:
            fh.write("- None\n\n")

        fh.write("## Family Coverage\n\n")
        if family_groups:
            for family, group_rows in family_groups.items():
                clean = sum(
                    1
                    for row in group_rows
                    if row.recoverable_total > 0
                    and row.recoverable_failed == 0
                    and row.recoverable_invalid == 0
                    and not row.error
                )
                fh.write(
                    f"- {family}: cases={len(group_rows)}, "
                    f"recoverable_clean={clean}/{len(group_rows)}, "
                    f"avg_text={_mean([row.raw_text_similarity for row in group_rows])}, "
                    f"avg_reading_order={_mean([row.reading_order_hit_rate for row in group_rows])}, "
                    f"avg_bookmark_match={_mean([row.bookmark_match_rate for row in group_rows])}, "
                    f"avg_recoverable_bookmark_match={_mean([row.recoverable_bookmark_match_rate for row in group_rows])}, "
                    f"avg_higher_order_bookmark_match={_mean([row.higher_order_bookmark_match_rate for row in group_rows])}\n"
                )
            fh.write("\n")
        else:
            fh.write("- None\n\n")

        fh.write("## Priority Coverage\n\n")
        if priority_groups:
            for priority, group_rows in priority_groups.items():
                fh.write(
                    f"- {priority}: cases={len(group_rows)}, "
                    f"recoverable_clean="
                    f"{sum(1 for row in group_rows if row.recoverable_total > 0 and row.recoverable_failed == 0 and row.recoverable_invalid == 0 and not row.error)}/{len(group_rows)}\n"
                )
            fh.write("\n")
        else:
            fh.write("- None\n\n")

        fh.write("## Case Summary\n\n")
        if rows:
            for row in rows:
                fh.write(
                    f"- {row.case_id} [{row.document_family}/{row.product_priority}]: "
                    f"status={row.workflow_final_status}, "
                    f"recoverable={row.recoverable_passed}/{row.recoverable_total}, "
                    f"text={row.raw_text_similarity}, "
                    f"reading_order={row.reading_order_hit_rate}/{row.reading_order_order_rate}, "
                    f"bookmarks={row.bookmark_match_rate}, "
                    f"recoverable_bookmarks={row.recoverable_bookmark_match_rate}"
                )
                if row.error:
                    fh.write(f", error={row.error}")
                fh.write("\n")
        else:
            fh.write("- None\n")

        fh.write("\n## Failures\n\n")
        if failed:
            for row in failed:
                fh.write(f"- {row.case_id}: {row.error}\n")
        else:
            fh.write("- None\n")


async def run_roundtrip_case(
    *,
    manifest_path: Path,
    case_dir: Path,
    settings,
    session_maker,
    job_manager: JobManager,
    workflow_runner: Callable[..., Awaitable[Any]] | None = None,
) -> RoundtripCorpusRow:
    runner = workflow_runner or resolve_workflow_runner(BENCHMARK_PROFILE_ASSISTIVE_CORE)
    manifest = load_roundtrip_manifest(manifest_path)
    case_id = str(manifest.get("id") or manifest_path.stem).strip() or manifest_path.stem
    gold_pdf = _resolve_manifest_path(manifest_path, str(manifest["gold_pdf"]))
    if not gold_pdf.exists():
        raise FileNotFoundError(f"Gold PDF not found for {case_id}: {gold_pdf}")

    stripped_pdf_raw = manifest.get("stripped_pdf")
    case_dir.mkdir(parents=True, exist_ok=True)
    if stripped_pdf_raw:
        stripped_pdf = _resolve_manifest_path(manifest_path, str(stripped_pdf_raw))
    else:
        stripped_pdf = case_dir / f"{gold_pdf.stem}_stripped.pdf"
        strip_report = strip_accessibility(input_path=gold_pdf, output_path=stripped_pdf)
        (case_dir / "strip_summary.json").write_text(
            json.dumps(asdict(strip_report), indent=2, sort_keys=True) + "\n"
        )

    workflow_dir = case_dir / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    workflow_metrics = await runner(
        stripped_pdf,
        workflow_dir,
        settings,
        session_maker,
        job_manager,
    )
    print(
        f"  -> workflow status={getattr(workflow_metrics, 'final_status', 'unknown')} "
        f"compliant={getattr(workflow_metrics, 'compliant', False)} "
        f"fidelity={getattr(workflow_metrics, 'fidelity_passed', False)}"
    )
    candidate_pdf = workflow_dir / f"{stripped_pdf.stem}_workflow_output.pdf"
    if not candidate_pdf.exists():
        return _row_with_error(
            case_id=case_id,
            manifest=manifest,
            manifest_path=manifest_path,
            gold_pdf=gold_pdf,
            stripped_pdf=stripped_pdf,
            candidate_pdf=candidate_pdf,
            workflow_metrics=workflow_metrics,
            error=(
                str(getattr(workflow_metrics, "error", "")).strip()
                or "Workflow output PDF was not produced."
            ),
        )

    candidate_structure_json = _load_structure_json(
        workflow_dir / f"{stripped_pdf.stem}_workflow_structure.json"
    )
    gold_structure_json = await _load_or_extract_gold_structure(
        case_id=case_id,
        gold_pdf=gold_pdf,
    )

    compare_work_dir = case_dir / "compare_work"
    print("  -> comparing against gold")
    report = await compare_roundtrip_pdfs(
        gold_pdf=gold_pdf,
        candidate_pdf=candidate_pdf,
        manifest=manifest,
        work_dir=compare_work_dir,
        gold_structure_json=gold_structure_json,
        candidate_structure_json=candidate_structure_json,
    )
    report_json_path = case_dir / "roundtrip_compare.json"
    report_md_path = case_dir / "roundtrip_compare.md"
    report_json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report_md_path.write_text(render_roundtrip_markdown(report))

    return _row_from_report(
        case_id=case_id,
        manifest=manifest,
        manifest_path=manifest_path,
        gold_pdf=gold_pdf,
        stripped_pdf=stripped_pdf,
        candidate_pdf=candidate_pdf,
        workflow_metrics=workflow_metrics,
        report=report,
        error=str(getattr(workflow_metrics, "error", "")).strip(),
    )


async def run_roundtrip_case_isolated(
    *,
    manifest_path: Path,
    case_dir: Path,
    settings,
    workflow_runner: Callable[..., Awaitable[Any]] | None = None,
) -> RoundtripCorpusRow:
    case_dir.mkdir(parents=True, exist_ok=True)
    workflow_db = case_dir / "workflow_benchmark.sqlite3"
    workflow_engine, session_maker = await _init_workflow_db(workflow_db)
    job_manager = JobManager()
    try:
        return await run_roundtrip_case(
            manifest_path=manifest_path,
            case_dir=case_dir,
            settings=settings,
            session_maker=session_maker,
            job_manager=job_manager,
            workflow_runner=workflow_runner,
        )
    finally:
        await workflow_engine.dispose()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow-profile",
        choices=BENCHMARK_PROFILE_CHOICES,
        default=BENCHMARK_PROFILE_ASSISTIVE_CORE,
        help=(
            "Benchmark execution profile for the remediation pass. "
            "`assistive-core` is the default and skips only figure alt-text generation. "
            "Use `full` to include that branch as well."
        ),
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help=(
            "Manifest discovery root. May be passed multiple times. "
            "Defaults to backend/benchmark_manifests/roundtrip_corpus."
        ),
    )
    parser.add_argument(
        "--manifest",
        action="append",
        default=[],
        help="Explicit manifest file. May be passed multiple times.",
    )
    parser.add_argument(
        "--out-dir",
        help="Optional output directory. Defaults to a timestamped directory under data/benchmarks.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="Number of manifests to process concurrently. Default: 4.",
    )
    args = parser.parse_args()

    explicit_manifests = [Path(path) for path in args.manifest]
    if explicit_manifests:
        manifest_roots = [Path(root) for root in args.root]
    else:
        manifest_roots = [Path(root) for root in args.root] or [DEFAULT_MANIFEST_ROOT]
    manifests = discover_manifests(roots=manifest_roots, explicit=explicit_manifests)
    if not manifests:
        raise SystemExit("No round-trip manifests found.")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = (
        Path(args.out_dir).expanduser().resolve()
        if args.out_dir
        else OUTPUT_ROOT / f"roundtrip_corpus_{ts}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    workflow_runner = resolve_workflow_runner(args.workflow_profile)
    max_jobs = max(1, int(args.jobs or 1))

    rows: list[RoundtripCorpusRow] = []
    if max_jobs == 1:
        workflow_db = out_dir / "workflow_benchmark.sqlite3"
        workflow_engine, session_maker = await _init_workflow_db(workflow_db)
        job_manager = JobManager()
        try:
            for index, manifest_path in enumerate(manifests, start=1):
                print(f"[{index}/{len(manifests)}] {manifest_path}")
                case_data = load_roundtrip_manifest(manifest_path)
                case_id = str(case_data.get("id") or manifest_path.stem).strip() or manifest_path.stem
                case_dir = out_dir / case_id
                row = await run_roundtrip_case(
                    manifest_path=manifest_path,
                    case_dir=case_dir,
                    settings=settings,
                    session_maker=session_maker,
                    job_manager=job_manager,
                    workflow_runner=workflow_runner,
                )
                rows.append(row)
                print(
                    f"  -> status={row.workflow_final_status} "
                    f"recoverable={row.recoverable_passed}/{row.recoverable_total} "
                    f"text={row.raw_text_similarity} "
                    f"reading_order={row.reading_order_hit_rate}"
                    + (f" error={row.error}" if row.error else "")
                )
        finally:
            await workflow_engine.dispose()
    else:
        semaphore = asyncio.Semaphore(max_jobs)

        async def _run_one(index: int, manifest_path: Path):
            async with semaphore:
                print(f"[{index}/{len(manifests)}] {manifest_path}")
                case_data = load_roundtrip_manifest(manifest_path)
                case_id = str(case_data.get("id") or manifest_path.stem).strip() or manifest_path.stem
                case_dir = out_dir / case_id
                row = await run_roundtrip_case_isolated(
                    manifest_path=manifest_path,
                    case_dir=case_dir,
                    settings=settings,
                    workflow_runner=workflow_runner,
                )
                print(
                    f"  -> status={row.workflow_final_status} "
                    f"recoverable={row.recoverable_passed}/{row.recoverable_total} "
                    f"text={row.raw_text_similarity} "
                    f"reading_order={row.reading_order_hit_rate}"
                    + (f" error={row.error}" if row.error else "")
                )
                return index, row

        results = await asyncio.gather(
            *[_run_one(index, manifest_path) for index, manifest_path in enumerate(manifests, start=1)]
        )
        rows = [row for _, row in sorted(results, key=lambda item: item[0])]

    write_outputs(out_dir, rows)
    print(f"\nWrote round-trip corpus outputs to: {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
