import shutil
from pathlib import Path

from app.config import get_settings
from app.services.path_safety import safe_filename


def ensure_dirs():
    settings = get_settings()
    for d in (settings.upload_dir, settings.processing_dir, settings.output_dir):
        d.mkdir(parents=True, exist_ok=True)


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
    return output_dir / safe_filename(filename)


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
    # Remove uploaded file (validate it's within upload_dir)
    if input_path:
        p = Path(input_path).resolve()
        upload_root = str(settings.upload_dir.resolve()) + "/"
        if not str(p).startswith(upload_root):
            import logging

            logging.getLogger(__name__).warning(
                "Refusing to delete input_path outside upload_dir: %s", input_path
            )
        elif p.exists():
            p.unlink()
