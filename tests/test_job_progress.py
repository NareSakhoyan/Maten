from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.services.ingestion_service as ingestion_service_module
from app.db.models import (
    Document,
    DocumentStatus,
    ExtractionMethod,
    IngestionJob,
    IngestionJobStatus,
    JobKind,
    JobStageEvent,
    Lexeme,
    ReferenceImportStatus,
    ReferenceMatchingDirection,
    ReferenceMatchRun,
    ReferenceMatchRunScope,
    ReferenceMatchRunStatus,
)
from app.schemas.reference import ReferenceMatchRunCreateRequest, ReferenceSourceCreateRequest
from app.services.ingestion_job_service import IngestionJobService
from app.services.ingestion_service import IngestionService
from app.services.job_progress_service import JobProgressService
from app.services.page_extraction_service import ExtractedPage
from app.services.reference_import_service import ReferenceImportService
from app.services.reference_matching_service import ReferenceMatchingService
from app.services.reference_source_service import ReferenceSourceService
from conftest import PRIMARY_USER_ID, SECONDARY_USER_ID


class StubStorageService:
    def __init__(self, *, fail_download: bool = False) -> None:
        self.fail_download = fail_download
        self.settings = SimpleNamespace(
            supabase_bucket_page_images="page-images",
            supabase_bucket_ocr_json="ocr-json",
        )

    def download_bytes(self, bucket: str, path: str) -> bytes:
        if self.fail_download:
            raise FileNotFoundError(f"{bucket}/{path} missing")
        return b"%PDF-1.4"

    def upload_bytes(self, **_: object) -> None:
        return None

    def upload_json(self, **_: object) -> None:
        return None


class StubPageExtractionService:
    def __init__(self, pages: list[ExtractedPage]) -> None:
        self.pages = pages
        self.ocr_service = SimpleNamespace(settings=SimpleNamespace(tesseract_lang="hye"))

    def iter_document_pages(self, file_bytes: bytes, mime_type: str):
        del file_bytes, mime_type
        return len(self.pages), iter(self.pages)


class StubOccurrenceService:
    def store_page_occurrences(self, session: Session, **_: object) -> None:
        del session
        return None


def _seed_document_and_job(session: Session) -> IngestionJob:
    document = Document(
        id=uuid4(),
        user_id=PRIMARY_USER_ID,
        title="Progress Doc",
        original_filename="progress.pdf",
        mime_type="application/pdf",
        file_size_bytes=100,
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/progress.pdf",
        sha256="a" * 64,
        page_count=None,
        status=DocumentStatus.QUEUED,
    )
    job = IngestionJob(
        id=uuid4(),
        document_id=document.id,
        user_id=PRIMARY_USER_ID,
        status=IngestionJobStatus.QUEUED,
        step="queued",
        progress_percent=0,
    )
    session.add_all([document, job])
    session.commit()
    return job


def test_job_detail_response_includes_stage_fields(db_session: Session) -> None:
    job_progress_service = JobProgressService()
    ingestion_job_service = IngestionJobService()
    document = Document(
        id=uuid4(),
        user_id=PRIMARY_USER_ID,
        title="OCR Doc",
        original_filename="ocr.pdf",
        mime_type="application/pdf",
        file_size_bytes=100,
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/ocr.pdf",
        sha256="b" * 64,
        page_count=3,
        status=DocumentStatus.PROCESSING,
    )
    job = IngestionJob(
        id=uuid4(),
        document_id=document.id,
        user_id=PRIMARY_USER_ID,
        status=IngestionJobStatus.RUNNING,
        progress_percent=0,
    )
    db_session.add_all([document, job])
    db_session.flush()
    job_progress_service.set_stage(
        db_session,
        job_kind=JobKind.INGESTION,
        job=job,
        stage_code="running_ocr",
        progress_percent=45,
        items_processed=1,
        items_total=3,
    )
    db_session.commit()

    payload = ingestion_job_service.build_job_read(db_session, job)
    assert payload.current_stage_code == "running_ocr"
    assert payload.current_stage_label == "Running OCR"
    assert payload.stage_message_user == "Reading scanned pages as text."
    assert payload.progress_percent == 45
    assert payload.items_processed == 1
    assert payload.items_total == 3


def test_ingestion_progress_reaches_completion_and_records_events(
    session_factory,
    db_session: Session,
    monkeypatch,
) -> None:
    job = _seed_document_and_job(db_session)

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
    service = IngestionService(
        storage_service=StubStorageService(),
        page_extraction_service=StubPageExtractionService(
            [
                ExtractedPage(
                    page_number=1,
                    extraction_method=ExtractionMethod.PDF_TEXT,
                    extracted_text="Հայաստան",
                    char_count=8,
                ),
                ExtractedPage(
                    page_number=2,
                    extraction_method=ExtractionMethod.OCR,
                    extracted_text="Գիրք",
                    char_count=4,
                    page_image_png=b"png",
                ),
            ]
        ),
        occurrence_service=StubOccurrenceService(),
        job_progress_service=JobProgressService(),
    )

    service.process_job(job.id)

    verification_session = session_factory()
    try:
        stored_job = verification_session.get(IngestionJob, job.id)
        assert stored_job is not None
        assert stored_job.status is IngestionJobStatus.COMPLETED
        assert stored_job.current_stage_code == "completed"
        assert stored_job.current_stage_label == "Completed"
        assert stored_job.progress_percent == 100
        assert stored_job.items_processed == 2
        assert stored_job.items_total == 2

        event_codes = [
            event.stage_code
            for event in verification_session.scalars(
                select(JobStageEvent)
                .where(
                    JobStageEvent.job_kind == JobKind.INGESTION,
                    JobStageEvent.job_id == str(job.id),
                )
                .order_by(JobStageEvent.created_at.asc(), JobStageEvent.id.asc())
            )
        ]
        assert "loading_source_file" in event_codes
        assert "opening_document" in event_codes
        assert "saving_results" in event_codes
        assert event_codes[-1] == "completed"
    finally:
        verification_session.close()


def test_ingestion_failure_preserves_latest_stage_context(
    session_factory,
    db_session: Session,
    monkeypatch,
) -> None:
    job = _seed_document_and_job(db_session)

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
    service = IngestionService(
        storage_service=StubStorageService(fail_download=True),
        page_extraction_service=StubPageExtractionService([]),
        occurrence_service=StubOccurrenceService(),
        job_progress_service=JobProgressService(),
    )

    with pytest.raises(FileNotFoundError):
        service.process_job(job.id)

    verification_session = session_factory()
    try:
        stored_job = verification_session.get(IngestionJob, job.id)
        assert stored_job is not None
        assert stored_job.status is IngestionJobStatus.FAILED
        assert stored_job.current_stage_code == "loading_source_file"
        assert stored_job.current_stage_label == "Preparing file"
        assert stored_job.progress_percent == 5

        latest_event = verification_session.scalar(
            select(JobStageEvent)
            .where(
                JobStageEvent.job_kind == JobKind.INGESTION,
                JobStageEvent.job_id == str(job.id),
            )
            .order_by(JobStageEvent.created_at.desc(), JobStageEvent.id.desc())
            .limit(1)
        )
        assert latest_event is not None
        assert latest_event.stage_code == "loading_source_file"
        assert latest_event.stage_label == "Preparing file"
    finally:
        verification_session.close()


def test_job_events_are_user_scoped(db_session: Session) -> None:
    job_progress_service = JobProgressService()
    ingestion_job_service = IngestionJobService()
    document = Document(
        id=uuid4(),
        user_id=PRIMARY_USER_ID,
        title="Scoped Job",
        original_filename="scoped.pdf",
        mime_type="application/pdf",
        file_size_bytes=100,
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/scoped.pdf",
        sha256="c" * 64,
        page_count=1,
        status=DocumentStatus.QUEUED,
    )
    job = IngestionJob(
        id=uuid4(),
        document_id=document.id,
        user_id=PRIMARY_USER_ID,
        status=IngestionJobStatus.QUEUED,
        progress_percent=0,
    )
    db_session.add_all([document, job])
    db_session.flush()
    job_progress_service.set_stage(
        db_session,
        job_kind=JobKind.INGESTION,
        job=job,
        stage_code="queued",
        progress_percent=0,
    )
    db_session.commit()

    assert ingestion_job_service.get_user_job(db_session, user_id=PRIMARY_USER_ID, job_id=job.id) is not None
    assert ingestion_job_service.get_user_job(db_session, user_id=SECONDARY_USER_ID, job_id=job.id) is None
    assert len(job_progress_service.list_events(
        db_session,
        job_kind=JobKind.INGESTION,
        job_id=job.id,
        user_id=PRIMARY_USER_ID,
    )) == 1
    assert job_progress_service.list_events(
        db_session,
        job_kind=JobKind.INGESTION,
        job_id=job.id,
        user_id=SECONDARY_USER_ID,
    ) == []


def test_reference_import_progress_response_and_source_detail(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    source = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Progress Wordlist"),
    )
    stored_source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source.id)
    assert stored_source is not None
    import_run = import_service.create_import_run(db_session, source=stored_source, filename="reference.txt")
    response = import_service.import_entries(
        db_session,
        source=stored_source,
        filename="reference.txt",
        content="Հայաստան\nԳիրք\n".encode("utf-8"),
        import_run=import_run,
    )

    assert response.status is ReferenceImportStatus.COMPLETED
    assert response.current_stage_code == "completed"
    assert response.current_stage_label == "Completed"
    assert response.progress_percent == 100
    assert response.rows_imported == 2
    assert response.items_processed == 2
    assert response.items_total == 2

    source_detail = source_service.get_source_detail(db_session, user_id=PRIMARY_USER_ID, source_id=source.id)
    assert source_detail is not None
    latest_import = source_detail.latest_import
    assert latest_import is not None
    assert latest_import.id == response.id
    assert latest_import.status is ReferenceImportStatus.COMPLETED


def test_reference_import_events_are_user_scoped(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    job_progress_service = JobProgressService()
    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Scoped Import Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_run = import_service.create_import_run(db_session, source=source, filename="scoped.txt")
    response = import_service.import_entries(
        db_session,
        source=source,
        filename="scoped.txt",
        content="Հայաստան\n".encode("utf-8"),
        import_run=import_run,
    )

    assert import_service.get_user_import(
        db_session,
        user_id=PRIMARY_USER_ID,
        source_id=source.id,
        import_id=response.id,
    ) is not None
    assert import_service.get_user_import(
        db_session,
        user_id=SECONDARY_USER_ID,
        source_id=source.id,
        import_id=response.id,
    ) is None
    assert job_progress_service.list_events(
        db_session,
        job_kind=JobKind.REFERENCE_IMPORT,
        job_id=response.id,
        user_id=SECONDARY_USER_ID,
    ) == []


def test_reference_matching_run_exposes_progress_and_events(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Matching Progress Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_run = import_service.create_import_run(db_session, source=source, filename="matching.txt")
    import_service.import_entries(
        db_session,
        source=source,
        filename="matching.txt",
        content="Հայաստան\n".encode("utf-8"),
        import_run=import_run,
    )

    lexeme = Lexeme(
        id=uuid4(),
        user_id=str(PRIMARY_USER_ID),
        canonical_form="Հայաստան",
        canonical_normalized_form="Հայաստան",
        notes=None,
        status="draft",
    )
    db_session.add(lexeme)
    db_session.flush()

    run = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(
            matching_direction=ReferenceMatchingDirection.INTERNAL_TO_REFERENCE,
            run_scope=ReferenceMatchRunScope.LEXEMES,
        ),
    )
    matching_service.process_run_in_session(
        db_session,
        run_id=run.id,
        include_fuzzy=False,
    )

    stored_run = db_session.get(ReferenceMatchRun, run.id)
    assert stored_run is not None
    assert stored_run.status is ReferenceMatchRunStatus.COMPLETED
    assert stored_run.current_stage_code == "completed"
    assert stored_run.progress_percent == 100
    assert stored_run.items_processed == 1
    assert stored_run.items_total == 1

    run_detail = matching_service.get_run_detail(db_session, user_id=PRIMARY_USER_ID, run_id=run.id)
    assert run_detail is not None
    assert run_detail.current_stage_code == "completed"

    event_codes = [
        event.stage_code
        for event in JobProgressService().list_events(
            db_session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job_id=run.id,
            user_id=PRIMARY_USER_ID,
        )
    ]
    assert "running_exact_match" in event_codes
    assert event_codes[-1] == "completed"

    assert matching_service.get_user_run(db_session, user_id=SECONDARY_USER_ID, run_id=run.id) is None
