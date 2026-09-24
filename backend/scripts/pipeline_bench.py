"""Produce olmOCR-Bench candidate outputs from the full remediation pipeline.

Each single-page benchmark PDF goes through the production ``run_pipeline``
(classification, OCR, Docling structure, tagging, validation, fidelity). The
tagged output is then read the way a screen reader reads it
(``app/services/structure_text.py``), and that text is written where the olmOCR-Bench
scorer expects it:

    <bench_dir>/<candidate>/<subset>/<pdf stem>_pg1_repeat1.md

The remediated PDFs are kept next to the Markdown for inspection.
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
from app.services.structure_text import screen_reader_text

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
    options = parser.parse_args()

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
            for pdf in sorted((options.bench_dir / "pdfs" / subset).glob("*.pdf")):
                target = out_dir / f"{pdf.stem}_pg1_repeat1.md"
                if target.exists():
                    continue
                output, status = await remediate(pdf, session_maker, settings, job_manager)
                text = ""
                if output is not None and output.exists():
                    shutil.copy(output, out_dir / f"{pdf.stem}.tagged.pdf")
                    text = screen_reader_text(output)
                target.write_text(text, encoding="utf-8")
                print(f"{subset}/{pdf.name}: {status} ({len(text.split())} words)", flush=True)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
