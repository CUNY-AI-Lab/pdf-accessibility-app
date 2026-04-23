import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import and_, or_, select

from app.api.health import router as health_router
from app.api.router import api_router
from app.config import BASE_DIR, get_settings
from app.database import get_session_maker, init_db
from app.models import Job
from app.services.anonymous_sessions import (
    csrf_token_for_session,
    ensure_anonymous_session,
    set_anonymous_session_cookie,
)
from app.services.file_storage import cleanup_job_files, ensure_dirs
from app.services.job_manager import get_job_manager
from app.services.job_state import (
    ACTIVE_JOB_STATUSES,
    CLEANUP_INTERRUPTED_ERROR,
    RESTART_INTERRUPTED_ERROR,
    mark_job_failed,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _frontend_dist(frontend_dist_dir: Path | None = None) -> Path | None:
    dist_dir = (frontend_dist_dir or (BASE_DIR / "frontend" / "dist")).resolve()
    if not (dist_dir / "index.html").is_file():
        return None
    return dist_dir


def _resolve_frontend_file(frontend_dist_dir: Path, requested_path: str) -> Path | None:
    if not requested_path:
        return None

    candidate = (frontend_dist_dir / requested_path.lstrip("/")).resolve()
    try:
        candidate.relative_to(frontend_dist_dir)
    except ValueError:
        return None

    if candidate.is_file():
        return candidate
    return None


def _cors_origins() -> list[str]:
    settings = get_settings()
    return [
        origin
        for origin in (item.strip() for item in settings.cors_allow_origins.split(","))
        if origin
    ]


_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CSRF_HEADER_NAME = "x-csrf-token"


def _normalized_origin(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    default_port = 443 if parsed.scheme == "https" else 80
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None or port == default_port:
        return f"{parsed.scheme}://{host}"
    return f"{parsed.scheme}://{host}:{port}"


def _first_forwarded_value(value: str | None) -> str:
    return (value or "").split(",", maxsplit=1)[0].strip()


def _same_origin_candidates(request) -> set[str]:
    candidates: set[str] = set()
    request_origin = _normalized_origin(str(request.url))
    if request_origin:
        candidates.add(request_origin)

    forwarded_proto = _first_forwarded_value(request.headers.get("x-forwarded-proto"))
    forwarded_host = _first_forwarded_value(request.headers.get("x-forwarded-host"))
    host = _first_forwarded_value(request.headers.get("host"))
    external_host = forwarded_host or host
    if forwarded_proto in {"http", "https"} and external_host:
        forwarded_origin = _normalized_origin(f"{forwarded_proto}://{external_host}")
        if forwarded_origin:
            candidates.add(forwarded_origin)

    return candidates


def _request_origin_allowed(request) -> bool:
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        referer = (request.headers.get("referer") or "").strip()
        if not referer:
            return True
        parsed_referer = urlparse(referer)
        if not parsed_referer.scheme or not parsed_referer.netloc:
            return False
        origin = f"{parsed_referer.scheme}://{parsed_referer.netloc}"

    normalized_origin = _normalized_origin(origin)
    if normalized_origin is None:
        return False
    configured_origins = {
        allowed_origin
        for origin in _cors_origins()
        if (allowed_origin := _normalized_origin(origin)) is not None
    }
    return (
        normalized_origin in configured_origins
        or normalized_origin in _same_origin_candidates(request)
    )


def _csrf_valid(request, session_token: str) -> bool:
    settings = get_settings()
    if not settings.csrf_protection_enabled:
        return True
    if request.method.upper() not in _UNSAFE_METHODS:
        return True
    if not request.url.path.startswith("/api"):
        return True
    if not request.cookies.get(settings.anonymous_session_cookie_name):
        return True
    if not _request_origin_allowed(request):
        return False

    supplied = (request.headers.get(_CSRF_HEADER_NAME) or "").strip()
    expected = csrf_token_for_session(session_token)
    return bool(supplied) and supplied == expected


async def _periodic_cleanup():
    """Delete expired jobs (files + DB rows) based on job_ttl_hours."""
    batch_size = 100

    while True:
        try:
            await asyncio.sleep(3600)  # Run every hour
            total_removed = await _cleanup_expired_jobs_once(batch_size=batch_size)
            if total_removed:
                logger.info(f"Expired job cleanup complete ({total_removed} removed)")
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Periodic cleanup failed, will retry next cycle")


async def _fail_abandoned_jobs_once(
    *,
    error_message: str = RESTART_INTERRUPTED_ERROR,
) -> int:
    session_maker = get_session_maker()
    async with session_maker() as session:
        result = await session.execute(
            select(Job).where(Job.status.in_(tuple(ACTIVE_JOB_STATUSES)))
        )
        abandoned_jobs = result.scalars().all()
        if not abandoned_jobs:
            return 0

        for job in abandoned_jobs:
            await mark_job_failed(session, job, error=error_message)
        await session.commit()
        return len(abandoned_jobs)


async def _cleanup_expired_jobs_once(*, cutoff: datetime | None = None, batch_size: int = 100) -> int:
    """Delete one or more batches of expired jobs and preserve retryable rows on cleanup failure."""
    session_maker = get_session_maker()
    job_manager = get_job_manager()
    if cutoff is None:
        settings = get_settings()
        cutoff = datetime.now(UTC) - timedelta(hours=settings.job_ttl_hours)

    active_statuses = tuple(ACTIVE_JOB_STATUSES)
    skipped_job_ids: set[str] = set()
    total_removed = 0

    while True:
        async with session_maker() as session:
            # Terminal jobs expire from their original upload time. Only active jobs
            # get an updated-at grace window so a fresh rerun does not age out immediately.
            query = select(Job).where(
                or_(
                    and_(Job.status.in_(active_statuses), Job.updated_at < cutoff),
                    and_(~Job.status.in_(active_statuses), Job.created_at < cutoff),
                )
            )
            if skipped_job_ids:
                query = query.where(~Job.id.in_(skipped_job_ids))

            result = await session.execute(query.limit(batch_size))
            expired = result.scalars().all()
            if not expired:
                break

            for job in expired:
                if job_manager.is_running(job.id):
                    await job_manager.cancel_job(job.id)

            removable_jobs: list[Job] = []
            batch_updated = False
            for job in expired:
                try:
                    await asyncio.to_thread(cleanup_job_files, job.id, job.input_path)
                except Exception:
                    skipped_job_ids.add(job.id)
                    logger.exception(f"Failed to clean files for job {job.id}")
                    await mark_job_failed(
                        session,
                        job,
                        error=CLEANUP_INTERRUPTED_ERROR,
                    )
                    batch_updated = True
                else:
                    removable_jobs.append(job)

            if removable_jobs:
                for job in removable_jobs:
                    await session.delete(job)
                await session.commit()
                total_removed += len(removable_jobs)
            elif batch_updated:
                await session.commit()
            else:
                break

    return total_removed


def _register_frontend_routes(app: FastAPI, frontend_dist_dir: Path) -> None:
    index_path = frontend_dist_dir / "index.html"

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def frontend_index():
        return FileResponse(index_path)

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def frontend_app(full_path: str):
        asset_path = _resolve_frontend_file(frontend_dist_dir, full_path)
        if asset_path is not None:
            return FileResponse(asset_path)
        if full_path and (full_path.startswith("assets/") or Path(full_path).suffix):
            raise HTTPException(status_code=404)
        return FileResponse(index_path)


def create_app(frontend_dist_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Starting PDF Accessibility app...")
        ensure_dirs()
        await init_db()
        logger.info("Database initialized")
        expired_jobs = await _cleanup_expired_jobs_once()
        if expired_jobs:
            logger.info("Removed %s expired job(s) during startup cleanup", expired_jobs)
        abandoned_jobs = await _fail_abandoned_jobs_once()
        if abandoned_jobs:
            logger.warning(
                "Marked %s abandoned in-flight job(s) as failed during startup",
                abandoned_jobs,
            )

        cleanup_task = asyncio.create_task(_periodic_cleanup(), name="periodic-cleanup")

        yield

        logger.info("Shutting down...")
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

        job_manager = get_job_manager()
        await job_manager.shutdown()
        logger.info("Shutdown complete")

    app = FastAPI(
        title="PDF Accessibility",
        description="PDF accessibility remediation pipeline",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def anonymous_session_middleware(request, call_next):
        session, created = ensure_anonymous_session(request)
        if not _csrf_valid(request, session.token):
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF validation failed"},
            )
        response = await call_next(request)
        settings = get_settings()
        csrf_cookie = request.cookies.get(settings.anonymous_session_csrf_cookie_name)
        if created or csrf_cookie != csrf_token_for_session(session.token):
            set_anonymous_session_cookie(response, session.token)
        return response

    app.include_router(health_router)
    app.include_router(api_router)

    dist_dir = _frontend_dist(frontend_dist_dir)
    if dist_dir is not None:
        _register_frontend_routes(app, dist_dir)

    return app


app = create_app()
