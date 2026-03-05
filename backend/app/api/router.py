from fastapi import APIRouter

from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.review import router as review_router

api_router = APIRouter(prefix="/api")
api_router.include_router(jobs_router)
api_router.include_router(documents_router)
api_router.include_router(review_router)

# Health is mounted at root level, not under /api
__all__ = ["api_router", "health_router"]
