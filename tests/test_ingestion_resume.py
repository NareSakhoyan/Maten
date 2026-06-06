from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.services.ingestion_service as ingestion_service_module
from app.db.models import Document, DocumentPage, DocumentStatus, ExtractionMethod, IngestionJob, IngestionJobStatus, Occurrence, OccurrenceScriptType
from app.services.ingestion_error_service import IngestionRetryError
from app.services.ingestion_job_service import IngestionJobService
from app.services.ingestion_service import IngestionService
from app.services.job_progress_service import JobProgressService
from app.services.page_extraction_service import ExtractedPage
from conftest import PRIMARY_USER_ID


class StubStorageService:
    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing
        self.settings = SimpleNamespace(
            supabase_bucket_page_images="page-images",
            supabase_bucket_ocr_json="ocr-json",
        )

    def download_bytes(self, bucket: str, path: str) -> bytes:
        if self.missing:
            raise FileNotFoundError(f"{bucket}/{path} missing")
        return b"%PDF-1.4"

    def upload_bytes(self, **_: object) -> None:
        return None

    def upload_json(self, **_: object) -> None:
        return None


class RecordingOccurrenceService:
    def store_page_occurrences(self, session: Session, **kwargs: object) -> list[Occurrence]:
        page_id = kwargs["page_id"]
        page_number = kwargs["page_number"]
        occurrence = Occurrence(
            id=uuid4(),
            document_id=kwargs["document_id"],
            page_id=page_id,
            page_number=page_number,
            token="token",
            normalized_token=f"page-{page_number}",
            script_type=OccurrenceScriptType.ARMENIAN,
            context_snippet=f"context-{page_number}",
        )
        session.add(occurrence)
        session.flush()
        return [occurrence]


class StubPageExtractionService:
    def __init__(self, pages: list[ExtractedPage]) -> None:
        self.pages = pages
        self.start_pages: list[int] = []
        self.ocr_service = SimpleNamespace(settings=SimpleNamespace(tesseract_lang="hye"))

    def iter_document_pages(self, file_bytes: bytes, mime_type: str, *, start_page: int = 1):
        del file_bytes, mime_type
        self.start_pages.append(start_page)
        return len(self.pages), (page for page in self.pages if page.page_number >= start_page)


def _seed_document(session: Session, *, page_count: int = 3, status: DocumentStatus = DocumentStatus.FAILED) -> Document:
    document = Document(
        id=uuid4(),
        user_id=PRIMARY_USER_ID,
        title="Resume Doc",
        original_filename="resume.pdf",
        mime_type="application/pdf",
        file_size_bytes=100,
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/resume.pdf",
        sha256="d" * 64,
        page_count=page_count,
        status=status,
    )
    session.add(document)
    session.flush()
    return document


def _seed_pages(session: Session, *, document: Document, through_page: int) -> None:
    for page_number in range(1, through_page + 1):
        page = DocumentPage(
            id=uuid4(),
            document_id=document.id,
            page_number=page_number,
            extraction_method=ExtractionMethod.PDF_TEXT,
            extracted_text=f"page-{page_number}",
            char_count=len(f"page-{page_number}"),
        )
        session.add(page)
        session.flush()
        session.add(
            Occurrence(
                id=uuid4(),
                document_id=document.id,
                page_id=page.id,
                page_number=page_number,
                token=f"token-{page_number}",
                normalized_token=f"page-{page_number}",
                script_type=OccurrenceScriptType.ARMENIAN,
                context_snippet=f"context-{page_number}",
            )
        )
    session.flush()


def _seed_failed_job(
    session: Session,
    *,
    document: Document,
    items_processed: int,
    items_total: int = 3,
) -> IngestionJob:
    job = IngestionJob(
        id=uuid4(),
        document_id=document.id,
        user_id=PRIMARY_USER_ID,
        status=IngestionJobStatus.FAILED,
        step="failed",
        progress_percent=40,
        items_processed=items_processed,
        items_total=items_total,
        can_retry=True,
    )
    session.add(job)
    session.flush()
    return job


def test_create_resume_job_starts_from_items_processed_checkpoint(db_session: Session) -> None:
    service = IngestionJobService(storage_service=StubStorageService())
    document = _seed_document(db_session)
    failed_job = _seed_failed_job(db_session, document=document, items_processed=126, items_total=299)
    db_session.commit()

    resume_job = service.create_resume_job(
        db_session,
        user_id=PRIMARY_USER_ID,
        source_job_id=failed_job.id,
    )

    assert resume_job.resume_of_job_id == failed_job.id
    assert resume_job.resume_from_page == 126
    assert resume_job.status is IngestionJobStatus.QUEUED
    assert resume_job.items_processed == 125
    assert resume_job.items_total == 299
    assert resume_job.retry_of_job_id is None


def test_create_resume_job_rejects_active_ingestion_for_same_document(db_session: Session) -> None:
    service = IngestionJobService(storage_service=StubStorageService())
    document = _seed_document(db_session)
    failed_job = _seed_failed_job(db_session, document=document, items_processed=10)
    active_job = IngestionJob(
        id=uuid4(),
        document_id=document.id,
        user_id=PRIMARY_USER_ID,
        status=IngestionJobStatus.RUNNING,
        step="running",
        progress_percent=20,
        items_processed=5,
        items_total=20,
    )
    db_session.add(active_job)
    db_session.commit()

    with pytest.raises(IngestionRetryError) as exc_info:
        service.create_resume_job(db_session, user_id=PRIMARY_USER_ID, source_job_id=failed_job.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "An ingestion job is already running for this document."


def test_create_resume_job_rejects_job_without_checkpoint(db_session: Session) -> None:
    service = IngestionJobService(storage_service=StubStorageService())
    document = _seed_document(db_session)
    failed_job = _seed_failed_job(db_session, document=document, items_processed=0)
    db_session.commit()

    with pytest.raises(IngestionRetryError) as exc_info:
        service.create_resume_job(db_session, user_id=PRIMARY_USER_ID, source_job_id=failed_job.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.message == "This job has no checkpoint to resume from."


def test_job_read_exposes_can_resume_and_resume_from_page(db_session: Session) -> None:
    service = IngestionJobService(storage_service=StubStorageService())
    document = _seed_document(db_session)
    failed_job = _seed_failed_job(db_session, document=document, items_processed=42, items_total=100)
    db_session.commit()

    payload = service.build_job_read(db_session, failed_job)

    assert payload.can_resume is True
    assert payload.resume_from_page == 42


def test_retry_job_clears_existing_pages(
    session_factory,
    db_session: Session,
    monkeypatch,
) -> None:
    document = _seed_document(db_session)
    _seed_pages(db_session, document=document, through_page=2)
    failed_job = _seed_failed_job(db_session, document=document, items_processed=2, items_total=3)
    db_session.commit()

    pages = [
        ExtractedPage(page_number=1, extraction_method=ExtractionMethod.PDF_TEXT, extracted_text="one", char_count=3),
        ExtractedPage(page_number=2, extraction_method=ExtractionMethod.PDF_TEXT, extracted_text="two", char_count=3),
        ExtractedPage(page_number=3, extraction_method=ExtractionMethod.PDF_TEXT, extracted_text="three", char_count=5),
    ]

    @contextmanager
    def fake_session_scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(ingestion_service_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        ingestion_service_module.IngestionService,
        "_start_post_ingestion_workflow",
        lambda *args, **kwargs: None,
    )

    ingestion_service = IngestionService(
        storage_service=StubStorageService(),
        page_extraction_service=StubPageExtractionService(pages),
        occurrence_service=RecordingOccurrenceService(),
        job_progress_service=JobProgressService(),
    )
    job_service = IngestionJobService(storage_service=StubStorageService())
    retry_job = job_service.create_retry_job(db_session, user_id=PRIMARY_USER_ID, failed_job_id=failed_job.id)
    ingestion_service.process_job(retry_job.id)

    verification_session = session_factory()
    try:
        stored_pages = verification_session.scalars(
            select(DocumentPage)
            .where(DocumentPage.document_id == document.id)
            .order_by(DocumentPage.page_number.asc())
        ).all()
        assert [(page.page_number, page.extracted_text) for page in stored_pages] == [
            (1, "one"),
            (2, "two"),
            (3, "three"),
        ]
        assert all(page.extracted_text != "page-1" for page in stored_pages)
    finally:
        verification_session.close()


def test_resume_job_preserves_pages_before_checkpoint(
    session_factory,
    db_session: Session,
    monkeypatch,
) -> None:
    document = _seed_document(db_session)
    _seed_pages(db_session, document=document, through_page=2)
    failed_job = _seed_failed_job(db_session, document=document, items_processed=2, items_total=3)
    job_service = IngestionJobService(storage_service=StubStorageService())
    resume_job = job_service.create_resume_job(db_session, user_id=PRIMARY_USER_ID, source_job_id=failed_job.id)
    db_session.commit()

    pages = [
        ExtractedPage(page_number=1, extraction_method=ExtractionMethod.PDF_TEXT, extracted_text="one", char_count=3),
        ExtractedPage(page_number=2, extraction_method=ExtractionMethod.PDF_TEXT, extracted_text="two", char_count=3),
        ExtractedPage(page_number=3, extraction_method=ExtractionMethod.PDF_TEXT, extracted_text="three", char_count=5),
    ]

    @contextmanager
    def fake_session_scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(ingestion_service_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        ingestion_service_module.IngestionService,
        "_start_post_ingestion_workflow",
        lambda *args, **kwargs: None,
    )

    page_extraction_service = StubPageExtractionService(pages)
    ingestion_service = IngestionService(
        storage_service=StubStorageService(),
        page_extraction_service=page_extraction_service,
        occurrence_service=RecordingOccurrenceService(),
        job_progress_service=JobProgressService(),
    )
    ingestion_service.process_job(resume_job.id)
    assert page_extraction_service.start_pages == [2]

    verification_session = session_factory()
    try:
        stored_pages = verification_session.scalars(
            select(DocumentPage)
            .where(DocumentPage.document_id == document.id)
            .order_by(DocumentPage.page_number.asc())
        ).all()
        assert [(page.page_number, page.extracted_text) for page in stored_pages] == [
            (1, "page-1"),
            (2, "two"),
            (3, "three"),
        ]
    finally:
        verification_session.close()


def test_resume_processing_replaces_pages_from_checkpoint_onward(
    session_factory,
    db_session: Session,
    monkeypatch,
) -> None:
    document = _seed_document(db_session)
    _seed_pages(db_session, document=document, through_page=3)
    failed_job = _seed_failed_job(db_session, document=document, items_processed=2, items_total=3)
    resume_job = IngestionJob(
        id=uuid4(),
        document_id=document.id,
        resume_of_job_id=failed_job.id,
        resume_from_page=2,
        user_id=PRIMARY_USER_ID,
        status=IngestionJobStatus.QUEUED,
        step="queued",
        progress_percent=0,
        items_processed=1,
        items_total=3,
    )
    db_session.add(resume_job)
    db_session.commit()

    pages = [
        ExtractedPage(page_number=1, extraction_method=ExtractionMethod.PDF_TEXT, extracted_text="skip", char_count=4),
        ExtractedPage(page_number=2, extraction_method=ExtractionMethod.PDF_TEXT, extracted_text="reprocessed", char_count=11),
        ExtractedPage(page_number=3, extraction_method=ExtractionMethod.PDF_TEXT, extracted_text="fresh", char_count=5),
    ]

    @contextmanager
    def fake_session_scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(ingestion_service_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        ingestion_service_module.IngestionService,
        "_start_post_ingestion_workflow",
        lambda *args, **kwargs: None,
    )

    page_extraction_service = StubPageExtractionService(pages)
    service = IngestionService(
        storage_service=StubStorageService(),
        page_extraction_service=page_extraction_service,
        occurrence_service=RecordingOccurrenceService(),
        job_progress_service=JobProgressService(),
    )
    service.process_job(resume_job.id)
    assert page_extraction_service.start_pages == [2]

    verification_session = session_factory()
    try:
        stored_pages = verification_session.scalars(
            select(DocumentPage)
            .where(DocumentPage.document_id == document.id)
            .order_by(DocumentPage.page_number.asc())
        ).all()
        assert [(page.page_number, page.extracted_text) for page in stored_pages] == [
            (1, "page-1"),
            (2, "reprocessed"),
            (3, "fresh"),
        ]

        stored_job = verification_session.get(IngestionJob, resume_job.id)
        assert stored_job is not None
        assert stored_job.status is IngestionJobStatus.COMPLETED
        assert stored_job.items_processed == 3
    finally:
        verification_session.close()


def test_can_resume_stale_running_job_with_progress(db_session: Session) -> None:
    service = IngestionJobService(
        storage_service=StubStorageService(),
        stale_job_recovery_service=SimpleNamespace(
            is_job_stale=lambda *args, **kwargs: (True, datetime.now(timezone.utc) - timedelta(hours=1)),
        ),
    )
    document = _seed_document(db_session, status=DocumentStatus.PROCESSING)
    running_job = IngestionJob(
        id=uuid4(),
        document_id=document.id,
        user_id=PRIMARY_USER_ID,
        status=IngestionJobStatus.RUNNING,
        step="running",
        progress_percent=40,
        items_processed=50,
        items_total=299,
    )
    db_session.add(running_job)
    db_session.commit()

    assert service.can_resume_job(db_session, running_job) is True

    resume_job = service.create_resume_job(
        db_session,
        user_id=PRIMARY_USER_ID,
        source_job_id=running_job.id,
    )
    assert resume_job.resume_from_page == 50
