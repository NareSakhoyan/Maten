from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from sqlalchemy import Engine, event

from app.core.config import Settings
from app.core.request_context import (
    current_user_id_var,
    request_id_var,
    request_method_var,
    request_path_var,
)
from app.core.resources import resource_snapshot


request_logger = logging.getLogger("app.performance.requests")
sql_logger = logging.getLogger("app.performance.sql")

SECRET_QUERY_KEYS = {"token", "access_token", "refresh_token", "authorization", "password", "secret", "key"}
DOCUMENT_ID_PATTERN = re.compile(r"/documents/([0-9a-fA-F-]{36})")


def install_sql_timing(engine: Engine, settings: Settings) -> None:
    if not settings.sql_timing_enabled:
        return
    if getattr(engine, "_baghramyan_sql_timing_installed", False):
        return
    setattr(engine, "_baghramyan_sql_timing_installed", True)

    @event.listens_for(engine, "before_cursor_execute")
    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        context._baghramyan_query_start = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        started_at = getattr(context, "_baghramyan_query_start", None)
        if started_at is None:
            return
        duration_ms = (time.perf_counter() - started_at) * 1000
        if duration_ms < settings.sql_slow_query_ms:
            return
        sql_logger.warning(
            "slow_sql request_id=%s method=%s path=%s duration_ms=%.2f rows=%s statement=%s",
            request_id_var.get(),
            request_method_var.get(),
            request_path_var.get(),
            duration_ms,
            cursor.rowcount,
            _sanitize_statement(statement),
        )


async def request_timing_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
    *,
    settings: Settings,
) -> Response:
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request_id_token = request_id_var.set(request_id)
    path_token = request_path_var.set(request.url.path)
    method_token = request_method_var.set(request.method)
    user_token = current_user_id_var.set(None)
    started_at = time.perf_counter()
    status_code = 500
    response: Response | None = None
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["x-request-id"] = request_id
        return response
    finally:
        duration_ms = (time.perf_counter() - started_at) * 1000
        if response is not None:
            status_code = response.status_code
        _log_request(
            request=request,
            request_id=request_id,
            status_code=status_code,
            duration_ms=duration_ms,
            settings=settings,
        )
        request_id_var.reset(request_id_token)
        request_path_var.reset(path_token)
        request_method_var.reset(method_token)
        current_user_id_var.reset(user_token)


def _log_request(
    *,
    request: Request,
    request_id: str,
    status_code: int,
    duration_ms: float,
    settings: Settings,
) -> None:
    slow_marker = _slow_marker(duration_ms, settings=settings)
    if slow_marker is None and duration_ms < settings.request_slow_info_ms:
        return
    log_level = logging.INFO
    if duration_ms >= settings.request_slow_error_ms:
        log_level = logging.ERROR
    elif duration_ms >= settings.request_slow_warning_ms:
        log_level = logging.WARNING

    extras = ""
    if duration_ms >= settings.request_slow_warning_ms:
        extras = f" resources={resource_snapshot()}"

    request_logger.log(
        log_level,
        "request_timing request_id=%s method=%s path=%s status_code=%s duration_ms=%.2f user_id=%s document_id=%s query=%s slow=%s%s",
        request_id,
        request.method,
        request.url.path,
        status_code,
        duration_ms,
        current_user_id_var.get(),
        _document_id_from_path(request.url.path),
        _safe_query_params(request),
        slow_marker or "false",
        extras,
    )


def _slow_marker(duration_ms: float, *, settings: Settings) -> str | None:
    if duration_ms >= settings.request_slow_error_ms:
        return "error"
    if duration_ms >= settings.request_slow_warning_ms:
        return "warning"
    if duration_ms >= settings.request_slow_info_ms:
        return "info"
    return None


def _document_id_from_path(path: str) -> str | None:
    match = DOCUMENT_ID_PATTERN.search(path)
    return match.group(1) if match else None


def _safe_query_params(request: Request) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        normalized = key.lower()
        if any(secret_key in normalized for secret_key in SECRET_QUERY_KEYS):
            safe[key] = "[redacted]"
        else:
            safe[key] = value
    return safe


def _sanitize_statement(statement: str) -> str:
    collapsed = " ".join(statement.strip().split())
    return collapsed[:1000]
