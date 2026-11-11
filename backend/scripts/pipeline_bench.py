"""Remediate benchmark PDFs with the full pipeline.

Each PDF in ``<bench_dir>/pdfs/<subset>/`` goes through the production
``run_pipeline`` (classification, OCR, Docling structure, tagging,
validation, fidelity). The tagged result is saved as
``<bench_dir>/<candidate>/<subset>/<stem>.tagged.pdf``; a PDF the pipeline
produced nothing for gets ``<stem>.failed`` holding the job's status and
error. Either file marks the PDF done, so an interrupted run resumes where it
stopped; delete ``.failed`` files to retry them. The scorers read the tagged
PDFs.

What the tagger was given for the tagged result is kept beside it in
``<stem>.tag-inputs/`` (the PDF it tagged, the structure, the alt text, and
its other arguments), so ``retag_bench.py`` can tag every page again with
other tagger code in about a minute, without OCR and Docling. Where the
pipeline rewrote the tagged PDF afterwards (its font repairs), the inputs of
its last tagger call are kept, marked ``rewritten-after-tagging``. Each
capture holds ``capture.json``: what produced it (commit, image, docling-serve
and Docling versions and device, OCR engine and versions, date).

Beside each page, ``<stem>.run.json`` records the run: each pipeline step's
status and seconds, docling-serve cache use, model requests, tokens and cost,
and its outcome (the release gate's checks, veraPDF's remaining rules, the
review tasks a person sees, and each figure's alt text). ``--docling-cache``
answers a conversion done before from disk, and ``--cassette`` records or
replays model calls (``bench_caches.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import bench_caches
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.pipeline.orchestrator as orchestrator
from app.config import get_settings
from app.models import AltTextEntry, Base, Job, JobStep, ReviewTask
from app.pipeline.steps import PIPELINE_STEPS
from app.services.pipeline_progress import NullProgress

BACKEND = Path(__file__).resolve().parent.parent

OWNER = "0" * 64
TAG_PDF = orchestrator.tag_pdf
# Each tag_pdf call of the current job: its output path, and a copy of its
# inputs taken when it was called.
tag_calls: list[tuple[Path, Path]] = []


async def recording_tag_pdf(*args, **kwargs):
    arguments = dict(inspect.signature(TAG_PDF).bind(*args, **kwargs).arguments)
    inputs = Path(tempfile.mkdtemp(prefix="tag-inputs-"))
    shutil.copy(arguments.pop("input_path"), inputs / "input.pdf")
    output_path = Path(arguments.pop("output_path"))
    structure = json.dumps(arguments.pop("structure_json"), default=str)
    (inputs / "structure.json").write_text(structure)
    (inputs / "arguments.json").write_text(json.dumps(arguments, default=str))
    tag_calls.append((output_path, inputs))
    return await TAG_PDF(*args, **kwargs)


orchestrator.tag_pdf = recording_tag_pdf


def command_output(*command: str) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout or result.stderr).strip() or None


def git_commit() -> str | None:
    """The commit of the app code that runs: as the run script names it for
    a container, or else this checkout's."""
    if commit := os.environ.get("BENCH_GIT_COMMIT"):
        return commit
    commit = command_output("git", "-C", str(BACKEND), "rev-parse", "HEAD")
    dirty = command_output("git", "-C", str(BACKEND), "status", "--porcelain", "--", "app")
    return f"{commit}-dirty" if commit and dirty else commit


def capture_metadata(settings) -> dict:
    """What produced this run's captures; the date is set per capture."""
    import ocrmypdf

    docling: dict = {"url": settings.docling_serve_url or None}
    if settings.docling_serve_url:
        headers = (
            {"Authorization": f"Bearer {settings.docling_serve_token}"}
            if settings.docling_serve_token
            else {}
        )
        url = f"{settings.docling_serve_url.rstrip('/')}/version"
        docling["versions"] = httpx.get(url, headers=headers, timeout=30).json()
    else:
        try:
            from importlib.metadata import version

            docling["versions"] = {"docling": version("docling"), "local": True}
        except Exception:  # noqa: BLE001 - recorded as unknown
            docling["versions"] = None
    # docling-serve does not report its device; the run script passes it on.
    docling["device"] = os.environ.get("BENCH_DOCLING_DEVICE", "unknown")
    tesseract = command_output("tesseract", "--version")
    return {
        "git_commit": git_commit(),
        "app_source": os.environ.get("BENCH_APP_SOURCE", "checkout"),
        "image": os.environ.get("BENCH_IMAGE"),
        "image_id": os.environ.get("BENCH_IMAGE_ID"),
        "docling_serve": docling,
        "ocr": {
            "engine": settings.ocr_engine,
            "tesseract": tesseract.splitlines()[0] if tesseract else None,
            "ocrmypdf": ocrmypdf.__version__,
            "ghostscript": command_output(settings.ghostscript_path, "--version"),
        },
        "ai_steps": os.environ.get("BENCH_AI_STEPS", "unknown"),
    }


async def step_ledger(session_maker, job_id: str) -> list[dict]:
    async with session_maker() as db:
        rows = (await db.execute(select(JobStep).where(JobStep.job_id == job_id))).scalars()
        order = {name: index for index, name in enumerate(PIPELINE_STEPS)}
        steps = []
        for step in sorted(rows, key=lambda row: order.get(row.step_name, len(order))):
            seconds = None
            if step.started_at and step.completed_at:
                seconds = round((step.completed_at - step.started_at).total_seconds(), 2)
            entry = {"step": step.step_name, "status": step.status, "seconds": seconds}
            if step.step_name == "tagging":
                result = json.loads(step.result_json) if step.result_json else {}
                entry["pretag_table_intelligence"] = result.get("table_intelligence_audit")
            steps.append(entry)
        return steps


async def run_outcome(session_maker, job_id: str) -> dict:
    """What the run decided: the release gate's checks, veraPDF's verdict and
    remaining rules, the review tasks a person sees, and each figure's alt
    text, as the app stored them."""
    async with session_maker() as db:
        job = await db.get(Job, job_id)
        tasks = (await db.execute(select(ReviewTask).where(ReviewTask.job_id == job_id))).scalars()
        alts = (
            await db.execute(select(AltTextEntry).where(AltTextEntry.job_id == job_id))
        ).scalars()
        validation = json.loads(job.validation_json) if job and job.validation_json else {}
        fidelity = validation.get("fidelity") or {}
        return {
            "table_decisions": (validation.get("tagging") or {}).get("table_decisions", []),
            "table_readability": (validation.get("tagging") or {}).get("table_readability", []),
            "validation_compliant": validation.get("compliant"),
            "remaining_rules": sorted(
                {
                    str(violation.get("rule_id"))
                    for violation in validation.get("violations") or []
                    if violation.get("severity") == "error"
                }
            ),
            "fidelity_passed": fidelity.get("passed"),
            "fidelity_summary": fidelity.get("summary"),
            "checks": [
                {"check": check.get("check"), "status": check.get("status")}
                for check in fidelity.get("checks") or []
            ],
            "review_tasks": [
                {
                    "task_type": task.task_type,
                    "blocking": task.blocking,
                    "severity": task.severity,
                    "source": task.source,
                    "title": task.title,
                }
                for task in tasks
            ],
            "alt_texts": [
                {
                    "figure_index": entry.figure_index,
                    "status": entry.status,
                    "text": entry.edited_text or entry.generated_text,
                }
                for entry in sorted(alts, key=lambda entry: entry.figure_index)
            ],
        }


async def remediate(
    pdf: Path, session_maker, settings, job_manager
) -> tuple[Path | None, str, str, str]:
    job_id = str(uuid.uuid4())
    async with session_maker() as db:
        db.add(
            Job(
                id=job_id,
                filename=pdf.name,
                original_filename=pdf.name,
                owner_session_hash=OWNER,
                status="queued",
                input_path=str(pdf),
                file_size_bytes=pdf.stat().st_size,
            )
        )
        for step in PIPELINE_STEPS:
            db.add(JobStep(job_id=job_id, step_name=step))
        await db.commit()
    await orchestrator.run_pipeline(job_id, session_maker, settings, job_manager)
    async with session_maker() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return None, "missing", "", job_id
        output = Path(job.output_path) if job.output_path else None
        return output, f"{job.status} {job.classification}", job.error or "", job_id


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench-dir", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--subsets", nargs="+", required=True)
    parser.add_argument(
        "--shard", default="0/1", help="i/n: run every n-th PDF starting at the i-th"
    )
    parser.add_argument(
        "--docling-cache", type=Path, help="answer docling-serve conversions done before from here"
    )
    parser.add_argument("--cassette", type=Path, help="record or replay model calls here")
    parser.add_argument("--cassette-mode", choices=bench_caches.CASSETTE_MODES, default="replay")
    options = parser.parse_args()
    shard, shards = (int(part) for part in options.shard.split("/"))

    settings = get_settings()
    job_manager = NullProgress()
    if options.docling_cache:
        bench_caches.install_docling_cache(options.docling_cache)
    bench_caches.install_llm_cassette(options.cassette, options.cassette_mode)
    metadata = capture_metadata(settings)
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp}/bench.sqlite3")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        for subset in options.subsets:
            out_dir = options.bench_dir / options.candidate / subset
            out_dir.mkdir(parents=True, exist_ok=True)
            pdfs = sorted((options.bench_dir / "pdfs" / subset).glob("*.pdf"))
            for pdf in pdfs[shard::shards]:
                tagged = out_dir / f"{pdf.stem}.tagged.pdf"
                failed = out_dir / f"{pdf.stem}.failed"
                if tagged.exists() or failed.exists():
                    continue
                tag_calls.clear()
                ledger = bench_caches.new_page()
                started = time.monotonic()
                output, status, error, job_id = await remediate(
                    pdf, session_maker, settings, job_manager
                )
                run = {
                    "pdf": f"{subset}/{pdf.name}",
                    "status": status,
                    "seconds": round(time.monotonic() - started, 2),
                    "steps": await step_ledger(session_maker, job_id),
                    "outcome": await run_outcome(session_maker, job_id),
                    **ledger.as_dict(),
                }
                (out_dir / f"{pdf.stem}.run.json").write_text(json.dumps(run, indent=1))
                if output is not None and output.exists():
                    shutil.copy(output, tagged)
                    kept = out_dir / f"{pdf.stem}.tag-inputs"
                    shutil.rmtree(kept, ignore_errors=True)
                    final = [inputs for path, inputs in tag_calls if path == output]
                    if final:
                        shutil.move(final[-1], kept)
                    elif tag_calls:
                        shutil.move(tag_calls[-1][1], kept)
                        (kept / "rewritten-after-tagging").touch()
                    if kept.exists():
                        captured = datetime.now(UTC).isoformat(timespec="seconds")
                        capture = {**metadata, "date": captured, "pdf": f"{subset}/{pdf.name}"}
                        (kept / "capture.json").write_text(json.dumps(capture, indent=1))
                else:
                    failed.write_text(f"{status}\n{error}\n", encoding="utf-8")
                for _, inputs in tag_calls:
                    shutil.rmtree(inputs, ignore_errors=True)
                print(f"{subset}/{pdf.name}: {status}", flush=True)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
