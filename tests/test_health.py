from __future__ import annotations

import asyncio
import os


os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/postgres")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-key")

from app.api.routers.health import healthcheck


def test_healthcheck() -> None:
    response = asyncio.run(healthcheck())
    assert response.status == "ok"
