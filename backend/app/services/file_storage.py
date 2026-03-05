import shutil
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import get_settings


def ensure_dirs():
    settings = get_settings()
    for d in (settings.upload_dir, settings.processing_dir, settings.output_dir):
        d.mkdir(parents=True, exist_ok=True)


async def save_upload(file: UploadFile) -> tuple[str, Path, int]:
    """Save an uploaded file and return (stored_filename, path, size_bytes)."""
    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(file.filename or "upload.pdf").suffix
    stored_name = f"{uuid.uuid4()}{ext}"
    dest = settings.upload_dir / stored_name

    size = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):  # 1MB chunks
            f.write(chunk)
            size += len(chunk)

    return stored_name, dest, size


def create_job_dir(job_id: str) -> Path:
    settings = get_settings()
    job_dir = settings.processing_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "figures").mkdir(exist_ok=True)
    return job_dir


def get_output_path(job_id: str, filename: str) -> Path:
    settings = get_settings()
    output_dir = settings.output_dir / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / filename


def cleanup_job_files(job_id: str, input_path: str | None = None):
    """Remove all files associated with a job."""
    settings = get_settings()
    # Remove processing directory
    proc_dir = settings.processing_dir / job_id
    if proc_dir.exists():
        shutil.rmtree(proc_dir)
    # Remove output directory
    out_dir = settings.output_dir / job_id
    if out_dir.exists():
        shutil.rmtree(out_dir)
    # Remove uploaded file
    if input_path:
        p = Path(input_path)
        if p.exists():
            p.unlink()
