from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.schemas import HealthResponse, ReadinessResponse
from app.services.readiness import collect_readiness

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    return HealthResponse()


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness_check():
    readiness = await collect_readiness()
    status_code = 200 if readiness["status"] == "ready" else 503
    return JSONResponse(status_code=status_code, content=readiness)
