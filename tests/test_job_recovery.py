from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentStatus, IngestionJob, IngestionJobStatus
from app.services.document_service import DocumentService
from app.services.ingestion_error_service import IngestionErrorService, IngestionRetryError
from app.services.ingestion_job_service import IngestionJobService
from conftest import PRIMARY_USER_ID, SECONDARY_USER_ID


class StubStorageService:
    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing

    def download_bytes(self, bucket: str, path: str) -> bytes:
        if self.missing:
            raise FileNotFoundError(f"{bucket}/{path} missing")
        return b"ok"


def _seed_document(session: Session, *, user_id=PRIMARY_USER_ID, status: DocumentStatus = DocumentStatus.FAILED) -> Document:
    document = Document(
        id=uuid4(),
        user_id=user_id,
        title="job doc",
        original_filename="job.pdf",
        mime_type="application/pdf",
        file_size_bytes=100,
        storage_bucket="book-originals",
        storage_path=f"{user_id}/job.pdf",
        sha256="a" * 64,
        page_count=None,
        status=status,
    )
    session.add(document)
    session.flush()
    return document


def _seed_job(
    session: Session,
    *,
    document: Document,
    user_id=PRIMARY_USER_ID,
    status: IngestionJobStatus = IngestionJobStatus.FAILED,
    can_retry: bool = True,
    retry_count: int = 0,
) -> IngestionJob:
    job = IngestionJob(
        id=uuid4(),
        document_id=document.id,
        user_id=user_id,
        status=status,
        step="failed" if status is IngestionJobStatus.FAILED else "queued",
        progress_percent=0,
        error_message="old error" if status is IngestionJobStatus.FAILED else None,
        can_retry=can_retry,
        retry_count=retry_count,
    )
    session.add(job)
    session.flush()
    return job


def test_failed_job_stores_user_safe_error_payload(db_session: Session) -> None:
    document_service = DocumentService()
    error_service = IngestionErrorService()
    document = _seed_document(db_session)
    job = _seed_job(db_session, document=document)

    failure_info = error_service.map_exception(ValueError("Unsupported file type. Only PDF and image uploads are accepted."))
    document_service.mark_job_failed(
        db_session,
        document_id=document.id,
        job_id=job.id,
        failure_info=failure_info,
    )

    db_session.refresh(job)
    assert job.error_code == "unsupported_file_type"
    assert job.error_message_user == "This file type is not supported for ingestion."
    assert job.error_message_technical is not None
    assert job.next_steps == [
        "Upload a PDF or supported image file.",
        "If you believe this file should work, contact the administrator.",
    ]
    assert job.can_retry is False


def test_retry_job_creates_new_job_row_and_links_history(db_session: Session) -> None:
    service = IngestionJobService(storage_service=StubStorageService())
    document = _seed_document(db_session)
    failed_job = _seed_job(db_session, document=document, retry_count=1)
    db_session.commit()

    retry_job = service.create_retry_job(db_session, user_id=PRIMARY_USER_ID, failed_job_id=failed_job.id)

    assert retry_job.retry_of_job_id == failed_job.id
    assert retry_job.status is IngestionJobStatus.QUEUED
    assert retry_job.retry_count == 2
    assert retry_job.id != failed_job.id
    db_session.refresh(failed_job)
    assert failed_job.last_retried_at is not None


def test_retry_job_rejects_non_failed_jobs(db_session: Session) -> None:
    service = IngestionJobService(storage_service=StubStorageService())
    document = _seed_document(db_session, status=DocumentStatus.QUEUED)
    job = _seed_job(db_session, document=document, status=IngestionJobStatus.QUEUED)
    db_session.commit()

    with pytest.raises(IngestionRetryError) as exc_info:
        service.create_retry_job(db_session, user_id=PRIMARY_USER_ID, failed_job_id=job.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "Only failed jobs can be retried."


def test_retry_job_rejects_non_retryable_failures(db_session: Session) -> None:
    service = IngestionJobService(storage_service=StubStorageService())
    document = _seed_document(db_session)
    job = _seed_job(db_session, document=document, can_retry=False)
    db_session.commit()

    with pytest.raises(IngestionRetryError) as exc_info:
        service.create_retry_job(db_session, user_id=PRIMARY_USER_ID, failed_job_id=job.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "This failed job cannot be retried."


def test_retry_job_rejects_another_users_job(db_session: Session) -> None:
    service = IngestionJobService(storage_service=StubStorageService())
    document = _seed_document(db_session, user_id=SECONDARY_USER_ID)
    job = _seed_job(db_session, document=document, user_id=SECONDARY_USER_ID)
    db_session.commit()

    with pytest.raises(IngestionRetryError) as exc_info:
        service.create_retry_job(db_session, user_id=PRIMARY_USER_ID, failed_job_id=job.id)

    assert exc_info.value.status_code == 404


def test_retry_job_rejects_missing_source_file(db_session: Session) -> None:
    service = IngestionJobService(storage_service=StubStorageService(missing=True))
    document = _seed_document(db_session)
    job = _seed_job(db_session, document=document)
    db_session.commit()

    with pytest.raises(IngestionRetryError) as exc_info:
        service.create_retry_job(db_session, user_id=PRIMARY_USER_ID, failed_job_id=job.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "The original uploaded file could not be found. Re-upload the document and try again."


def test_job_detail_payload_includes_next_steps_and_latest_retry(db_session: Session) -> None:
    service = IngestionJobService(storage_service=StubStorageService())
    document = _seed_document(db_session)
    job = _seed_job(db_session, document=document, retry_count=0)
    job.error_code = "unknown_ingestion_error"
    job.error_message_user = "The document could not be processed due to an unexpected error."
    job.error_message = job.error_message_user
    job.next_steps = ["Retry the job.", "If it fails again, contact the administrator."]
    job.can_retry = True
    db_session.flush()
    retry_job = _seed_job(
        db_session,
        document=document,
        status=IngestionJobStatus.QUEUED,
        retry_count=1,
    )
    retry_job.retry_of_job_id = job.id
    db_session.commit()

    payload = service.build_job_read(db_session, job)

    assert payload.error_message_user == "The document could not be processed due to an unexpected error."
    assert payload.next_steps == ["Retry the job.", "If it fails again, contact the administrator."]
    assert payload.can_retry is True
    assert payload.latest_retry_job_id == retry_job.id
