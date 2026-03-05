import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AltTextEntry, Job
from app.schemas import AltTextResponse, AltTextUpdateRequest

router = APIRouter(prefix="/jobs/{job_id}", tags=["documents"])


@router.get("/structure")
async def get_structure(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.structure_json:
        raise HTTPException(status_code=404, detail="Structure not yet extracted")
    return json.loads(job.structure_json)


@router.get("/alt-texts", response_model=list[AltTextResponse])
async def list_alt_texts(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(AltTextEntry)
        .where(AltTextEntry.job_id == job_id)
        .order_by(AltTextEntry.figure_index)
    )
    entries = result.scalars().all()
    return [
        AltTextResponse(
            id=e.id,
            figure_index=e.figure_index,
            image_url=f"/api/jobs/{job_id}/figures/{e.figure_index}/image",
            generated_text=e.generated_text,
            edited_text=e.edited_text,
            status=e.status,
        )
        for e in entries
    ]


@router.put("/alt-texts/{figure_index}", response_model=AltTextResponse)
async def update_alt_text(
    job_id: str,
    figure_index: int,
    update: AltTextUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AltTextEntry).where(
            AltTextEntry.job_id == job_id, AltTextEntry.figure_index == figure_index
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Alt text entry not found")

    if update.edited_text is not None:
        entry.edited_text = update.edited_text
    if update.status is not None:
        entry.status = update.status

    await db.commit()
    await db.refresh(entry)

    return AltTextResponse(
        id=entry.id,
        figure_index=entry.figure_index,
        image_url=f"/api/jobs/{job_id}/figures/{entry.figure_index}/image",
        generated_text=entry.generated_text,
        edited_text=entry.edited_text,
        status=entry.status,
    )


@router.get("/figures/{figure_index}/image")
async def get_figure_image(
    job_id: str, figure_index: int, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(AltTextEntry).where(
            AltTextEntry.job_id == job_id, AltTextEntry.figure_index == figure_index
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Figure not found")

    image_path = Path(entry.image_path)
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Image file not found")

    return FileResponse(image_path, media_type="image/png")


@router.get("/download")
async def download_pdf(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.output_path:
        raise HTTPException(status_code=404, detail="Accessible PDF not yet available")

    output_path = Path(job.output_path)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Output file not found")

    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename=f"accessible_{job.original_filename}",
    )


@router.get("/download/report")
async def download_report(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.validation_json:
        raise HTTPException(status_code=404, detail="Validation report not yet available")

    return json.loads(job.validation_json)
