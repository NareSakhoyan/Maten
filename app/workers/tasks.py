from __future__ import annotations

import logging

from app.core.celery_app import celery_app
from app.services.ingestion_service import get_ingestion_service
from app.services.reference_import_service import get_reference_import_service
from app.services.reference_matching_service import get_reference_matching_service


logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks.process_document_ingestion")
def process_document_ingestion(job_id: str) -> dict[str, str]:
    ingestion_service = get_ingestion_service()
    ingestion_service.process_job(job_id)
    return {"job_id": job_id, "status": "completed"}


@celery_app.task(name="app.workers.tasks.process_reference_matching_run")
def process_reference_matching_run(
    run_id: str,
    *,
    view: str = "candidates",
    include_fuzzy: bool = False,
) -> dict[str, str]:
    logger.info(
        "Starting reference matching run task run_id=%s view=%s include_fuzzy=%s",
        run_id,
        view,
        include_fuzzy,
    )
    reference_matching_service = get_reference_matching_service()
    try:
        reference_matching_service.process_run(run_id, view=view, include_fuzzy=include_fuzzy)
    except Exception:
        logger.exception("Reference matching run task failed run_id=%s", run_id)
        raise
    logger.info("Finished reference matching run task run_id=%s", run_id)
    return {"run_id": run_id, "status": "completed"}


@celery_app.task(name="app.workers.tasks.process_reference_source_import")
def process_reference_source_import(import_run_id: str) -> dict[str, str]:
    logger.info("Starting reference source import task import_run_id=%s", import_run_id)
    reference_import_service = get_reference_import_service()
    try:
        reference_import_service.process_import_run(import_run_id)
    except Exception:
        logger.exception("Reference source import task failed import_run_id=%s", import_run_id)
        raise
    logger.info("Finished reference source import task import_run_id=%s", import_run_id)
    return {"import_run_id": import_run_id, "status": "completed"}
