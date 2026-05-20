from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentPage,
    DocumentStatus,
    IngestionJob,
    IngestionJobStatus,
    JobKind,
    JobResultResourceType,
    Occurrence,
)
from app.schemas.document import DocumentOptionRead, DocumentRead
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.ingestion_error_service import IngestionFailureInfo
from app.services.storage_service import StorageService, get_storage_service, sanitize_storage_filename


DEFAULT_LANGUAGE_STAGE = "classical"
DEFAULT_MORPHOLOGY_PROFILE = "xcl_pie"


class DocumentService:
    def __init__(
        self,
        storage_service: StorageService | None = None,
        job_progress_service: JobProgressService | None = None,
    ) -> None:
        self.storage_service = storage_service or get_storage_service()
        self.job_progress_service = job_progress_service or get_job_progress_service()

    def create_document_and_job(
        self,
        session: Session,
        *,
        user_id: UUID,
        title: str | None,
        original_filename: str,
        mime_type: str,
        file_size_bytes: int,
        file_bytes: bytes,
        sha256: str,
        language_stage: str | None = None,
        morphology_profile: str | None = None,
    ) -> tuple[Document, IngestionJob]:
        document_id = uuid4()
        job_id = uuid4()
        storage_filename = sanitize_storage_filename(original_filename)
        storage_path = f"{user_id}/{document_id}/original/{storage_filename}"

        self.storage_service.upload_bytes(
            bucket=self.storage_service.settings.supabase_bucket_book_originals,
            path=storage_path,
            data=file_bytes,
            content_type=mime_type,
        )

        document = Document(
            id=document_id,
            user_id=user_id,
            title=self._resolve_title(title, original_filename),
            original_filename=original_filename,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            storage_bucket=self.storage_service.settings.supabase_bucket_book_originals,
            storage_path=storage_path,
            sha256=sha256,
            page_count=None,
            language_stage=self._language_stage_or_default(language_stage),
            morphology_profile=self._morphology_profile_or_default(morphology_profile),
            status=DocumentStatus.UPLOADED,
        )
        job = IngestionJob(
            id=job_id,
            document_id=document_id,
            user_id=user_id,
            status=IngestionJobStatus.QUEUED,
            step="queued",
            progress_percent=0,
            result_resource_type=JobResultResourceType.DOCUMENT,
            result_resource_id=str(document_id),
        )

        session.add(document)
        session.add(job)
        session.flush()
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.INGESTION,
            job=job,
            stage_code="queued",
            progress_percent=0,
        )
        session.commit()
        session.refresh(document)
        session.refresh(job)
        from app.services.document_workflow_service import get_document_workflow_service

        get_document_workflow_service().ensure_workflow(
            session,
            document=document,
            last_job_id=job.id,
        )
        session.commit()
        return document, job

    def mark_document_queued(self, session: Session, *, document_id: UUID) -> Document:
        document = session.get(Document, document_id)
        if document is None:
            raise ValueError(f"Document {document_id} was not found.")
        document.status = DocumentStatus.QUEUED
        session.commit()
        session.refresh(document)
        from app.services.document_workflow_service import get_document_workflow_service

        get_document_workflow_service().sync_for_document(session, document_id=document.id)
        session.commit()
        return document

    def mark_enqueue_failed(self, session: Session, *, document_id: UUID, job_id: UUID, error_message: str) -> None:
        document = session.get(Document, document_id)
        job = session.get(IngestionJob, job_id)
        if document is not None:
            document.status = DocumentStatus.FAILED
        if job is not None:
            job.status = IngestionJobStatus.FAILED
            job.error_message = error_message
            self.job_progress_service.fail(session, job_kind=JobKind.INGESTION, job=job, message_user=error_message)
        session.commit()

    def mark_job_failed(
        self,
        session: Session,
        *,
        document_id: UUID,
        job_id: UUID,
        failure_info: IngestionFailureInfo,
    ) -> None:
        document = session.get(Document, document_id)
        job = session.get(IngestionJob, job_id)
        if document is not None:
            document.status = DocumentStatus.FAILED
        if job is not None:
            job.status = IngestionJobStatus.FAILED
            job.error_message = failure_info.error_message_user
            job.error_code = failure_info.error_code
            job.error_message_user = failure_info.error_message_user
            job.error_message_technical = failure_info.error_message_technical
            job.next_steps = failure_info.next_steps
            job.can_retry = failure_info.can_retry
            self.job_progress_service.fail(
                session,
                job_kind=JobKind.INGESTION,
                job=job,
                message_user=failure_info.error_message_user,
            )
        session.commit()

    def get_user_document(self, session: Session, *, user_id: UUID, document_id: UUID) -> Document | None:
        return session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.user_id == user_id,
            )
        )

    def build_document_read(
        self,
        session: Session,
        document: Document,
        *,
        include_workspace_summary: bool = False,
        latest_job: IngestionJob | None = None,
    ) -> DocumentRead:
        if latest_job is None:
            latest_job = session.scalar(
                select(IngestionJob)
                .where(IngestionJob.document_id == document.id)
                .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc())
                .limit(1)
            )

        updates: dict[str, object] = {
            "latest_job_id": latest_job.id if latest_job is not None else None,
            "latest_job_status": latest_job.status.value if latest_job is not None else None,
        }
        if include_workspace_summary:
            from app.services.source_word_review_service import SourceWordReviewService

            updates.update(
                SourceWordReviewService().count_document_workspace_summary(
                    session,
                    user_id=document.user_id,
                    document_id=document.id,
                )
            )

        return DocumentRead.model_validate(document).model_copy(update=updates)

    def build_documents_read(
        self,
        session: Session,
        documents: list[Document],
        *,
        include_workspace_summary: bool = False,
    ) -> list[DocumentRead]:
        if not documents:
            return []

        document_ids = [document.id for document in documents]
        latest_jobs = self._latest_ingestion_jobs_for_documents(session, document_ids=document_ids)
        return [
            self.build_document_read(
                session,
                document,
                include_workspace_summary=include_workspace_summary,
                latest_job=latest_jobs.get(document.id),
            )
            for document in documents
        ]

    @staticmethod
    def _latest_ingestion_jobs_for_documents(
        session: Session,
        *,
        document_ids: list[UUID],
    ) -> dict[UUID, IngestionJob]:
        if not document_ids:
            return {}

        latest_created = (
            select(
                IngestionJob.document_id.label("document_id"),
                func.max(IngestionJob.created_at).label("max_created_at"),
            )
            .where(IngestionJob.document_id.in_(document_ids))
            .group_by(IngestionJob.document_id)
            .subquery()
        )
        rows = session.scalars(
            select(IngestionJob)
            .join(
                latest_created,
                (IngestionJob.document_id == latest_created.c.document_id)
                & (IngestionJob.created_at == latest_created.c.max_created_at),
            )
            .order_by(IngestionJob.id.desc())
        ).all()

        latest_by_document: dict[UUID, IngestionJob] = {}
        for job in rows:
            if job.document_id not in latest_by_document:
                latest_by_document[job.document_id] = job
        return latest_by_document

    def list_document_options(
        self,
        session: Session,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
        search: str | None = None,
    ) -> tuple[list[DocumentOptionRead], int]:
        filters = [Document.user_id == user_id]
        if search:
            term = search.strip()
            if term:
                filters.append(
                    (Document.title.ilike(f"%{term}%")) | (Document.original_filename.ilike(f"%{term}%"))
                )

        total = session.scalar(select(func.count(Document.id)).where(*filters)) or 0
        rows = session.execute(
            select(Document.id, Document.title, Document.original_filename)
            .where(*filters)
            .order_by(Document.title.asc(), Document.created_at.desc(), Document.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        items = [
            DocumentOptionRead(
                id=row.id,
                title=row.title,
                original_filename=row.original_filename,
            )
            for row in rows
        ]
        return items, total

    def document_status_stats(self, session: Session, *, user_id: UUID) -> dict[str, int]:
        from app.db.models import DocumentStatus

        counts = {status.value: 0 for status in DocumentStatus}
        rows = session.execute(
            select(Document.status, func.count(Document.id))
            .where(Document.user_id == user_id)
            .group_by(Document.status)
        ).all()
        for status, count in rows:
            counts[status.value] = int(count)

        return {
            "total": sum(counts.values()),
            "completed": counts.get(DocumentStatus.COMPLETED.value, 0),
            "processing": counts.get(DocumentStatus.PROCESSING.value, 0),
            "queued": counts.get(DocumentStatus.QUEUED.value, 0),
            "failed": counts.get(DocumentStatus.FAILED.value, 0),
        }

    def update_morphology_settings(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        language_stage: str | None,
        morphology_profile: str | None,
    ) -> Document:
        document = self.get_user_document(session, user_id=user_id, document_id=document_id)
        if document is None:
            raise ValueError("Document not found.")

        document.language_stage = self._language_stage_or_default(language_stage)
        document.morphology_profile = self._morphology_profile_or_default(morphology_profile)
        session.commit()
        session.refresh(document)
        return document

    def list_documents(self, session: Session, *, user_id: UUID, limit: int, offset: int) -> tuple[list[Document], int]:
        total = session.scalar(select(func.count(Document.id)).where(Document.user_id == user_id)) or 0
        items = list(
            session.scalars(
                select(Document)
                .where(Document.user_id == user_id)
                .order_by(Document.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        return items, total

    def list_document_pages(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[DocumentPage], int]:
        total = (
            session.scalar(
                select(func.count(DocumentPage.id))
                .join(Document, DocumentPage.document_id == Document.id)
                .where(DocumentPage.document_id == document_id, Document.user_id == user_id)
            )
            or 0
        )
        items = list(
            session.scalars(
                select(DocumentPage)
                .join(Document, DocumentPage.document_id == Document.id)
                .where(DocumentPage.document_id == document_id, Document.user_id == user_id)
                .order_by(DocumentPage.page_number.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        return items, total

    def list_occurrences(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        limit: int,
        offset: int,
        page_number: int | None = None,
        normalized_token: str | None = None,
    ) -> tuple[list[Occurrence], int]:
        filters = [Occurrence.document_id == document_id, Document.user_id == user_id]
        if page_number is not None:
            filters.append(Occurrence.page_number == page_number)
        if normalized_token is not None:
            filters.append(Occurrence.normalized_token == normalized_token)

        total = (
            session.scalar(
                select(func.count(Occurrence.id))
                .join(Document, Occurrence.document_id == Document.id)
                .where(*filters)
            )
            or 0
        )
        items = list(
            session.scalars(
                select(Occurrence)
                .join(Document, Occurrence.document_id == Document.id)
                .where(*filters)
                .order_by(Occurrence.page_number.asc(), Occurrence.char_start.asc(), Occurrence.created_at.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        return items, total

    def get_user_job(self, session: Session, *, user_id: UUID, job_id: UUID) -> IngestionJob | None:
        return session.scalar(
            select(IngestionJob).where(
                IngestionJob.id == job_id,
                IngestionJob.user_id == user_id,
            )
        )

    @staticmethod
    def _resolve_title(title: str | None, original_filename: str) -> str:
        if title and title.strip():
            return title.strip()

        stem = Path(original_filename).stem.strip()
        return stem or "Untitled document"

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _language_stage_or_default(value: object) -> str:
        return DocumentService._optional_text(value) or DEFAULT_LANGUAGE_STAGE

    @staticmethod
    def _morphology_profile_or_default(value: object) -> str:
        # TODO: Re-enable profile selection when more morphology tools are available.
        return DocumentService._optional_text(value) or DEFAULT_MORPHOLOGY_PROFILE


def get_document_service() -> DocumentService:
    return DocumentService()
