from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import JobKind
from app.schemas.job import ResumeJobStartResponse
from app.services.document_service import DocumentService, get_document_service
from app.services.ingestion_error_service import get_ingestion_error_service
from app.services.ingestion_job_service import IngestionJobService, get_ingestion_job_service
from app.services.job_orchestrator import get_job_orchestrator
from app.services.retry_errors import RetryStartError


class JobResumeService:
    def __init__(
        self,
        *,
        ingestion_job_service: IngestionJobService | None = None,
        document_service: DocumentService | None = None,
    ) -> None:
        self.ingestion_job_service = ingestion_job_service or get_ingestion_job_service()
        self.document_service = document_service or get_document_service()
        self.job_orchestrator = get_job_orchestrator()

    def resume_job(self, session: Session, *, user_id: UUID, job_id: UUID) -> ResumeJobStartResponse:
        resume_job = self.ingestion_job_service.create_resume_job(
            session,
            user_id=user_id,
            source_job_id=job_id,
        )
        try:
            self.job_orchestrator.enqueue(JobKind.INGESTION, resume_job.id)
            self.document_service.mark_document_queued(session, document_id=resume_job.document_id)
        except Exception as exc:
            failure_info = get_ingestion_error_service().map_exception(exc)
            self.document_service.mark_job_failed(
                session,
                document_id=resume_job.document_id,
                job_id=resume_job.id,
                failure_info=failure_info,
            )
            raise RetryStartError(
                status_code=500,
                message="Failed to enqueue resume job.",
            ) from exc

        stored_job = self.ingestion_job_service.get_user_job(session, user_id=user_id, job_id=resume_job.id)
        if stored_job is None:
            raise RetryStartError(status_code=500, message="Resume job not found.")
        return ResumeJobStartResponse(
            message="Resume started",
            document_id=stored_job.document_id,
            job=self.ingestion_job_service.build_job_read(session, stored_job),
        )


def get_job_resume_service() -> JobResumeService:
    return JobResumeService()
