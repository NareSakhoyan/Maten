from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import documents, health, jobs, lexemes, lexicon, occurrences
from app.core.config import get_settings
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Armenian Historical Books OCR API",
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins_list,
        allow_origin_regex=settings.cors_allow_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(health.router, tags=["health"])
    api_v1.include_router(documents.router, tags=["documents"])
    api_v1.include_router(occurrences.router, tags=["occurrences"])
    api_v1.include_router(jobs.router, tags=["jobs"])
    api_v1.include_router(lexicon.router, tags=["lexicon"])
    api_v1.include_router(lexemes.router, tags=["lexemes"])
    app.include_router(api_v1)
    return app


app = create_app()
