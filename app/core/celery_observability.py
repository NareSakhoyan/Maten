from __future__ import annotations

import logging
import threading
import time
from typing import Any

from celery import Celery, signals

from app.core.config import get_settings
from app.core.resources import resource_snapshot


logger = logging.getLogger("app.performance.celery")
_task_starts: dict[str, float] = {}
_task_heartbeats: dict[str, threading.Event] = {}


def install_celery_observability(celery_app: Celery) -> None:
    if getattr(celery_app, "_baghramyan_observability_installed", False):
        return
    setattr(celery_app, "_baghramyan_observability_installed", True)

    @signals.before_task_publish.connect(weak=False)
    def before_task_publish(sender=None, headers=None, body=None, **kwargs):  # noqa: ANN001, ARG001
        task_id = headers.get("id") if isinstance(headers, dict) else None
        logger.info(
            "celery_task_queued task_name=%s task_id=%s queue=%s metadata=%s",
            sender,
            task_id,
            kwargs.get("routing_key"),
            _metadata_from_body(body),
        )

    @signals.task_prerun.connect(weak=False)
    def task_prerun(task_id=None, task=None, args=None, kwargs=None, **signal_kwargs):  # noqa: ANN001, ARG001
        if task_id:
            _task_starts[str(task_id)] = time.perf_counter()
            _start_task_heartbeat(
                task_id=str(task_id),
                task_name=task.name if task else None,
                metadata=_metadata_from_task(task, args, kwargs),
            )
        logger.info(
            "celery_task_start task_name=%s task_id=%s queue=%s metadata=%s resources=%s",
            task.name if task else None,
            task_id,
            _queue_from_task(task),
            _metadata_from_task(task, args, kwargs),
            resource_snapshot(),
        )

    @signals.task_postrun.connect(weak=False)
    def task_postrun(task_id=None, task=None, args=None, kwargs=None, retval=None, state=None, **signal_kwargs):  # noqa: ANN001, ARG001
        if task_id:
            _stop_task_heartbeat(str(task_id))
        started_at = _task_starts.pop(str(task_id), None) if task_id else None
        duration_ms = (time.perf_counter() - started_at) * 1000 if started_at is not None else None
        log_level = logging.INFO
        if duration_ms is not None and duration_ms >= 5000:
            log_level = logging.WARNING
        logger.log(
            log_level,
            "celery_task_end task_name=%s task_id=%s queue=%s state=%s duration_ms=%s metadata=%s resources=%s",
            task.name if task else None,
            task_id,
            _queue_from_task(task),
            state,
            round(duration_ms, 2) if duration_ms is not None else None,
            _metadata_from_task(task, args, kwargs),
            resource_snapshot(),
        )

    @signals.task_failure.connect(weak=False)
    def task_failure(task_id=None, exception=None, traceback=None, sender=None, args=None, kwargs=None, **signal_kwargs):  # noqa: ANN001, ARG001
        if task_id:
            _stop_task_heartbeat(str(task_id))
        started_at = _task_starts.get(str(task_id)) if task_id else None
        duration_ms = (time.perf_counter() - started_at) * 1000 if started_at is not None else None
        logger.error(
            "celery_task_failure task_name=%s task_id=%s queue=%s duration_ms=%s error_type=%s error_message=%s metadata=%s resources=%s",
            sender.name if sender else None,
            task_id,
            _queue_from_task(sender),
            round(duration_ms, 2) if duration_ms is not None else None,
            type(exception).__name__ if exception else None,
            str(exception) if exception else None,
            _metadata_from_task(sender, args, kwargs),
            resource_snapshot(),
        )


def _start_task_heartbeat(*, task_id: str, task_name: str | None, metadata: dict[str, Any]) -> None:
    interval_seconds = get_settings().celery_task_heartbeat_seconds
    if interval_seconds <= 0:
        return

    stop_event = threading.Event()
    _task_heartbeats[task_id] = stop_event
    started_at = time.perf_counter()

    def heartbeat_loop() -> None:
        while not stop_event.wait(interval_seconds):
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.warning(
                "celery_task_heartbeat task_name=%s task_id=%s running_for_ms=%s metadata=%s resources=%s",
                task_name,
                task_id,
                round(duration_ms, 2),
                metadata,
                resource_snapshot(),
            )

    thread = threading.Thread(
        target=heartbeat_loop,
        name=f"celery-heartbeat-{task_id}",
        daemon=True,
    )
    thread.start()


def _stop_task_heartbeat(task_id: str) -> None:
    stop_event = _task_heartbeats.pop(task_id, None)
    if stop_event is not None:
        stop_event.set()


def _queue_from_task(task: Any) -> str | None:
    request = getattr(task, "request", None)
    delivery_info = getattr(request, "delivery_info", None)
    if not isinstance(delivery_info, dict):
        return None
    routing_key = delivery_info.get("routing_key")
    return str(routing_key) if routing_key else None


def _metadata_from_body(body: Any) -> dict[str, Any]:
    if isinstance(body, tuple) and len(body) >= 2:
        args, kwargs = body[0], body[1]
        return _metadata_from_args(args, kwargs)
    return {}


def _metadata_from_args(args: Any, kwargs: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if isinstance(kwargs, dict):
        for key in ("user_id", "document_id", "run_id", "job_id", "import_run_id"):
            if key in kwargs:
                metadata[key] = str(kwargs[key])
    if isinstance(args, (list, tuple)):
        if args:
            metadata["primary_id"] = str(args[0])
        if len(args) >= 2:
            metadata["secondary_id"] = str(args[1])
    return metadata


def _metadata_from_task(task: Any, args: Any, kwargs: Any) -> dict[str, Any]:
    metadata = _metadata_from_args(args, kwargs)
    task_name = getattr(task, "name", None)
    primary_id = metadata.get("primary_id")
    if not task_name or not primary_id:
        return metadata

    try:
        hydrated = _hydrate_task_metadata(task_name=str(task_name), primary_id=str(primary_id))
    except Exception:
        return metadata
    return {**metadata, **hydrated}


def _hydrate_task_metadata(*, task_name: str, primary_id: str) -> dict[str, Any]:
    from uuid import UUID

    from app.core.database import session_scope
    from app.db.models import (
        DiscoveryBuildRun,
        DocumentNayiriLookupRun,
        IngestionJob,
        MorphologyRun,
        ReferenceMatchRun,
        ReferenceSourceImport,
    )

    task_model_map = {
        "app.workers.tasks.process_document_ingestion": IngestionJob,
        "app.workers.tasks.process_reference_source_import": ReferenceSourceImport,
        "app.workers.tasks.process_reference_matching_run": ReferenceMatchRun,
        "app.workers.tasks.process_morphology_run": MorphologyRun,
        "app.workers.tasks.process_document_trusted_external_lookup_run": DocumentNayiriLookupRun,
        "app.workers.tasks.process_document_nayiri_lookup_run": DocumentNayiriLookupRun,
        "app.workers.tasks.process_document_discovery_build": DiscoveryBuildRun,
    }
    model = task_model_map.get(task_name)
    if model is None:
        return {}

    with session_scope() as session:
        row = session.get(model, UUID(primary_id))
        if row is None:
            return {}
        metadata: dict[str, Any] = {}
        for attr in ("user_id", "document_id", "reference_source_id", "source_id"):
            value = getattr(row, attr, None)
            if value:
                metadata[attr] = str(value)
        retry_count = getattr(row, "retry_count", None)
        if retry_count is not None:
            metadata["retry_count"] = int(retry_count)
        return metadata
