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
its last tagger call are kept, marked ``rewritten-after-tagging``.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.pipeline.orchestrator as orchestrator
from app.api.jobs import PIPELINE_STEPS
from app.config import get_settings
from app.models import Base, Job, JobStep
from app.services.job_manager import JobManager

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


async def remediate(
    pdf: Path, session_maker, settings, job_manager
) -> tuple[Path | None, str, str]:
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
            return None, "missing", ""
        output = Path(job.output_path) if job.output_path else None
        return output, f"{job.status} {job.classification}", job.error or ""


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench-dir", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--subsets", nargs="+", required=True)
    parser.add_argument(
        "--shard", default="0/1", help="i/n: run every n-th PDF starting at the i-th"
    )
    options = parser.parse_args()
    shard, shards = (int(part) for part in options.shard.split("/"))

    settings = get_settings()
    job_manager = JobManager()
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
                output, status, error = await remediate(pdf, session_maker, settings, job_manager)
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
                else:
                    failed.write_text(f"{status}\n{error}\n", encoding="utf-8")
                for _, inputs in tag_calls:
                    shutil.rmtree(inputs, ignore_errors=True)
                print(f"{subset}/{pdf.name}: {status}", flush=True)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
