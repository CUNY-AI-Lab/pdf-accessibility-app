"""Remediate benchmark PDFs with the full pipeline.

Each PDF in ``<bench_dir>/pdfs/<subset>/`` goes through the production
``run_pipeline`` (classification, OCR, Docling structure, tagging,
validation, fidelity). The tagged result is saved as
``<bench_dir>/<candidate>/<subset>/<stem>.tagged.pdf``; a PDF the pipeline
produced nothing for gets ``<stem>.failed`` holding the job's status. Either
file marks the PDF done, so an interrupted run resumes where it stopped;
delete ``.failed`` files to retry them. The scorers read the tagged PDFs.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import tempfile
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.jobs import PIPELINE_STEPS
from app.config import get_settings
from app.models import Base, Job, JobStep
from app.pipeline.orchestrator import run_pipeline
from app.services.job_manager import JobManager

OWNER = "0" * 64


async def remediate(pdf: Path, session_maker, settings, job_manager) -> tuple[Path | None, str]:
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
    await run_pipeline(job_id, session_maker, settings, job_manager)
    async with session_maker() as db:
        job = await db.get(Job, job_id)
        output = Path(job.output_path) if job and job.output_path else None
        return output, f"{job.status if job else 'missing'} {job.classification if job else ''}"


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
                output, status = await remediate(pdf, session_maker, settings, job_manager)
                if output is not None and output.exists():
                    shutil.copy(output, tagged)
                else:
                    failed.write_text(status + "\n", encoding="utf-8")
                print(f"{subset}/{pdf.name}: {status}", flush=True)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
