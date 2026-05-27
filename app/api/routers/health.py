from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.schemas.common import HealthComponentStatus, HealthResponse, ReadinessResponse
from app.services.health_service import get_readiness_report


router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def healthcheck() -> HealthResponse:
    """Liveness: process is up (use for load balancer ping)."""
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness_check(response: Response) -> ReadinessResponse:
    """Readiness: database and Redis are reachable (use before routing traffic)."""
    report = get_readiness_report()
    if not report.is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status=report.status,
        database=HealthComponentStatus(
            status=report.database.status,
            detail=report.database.detail,
        ),
        redis=HealthComponentStatus(
            status=report.redis.status,
            detail=report.redis.detail,
        ),
    )
