from __future__ import annotations

import logging

from app.core.celery_app import celery_app
from app.services.ingestion_service import get_ingestion_service
from app.services.morphology.morphology_service import get_morphology_service
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


@celery_app.task(name="app.workers.tasks.rebuild_lexicon_index_document")
def rebuild_lexicon_index_document_task(user_id: str, document_id: str) -> dict[str, object]:
    from app.services.lexicon_index_rebuild_service import LexiconIndexRebuildService

    logger.info(
        "Starting lexicon index document rebuild user_id=%s document_id=%s",
        user_id,
        document_id,
    )
    try:
        form_count = LexiconIndexRebuildService.process_document_rebuild(
            user_id=user_id,
            document_id=document_id,
        )
    except Exception:
        logger.exception(
            "Lexicon index document rebuild failed user_id=%s document_id=%s",
            user_id,
            document_id,
        )
        raise
    logger.info(
        "Finished lexicon index document rebuild user_id=%s document_id=%s forms=%s",
        user_id,
        document_id,
        form_count,
    )
    return {"user_id": user_id, "document_id": document_id, "form_count": form_count}


@celery_app.task(name="app.workers.tasks.rebuild_lexicon_index_user")
def rebuild_lexicon_index_user_task(user_id: str) -> dict[str, object]:
    from app.services.lexicon_index_rebuild_service import LexiconIndexRebuildService

    logger.info("Starting lexicon index user rebuild user_id=%s", user_id)
    try:
        document_count = LexiconIndexRebuildService.process_user_rebuild(user_id=user_id)
    except Exception:
        logger.exception("Lexicon index user rebuild failed user_id=%s", user_id)
        raise
    logger.info(
        "Finished lexicon index user rebuild user_id=%s documents=%s",
        user_id,
        document_count,
    )
    return {"user_id": user_id, "document_count": document_count}


@celery_app.task(name="app.workers.tasks.process_document_nayiri_lookup_run")
def process_document_nayiri_lookup_run(run_id: str) -> dict[str, str]:
    from app.services.document_nayiri_lookup_service import get_document_nayiri_lookup_service

    logger.info("Starting document Nayiri lookup run task run_id=%s", run_id)
    lookup_service = get_document_nayiri_lookup_service()
    try:
        lookup_service.process_run(run_id)
    except ValueError as exc:
        if "was not found" in str(exc):
            logger.warning(
                "Skipping stale document Nayiri lookup task run_id=%s reason=%s",
                run_id,
                str(exc),
            )
            return {"run_id": run_id, "status": "skipped_missing_run"}
        logger.exception("Document Nayiri lookup run task failed run_id=%s", run_id)
        raise
    except Exception:
        logger.exception("Document Nayiri lookup run task failed run_id=%s", run_id)
        raise
    logger.info("Finished document Nayiri lookup run task run_id=%s", run_id)
    return {"run_id": run_id, "status": "completed"}


@celery_app.task(name="app.workers.tasks.process_morphology_run")
def process_morphology_run(run_id: str) -> dict[str, str]:
    logger.info("Starting morphology run task run_id=%s", run_id)
    morphology_service = get_morphology_service()
    try:
        morphology_service.process_run(run_id)
    except Exception:
        logger.exception("Morphology run task failed run_id=%s", run_id)
        raise
    logger.info("Finished morphology run task run_id=%s", run_id)
    return {"run_id": run_id, "status": "completed"}
