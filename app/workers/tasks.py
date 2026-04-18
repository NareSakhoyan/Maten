from __future__ import annotations

from app.core.celery_app import celery_app
from app.services.ingestion_service import get_ingestion_service


@celery_app.task(name="app.workers.tasks.process_document_ingestion")
def process_document_ingestion(job_id: str) -> dict[str, str]:
    ingestion_service = get_ingestion_service()
    ingestion_service.process_job(job_id)
    return {"job_id": job_id, "status": "completed"}

