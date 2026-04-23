from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import documents, health, jobs, lexemes, lexicon, occurrences, reference_matching, reference_sources, words
from app.core.config import get_settings
from app.core.logging import configure_logging


logger = logging.getLogger("app.api.errors")


def _log_frontend_error(request: Request, *, status_code: int, detail: object) -> None:
    logger.error(
        "Frontend error response sent: status=%s method=%s path=%s detail=%s",
        status_code,
        request.method,
        request.url.path,
        detail,
    )


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

    @app.exception_handler(StarletteHTTPException)
    async def log_http_exception(request: Request, exc: StarletteHTTPException):
        _log_frontend_error(request, status_code=exc.status_code, detail=exc.detail)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def log_validation_exception(request: Request, exc: RequestValidationError):
        _log_frontend_error(request, status_code=422, detail=exc.errors())
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def log_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled server error while processing method=%s path=%s",
            request.method,
            request.url.path,
        )
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(health.router, tags=["health"])
    api_v1.include_router(documents.router, tags=["documents"])
    api_v1.include_router(occurrences.router, tags=["occurrences"])
    api_v1.include_router(jobs.router, tags=["jobs"])
    api_v1.include_router(lexicon.router, tags=["lexicon"])
    api_v1.include_router(lexemes.router, tags=["lexemes"])
    api_v1.include_router(reference_sources.router, tags=["reference-sources"])
    api_v1.include_router(reference_matching.router, tags=["reference-matching"])
    api_v1.include_router(words.router, tags=["words"])
    app.include_router(api_v1)
    return app


app = create_app()
