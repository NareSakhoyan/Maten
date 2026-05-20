from __future__ import annotations

from dataclasses import dataclass

import redis
from sqlalchemy import text
from app.core.config import get_settings
from app.core.database import SessionLocal


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    status: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    status: str
    database: ComponentHealth
    redis: ComponentHealth

    @property
    def is_ready(self) -> bool:
        return self.status == "ok"


def check_database() -> ComponentHealth:
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        return ComponentHealth(status="ok")
    except Exception as exc:
        return ComponentHealth(status="error", detail=str(exc))


def check_redis() -> ComponentHealth:
    settings = get_settings()
    try:
        client = redis.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        return ComponentHealth(status="ok")
    except Exception as exc:
        return ComponentHealth(status="error", detail=str(exc))


def get_readiness_report() -> ReadinessReport:
    database = check_database()
    redis_health = check_redis()
    overall = "ok" if database.status == "ok" and redis_health.status == "ok" else "degraded"
    return ReadinessReport(status=overall, database=database, redis=redis_health)
