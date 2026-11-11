"""A job's working files on the runner, and the archive the Worker keeps.

The pipeline records absolute paths (input, figures, output) in the job's
database. Each lease has an isolated data root. Archives retain the deployed
runner's relative layout and canonical file references; unpacking rebases
file-reference columns and revision snapshot paths to the receiving attempt.
Job files are removed after owned work, including cancelled threads, has joined.

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

import fcntl
import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import contextmanager
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
    roots = {
        d.resolve().parent
        for d in (settings.upload_dir, settings.processing_dir, settings.output_dir)
    }
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


def pack_state(
    settings: Settings, job_id: str, archive: Path, *, canonical_root: Path | None = None
) -> Path:
    root = data_root(settings)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        for path in job_paths(settings, job_id):
            if path.exists():
                name = str(path.relative_to(root))
                if path == database_path(settings, job_id) and canonical_root is not None:
                    with tempfile.TemporaryDirectory(dir=archive.parent) as temp:
                        snapshot = Path(temp) / "state.db"
                        with sqlite3.connect(path) as source, sqlite3.connect(snapshot) as target:
                            source.backup(target)
                        relocate_database(snapshot, job_id, canonical_root)
                        tar.add(snapshot, arcname=name)
                else:
                    tar.add(path, arcname=name)
    return archive


def unpack_state(settings: Settings, job_id: str, archive: Path) -> None:
    root = data_root(settings)
    allowed = [str(path.relative_to(root)) for path in job_paths(settings, job_id)]
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            if not any(
                member.name == prefix or member.name.startswith(f"{prefix}/") for prefix in allowed
            ):
                raise ValueError(f"unexpected path in job state: {member.name}")
        if any(not (member.isfile() or member.isdir()) for member in members):
            raise ValueError("Saved state must contain only files and directories")
        tar.extractall(root, members=members, filter="data")
    relocate_database(database_path(settings, job_id), job_id, root)


def attempt_settings(settings: Settings, lease: str) -> Settings:
    """Lease identity is unique even when a later review resets its attempt counter."""
    root = data_root(settings) / "runner-attempts" / hashlib.sha256(lease.encode()).hexdigest()
    return settings.model_copy(
        update={
            "upload_dir": root / settings.upload_dir.name,
            "processing_dir": root / settings.processing_dir.name,
            "output_dir": root / settings.output_dir.name,
            "alt_text_cache_dir": settings.alt_text_cache_dir or settings.processing_dir.parent,
        }
    )


_FILE_REFERENCE_COLUMNS = (
    ("jobs", "input_path"),
    ("jobs", "output_path"),
    ("alt_texts", "image_path"),
)
_REVISION_SNAPSHOTS = ("before_json", "edits_json", "published_json")


def _relocate_revision_snapshot(raw: str | None, job_id: str, root: Path) -> str | None:
    """Rebase only file fields; each snapshot may come from an older attempt."""
    if raw is None:
        return raw
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(state, dict) or not isinstance(state.get("jobs"), list):
        return raw
    saved_job = next(
        (row for row in state["jobs"] if isinstance(row, dict) and row.get("id") == job_id), None
    )
    if saved_job is None or not isinstance(saved_job.get("input_path"), str):
        return raw
    old_root = str(Path(saved_job["input_path"]).parent.parent) + "/"
    new_root = str(root) + "/"
    changed = False
    for table, column in _FILE_REFERENCE_COLUMNS:
        records = state.get(table, [])
        if not isinstance(records, list):
            continue
        for row in records:
            identity = "id" if table == "jobs" else "job_id"
            if not isinstance(row, dict) or row.get(identity) != job_id:
                continue
            value = row.get(column)
            if isinstance(value, str) and value.startswith(old_root):
                relocated = new_root + value[len(old_root) :]
                if relocated != value:
                    row[column] = relocated
                    changed = True
    return json.dumps(state) if changed else raw


def relocate_database(path: Path, job_id: str, root: Path) -> None:
    """Rebase live and revision file references, preserving text and digests."""
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT input_path FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Saved state has no job")
        old_root = str(Path(row[0]).parent.parent) + "/"
        new_root = str(root) + "/"
        # Only known file fields are rebased; literal paths in document text,
        # commands, metadata and digest records must remain unchanged.
        for table, column in _FILE_REFERENCE_COLUMNS:
            db.execute(
                f'UPDATE "{table}" SET "{column}" = replace("{column}", ?, ?)',
                (old_root, new_root),
            )
        # Deployed/pre-journal archives do not have this optional table.
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'review_revisions'"
        ).fetchone():
            revisions = db.execute(
                "SELECT revision, before_json, edits_json, published_json FROM review_revisions WHERE job_id = ?",
                (job_id,),
            ).fetchall()
            for revision, *snapshots in revisions:
                for field, raw in zip(_REVISION_SNAPSHOTS, snapshots, strict=True):
                    relocated = _relocate_revision_snapshot(raw, job_id, root)
                    if relocated != raw:
                        db.execute(
                            f'UPDATE review_revisions SET "{field}" = ? WHERE job_id = ? AND revision = ?',
                            (relocated, job_id, revision),
                        )


@contextmanager
def _workspace_index(settings: Settings):
    root = data_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".runner-attempts.lock").open("ab") as index:
        fcntl.flock(index, fcntl.LOCK_EX)
        yield


@contextmanager
def attempt_workspace(settings: Settings, lease: str):
    scoped = attempt_settings(settings, lease)
    root = data_root(scoped)
    # Coordinate creation and deletion so a startup reaper cannot take an
    # attempt between its directory creation and ownership-lock acquisition.
    with _workspace_index(settings):
        root.mkdir(parents=True, exist_ok=True)
        owner = (root / ".owner").open("ab")
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner.close()
            raise RuntimeError("This attempt already has a local file owner") from exc
    try:
        yield scoped
    finally:
        with _workspace_index(settings):
            try:
                shutil.rmtree(root)
            finally:
                owner.close()


def cleanup_abandoned_attempts(settings: Settings) -> None:
    """At startup, reap crashed attempts while preserving other live runners."""
    with _workspace_index(settings):
        attempts = data_root(settings) / "runner-attempts"
        if not attempts.exists():
            return
        for root in attempts.iterdir():
            if root.is_symlink() or not root.is_dir():
                continue
            try:
                with (root / ".owner").open("ab") as owner:
                    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    shutil.rmtree(root)
            except (BlockingIOError, FileNotFoundError):
                continue
