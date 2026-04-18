from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentPage, DocumentStatus, IngestionJob, IngestionJobStatus, Occurrence
from app.services.ingestion_error_service import IngestionFailureInfo
from app.services.storage_service import StorageService, get_storage_service, sanitize_storage_filename


class DocumentService:
    def __init__(self, storage_service: StorageService | None = None) -> None:
        self.storage_service = storage_service or get_storage_service()

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
            status=DocumentStatus.UPLOADED,
        )
        job = IngestionJob(
            id=job_id,
            document_id=document_id,
            user_id=user_id,
            status=IngestionJobStatus.QUEUED,
            step="queued",
            progress_percent=0,
        )

        session.add(document)
        session.add(job)
        session.commit()
        session.refresh(document)
        session.refresh(job)
        return document, job

    def mark_document_queued(self, session: Session, *, document_id: UUID) -> Document:
        document = session.get(Document, document_id)
        if document is None:
            raise ValueError(f"Document {document_id} was not found.")
        document.status = DocumentStatus.QUEUED
        session.commit()
        session.refresh(document)
        return document

    def mark_enqueue_failed(self, session: Session, *, document_id: UUID, job_id: UUID, error_message: str) -> None:
        document = session.get(Document, document_id)
        job = session.get(IngestionJob, job_id)
        if document is not None:
            document.status = DocumentStatus.FAILED
        if job is not None:
            job.status = IngestionJobStatus.FAILED
            job.error_message = error_message
            job.finished_at = datetime.now(timezone.utc)
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
            job.step = "failed"
            job.error_message = failure_info.error_message_user
            job.error_code = failure_info.error_code
            job.error_message_user = failure_info.error_message_user
            job.error_message_technical = failure_info.error_message_technical
            job.next_steps = failure_info.next_steps
            job.can_retry = failure_info.can_retry
            job.finished_at = datetime.now(timezone.utc)
        session.commit()

    def get_user_document(self, session: Session, *, user_id: UUID, document_id: UUID) -> Document | None:
        return session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.user_id == user_id,
            )
        )

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


def get_document_service() -> DocumentService:
    return DocumentService()
