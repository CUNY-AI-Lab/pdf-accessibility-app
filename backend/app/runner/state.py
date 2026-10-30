"""A job's working files on the runner, and the archive the Worker keeps.

The pipeline records absolute paths (input, figures, output) in the job's
database, so the runner keeps one layout under the settings' data root and
the archive stores paths relative to it. Unpacking on any runner with the
same layout restores a job exactly. Job files are removed after each task.

A shared cache keeps generated figure descriptions keyed by hashes of the
image and exact model request, model ID, and prompt version. It stores no
source document text, captions, images, or job identifiers. It lives at
processing_dir.parent / "alt_text_cache.sqlite3" (/app/data in production),
outside job archives. Entries expire 48 hours after storage; reads do not
extend their lifetime. Expired rows are deleted on open, get, and put.
The cache holds at most 2,000 descriptions of at most 4,096 UTF-8 bytes each;
the SQLite file is capped at 16 MiB, with LRU eviction and full auto-vacuum.
ALT_TEXT_CACHE_ENABLED=false disables all cache file creation, reads, and
writes. It defaults to true. Disabling it leaves any existing file untouched.
"""

import shutil
import tarfile
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.database import prepare_schema


def data_root(settings: Settings) -> Path:
    roots = {d.resolve().parent for d in (settings.upload_dir, settings.processing_dir, settings.output_dir)}
    if len(roots) != 1:
        raise RuntimeError(
            "The runner needs UPLOAD_DIR, PROCESSING_DIR and OUTPUT_DIR in one directory"
        )
    return roots.pop()


def input_path(settings: Settings, job_id: str) -> Path:
    return settings.upload_dir.resolve() / f"{job_id}.pdf"


def database_path(settings: Settings, job_id: str) -> Path:
    return data_root(settings) / "runner" / f"{job_id}.db"


def job_paths(settings: Settings, job_id: str) -> list[Path]:
    return [
        input_path(settings, job_id),
        settings.processing_dir.resolve() / job_id,
        settings.output_dir.resolve() / job_id,
        database_path(settings, job_id),
    ]


def remove_job_files(settings: Settings, job_id: str) -> None:
    for path in job_paths(settings, job_id):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


async def open_job_database(
    settings: Settings, job_id: str
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    path = database_path(settings, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    await prepare_schema(engine)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def pack_state(settings: Settings, job_id: str, archive: Path) -> Path:
    root = data_root(settings)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        for path in job_paths(settings, job_id):
            if path.exists():
                tar.add(path, arcname=str(path.relative_to(root)))
    return archive


def unpack_state(settings: Settings, job_id: str, archive: Path) -> None:
    root = data_root(settings)
    allowed = [str(path.relative_to(root)) for path in job_paths(settings, job_id)]
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            if not any(member.name == prefix or member.name.startswith(f"{prefix}/") for prefix in allowed):
                raise ValueError(f"unexpected path in job state: {member.name}")
        tar.extractall(root, members=members, filter="data")
