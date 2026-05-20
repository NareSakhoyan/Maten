from __future__ import annotations

import asyncio
from unittest.mock import patch

from fastapi import Response

from app.api.routers.health import healthcheck, readiness_check
from app.services.health_service import ComponentHealth, ReadinessReport


def test_healthcheck() -> None:
    response = asyncio.run(healthcheck())
    assert response.status == "ok"


def test_readiness_ok() -> None:
    report = ReadinessReport(
        status="ok",
        database=ComponentHealth(status="ok"),
        redis=ComponentHealth(status="ok"),
    )
    response = Response()

    with patch("app.api.routers.health.get_readiness_report", return_value=report):
        body = asyncio.run(readiness_check(response))

    assert body.status == "ok"
    assert response.status_code == 200


def test_readiness_degraded_returns_503() -> None:
    report = ReadinessReport(
        status="degraded",
        database=ComponentHealth(status="ok"),
        redis=ComponentHealth(status="error", detail="connection refused"),
    )
    response = Response()

    with patch("app.api.routers.health.get_readiness_report", return_value=report):
        body = asyncio.run(readiness_check(response))

    assert body.status == "degraded"
    assert body.redis.status == "error"
    assert response.status_code == 503
