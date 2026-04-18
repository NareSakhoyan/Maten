from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.core.celery_app import celery_app
from app.schemas.job import IngestionJobRead, RetryJobResponse
from app.services.auth_service import AuthenticatedUser
from app.services.document_service import DocumentService, get_document_service
from app.services.ingestion_error_service import get_ingestion_error_service
from app.services.ingestion_job_service import (
    IngestionJobService,
    get_ingestion_job_service,
)


router = APIRouter(prefix="/jobs")


@router.get("/{job_id}", response_model=IngestionJobRead)
async def get_job(
    job_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    ingestion_job_service: IngestionJobService = Depends(get_ingestion_job_service),
) -> IngestionJobRead:
    job = ingestion_job_service.get_user_job(session, user_id=current_user.user_id, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return ingestion_job_service.build_job_read(session, job)


@router.post("/{job_id}/retry", response_model=RetryJobResponse)
async def retry_job(
    job_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    ingestion_job_service: IngestionJobService = Depends(get_ingestion_job_service),
    document_service: DocumentService = Depends(get_document_service),
) -> RetryJobResponse:
    try:
        retry_job = ingestion_job_service.create_retry_job(
            session,
            user_id=current_user.user_id,
            failed_job_id=job_id,
        )
    except Exception as exc:
        from app.services.ingestion_error_service import IngestionRetryError

        if isinstance(exc, IngestionRetryError):
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        raise

    try:
        celery_app.send_task(
            "app.workers.tasks.process_document_ingestion",
            args=[str(retry_job.id)],
            task_id=str(retry_job.id),
        )
        document_service.mark_document_queued(session, document_id=retry_job.document_id)
    except Exception as exc:
        failure_info = get_ingestion_error_service().map_exception(exc)
        document_service.mark_job_failed(
            session,
            document_id=retry_job.document_id,
            job_id=retry_job.id,
            failure_info=failure_info,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue retry job.",
        ) from exc

    retry_job = ingestion_job_service.get_user_job(session, user_id=current_user.user_id, job_id=retry_job.id)
    if retry_job is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Retry job not found.")
    return RetryJobResponse(
        message="Retry started",
        job=ingestion_job_service.build_job_read(session, retry_job),
    )
