from __future__ import annotations

import logging

from fastapi import FastAPI
from sqlalchemy import Engine

from app.core.config import Settings


logger = logging.getLogger("app.performance.otel")


def configure_opentelemetry(app: FastAPI, engine: Engine, settings: Settings) -> None:
    if not settings.otel_enabled:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.trace import set_tracer_provider
    except Exception as exc:
        logger.warning("OpenTelemetry enabled but instrumentation packages are unavailable: %s", exc)
        return

    try:
        resource = Resource.create({"service.name": settings.otel_service_name})
        set_tracer_provider(TracerProvider(resource=resource))
        FastAPIInstrumentor.instrument_app(app)
        SQLAlchemyInstrumentor().instrument(engine=engine)
        logger.info("OpenTelemetry instrumentation enabled service_name=%s", settings.otel_service_name)
    except Exception:
        logger.exception("Failed to configure OpenTelemetry instrumentation")
