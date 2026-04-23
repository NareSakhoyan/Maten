from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

import app.services.reference_import_service as reference_import_service_module
from app.api.routers.documents import get_document, upload_document
from app.api.routers.jobs import get_job, list_jobs, retry_job
from app.api.routers.reference_matching import create_reference_matching_run, retry_reference_matching_run
from app.api.routers.reference_sources import (
    get_reference_source,
    import_reference_source_entries,
    retry_reference_source_import,
)
from app.core.celery_app import celery_app
from app.db.models import (
    Document,
    DocumentStatus,
    IngestionJob,
    IngestionJobStatus,
    JobResultResourceType,
    ReferenceImportStatus,
    ReferenceMatchingDirection,
    ReferenceSourceImport,
    ReferenceMatchRun,
    ReferenceMatchRunStatus,
    ReferenceMatchTargetScope,
)
from app.schemas.reference import ReferenceMatchRunCreateRequest, ReferenceSourceCreateRequest
from app.services.auth_service import AuthenticatedUser
from app.services.document_service import DocumentService
from app.services.ingestion_job_service import IngestionJobService
from app.services.job_retry_service import JobRetryService
from app.services.long_running_job_service import LongRunningJobService
from app.services.ocr_service import OCRService
from app.services.reference_import_service import ReferenceImportService
from app.services.reference_matching_service import ReferenceMatchingService
from app.services.reference_source_service import ReferenceSourceService
from conftest import PRIMARY_USER_ID, SECONDARY_USER_ID


class StubUploadFile:
    def __init__(self, filename: str, data: bytes, content_type: str) -> None:
        self.filename = filename
        self._data = data
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._data


class StubStorageService:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.settings = SimpleNamespace(
            supabase_bucket_book_originals="book-originals",
            supabase_bucket_page_images="page-images",
            supabase_bucket_ocr_json="ocr-json",
        )

    def upload_bytes(
        self,
        bucket: str,
        path: str,
        data: bytes,
        content_type: str,
        *,
        upsert: bool = False,
    ) -> None:
        del content_type, upsert
        self.objects[(bucket, path)] = data

    def upload_json(self, bucket: str, path: str, payload: dict[str, object], *, upsert: bool = True) -> None:
        del payload, upsert
        self.objects[(bucket, path)] = b"{}"

    def download_bytes(self, bucket: str, path: str) -> bytes:
        return self.objects[(bucket, path)]


class StubOCRService(OCRService):
    def __init__(self) -> None:
        pass

    def image_to_text(self, image_bytes: bytes) -> str:
        del image_bytes
        return "Հայաստան"


def _current_user(user_id: UUID = PRIMARY_USER_ID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        access_token="test-token",
        email="test@example.com",
    )


def test_document_upload_returns_queued_job_and_latest_job_metadata(
    db_session: Session,
    monkeypatch,
) -> None:
    storage = StubStorageService()
    document_service = DocumentService(storage_service=storage)
    ingestion_job_service = IngestionJobService(storage_service=storage)
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, task_id=None: sent_tasks.append(
            {"name": name, "args": args or [], "kwargs": kwargs or {}, "task_id": task_id}
        ),
    )

    response = asyncio.run(
        upload_document(
            file=StubUploadFile("test.pdf", b"%PDF-1.4 test payload", "application/pdf"),
            title="Test Upload",
            current_user=_current_user(),
            session=db_session,
            document_service=document_service,
            ingestion_job_service=ingestion_job_service,
        )
    )

    assert response.message == "Processing started"
    assert response.job.job_kind.value == "ingestion"
    assert response.job.status == "queued"
    assert response.job.result_resource_type is JobResultResourceType.DOCUMENT
    assert response.job.result_resource_id == str(response.document.id)
    assert sent_tasks == [
        {
            "name": "app.workers.tasks.process_document_ingestion",
            "args": [str(response.job.id)],
            "kwargs": {},
            "task_id": str(response.job.id),
        }
    ]

    detail = asyncio.run(
        get_document(
            document_id=response.document.id,
            current_user=_current_user(),
            session=db_session,
            document_service=document_service,
        )
    )
    assert detail.latest_job_id == response.job.id
    assert detail.latest_job_status == "queued"


def test_retry_endpoint_returns_fast_start_response(db_session: Session, monkeypatch) -> None:
    storage = StubStorageService()
    document_service = DocumentService(storage_service=storage)
    ingestion_job_service = IngestionJobService(storage_service=storage)
    job_retry_service = JobRetryService(
        ingestion_job_service=ingestion_job_service,
        document_service=document_service,
    )
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, task_id=None: sent_tasks.append(
            {"name": name, "args": args or [], "kwargs": kwargs or {}, "task_id": task_id}
        ),
    )

    document = Document(
        id=uuid4(),
        user_id=PRIMARY_USER_ID,
        title="Retry Doc",
        original_filename="retry.pdf",
        mime_type="application/pdf",
        file_size_bytes=100,
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/retry.pdf",
        sha256="a" * 64,
        page_count=None,
        status=DocumentStatus.FAILED,
    )
    failed_job = IngestionJob(
        id=uuid4(),
        document_id=document.id,
        user_id=PRIMARY_USER_ID,
        status=IngestionJobStatus.FAILED,
        progress_percent=100,
        error_message="old error",
        can_retry=True,
        retry_count=1,
        result_resource_type=JobResultResourceType.DOCUMENT,
        result_resource_id=str(document.id),
    )
    db_session.add_all([document, failed_job])
    db_session.commit()
    storage.objects[(document.storage_bucket, document.storage_path)] = b"%PDF-1.4 retry"

    response = asyncio.run(
        retry_job(
            job_id=failed_job.id,
            current_user=_current_user(),
            session=db_session,
            job_retry_service=job_retry_service,
        )
    )

    assert response.message == "Retry started"
    assert response.document_id == document.id
    assert response.job.job_kind.value == "ingestion"
    assert response.job.status == "queued"
    assert response.job.result_resource_type is JobResultResourceType.DOCUMENT
    assert response.job.result_resource_id == str(document.id)
    assert sent_tasks[0]["name"] == "app.workers.tasks.process_document_ingestion"


def test_generic_retry_endpoint_dispatches_reference_import_jobs(db_session: Session, monkeypatch) -> None:
    storage = StubStorageService()
    import_service = ReferenceImportService(storage_service=storage, ocr_service=StubOCRService())
    source_service = ReferenceSourceService()
    job_retry_service = JobRetryService(reference_import_service=import_service)
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, task_id=None: sent_tasks.append(
            {"name": name, "args": args or [], "kwargs": kwargs or {}, "task_id": task_id}
        ),
    )

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Generic Retry Import Source"),
    )
    failed_import = ReferenceSourceImport(
        id=uuid4(),
        source_id=source_detail.id,
        user_id=str(PRIMARY_USER_ID),
        file_name="retry.txt",
        file_type="txt",
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/reference-imports/{source_detail.id}/retry.txt",
        mime_type="text/plain",
        file_size_bytes=len("Հայաստան\n".encode("utf-8")),
        status=ReferenceImportStatus.FAILED,
        progress_percent=100,
        retry_count=1,
        can_retry=True,
        result_resource_type=JobResultResourceType.REFERENCE_SOURCE,
        result_resource_id=str(source_detail.id),
    )
    db_session.add(failed_import)
    db_session.commit()
    storage.objects[(failed_import.storage_bucket, failed_import.storage_path)] = "Հայաստան\n".encode("utf-8")

    response = asyncio.run(
        retry_job(
            job_id=failed_import.id,
            current_user=_current_user(),
            session=db_session,
            job_retry_service=job_retry_service,
        )
    )

    assert response.message == "Retry started"
    assert response.document_id is None
    assert response.job.job_kind.value == "reference_import"
    assert response.job.status == "queued"
    assert response.job.result_resource_type is JobResultResourceType.REFERENCE_SOURCE
    assert response.job.result_resource_id == str(source_detail.id)
    assert sent_tasks == [
        {
            "name": "app.workers.tasks.process_reference_source_import",
            "args": [str(response.job.id)],
            "kwargs": {},
            "task_id": str(response.job.id),
        }
    ]


def test_generic_retry_endpoint_dispatches_reference_matching_jobs(db_session: Session, monkeypatch) -> None:
    matching_service = ReferenceMatchingService()
    source_service = ReferenceSourceService()
    job_retry_service = JobRetryService(reference_matching_service=matching_service)
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, task_id=None: sent_tasks.append(
            {"name": name, "args": args or [], "kwargs": kwargs or {}, "task_id": task_id}
        ),
    )

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Generic Retry Matching Source"),
    )
    failed_run_detail = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(
            source_id=source_detail.id,
            view="linked",
            include_fuzzy=True,
        ),
    )
    failed_run = db_session.get(ReferenceMatchRun, failed_run_detail.id)
    assert failed_run is not None
    failed_run.status = ReferenceMatchRunStatus.FAILED
    failed_run.can_retry = True
    failed_run.error_message = "old error"
    db_session.commit()

    response = asyncio.run(
        retry_job(
            job_id=failed_run.id,
            current_user=_current_user(),
            session=db_session,
            job_retry_service=job_retry_service,
        )
    )

    assert response.message == "Retry started"
    assert response.document_id is None
    assert response.job.job_kind.value == "reference_matching"
    assert response.job.status == "queued"
    assert response.job.result_resource_type is JobResultResourceType.REFERENCE_MATCH_RUN
    assert response.job.result_resource_id == str(response.job.id)
    assert sent_tasks == [
        {
            "name": "app.workers.tasks.process_reference_matching_run",
            "args": [str(response.job.id)],
            "kwargs": {"view": "linked", "include_fuzzy": True},
            "task_id": str(response.job.id),
        }
    ]


def test_reference_import_start_endpoint_does_not_parse_in_request(
    db_session: Session,
    monkeypatch,
) -> None:
    storage = StubStorageService()
    import_service = ReferenceImportService(storage_service=storage, ocr_service=StubOCRService())
    source_service = ReferenceSourceService()
    long_running_job_service = LongRunningJobService()
    monkeypatch.setattr(import_service, "_parse_file", lambda **_: (_ for _ in ()).throw(AssertionError("parsed in request")))
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, task_id=None: sent_tasks.append(
            {"name": name, "args": args or [], "kwargs": kwargs or {}, "task_id": task_id}
        ),
    )

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Async Import Source"),
    )

    response = asyncio.run(
        import_reference_source_entries(
            source_id=source_detail.id,
            file=StubUploadFile("reference.txt", "Հայաստան\nԳիրք\n".encode("utf-8"), "text/plain"),
            current_user=_current_user(),
            session=db_session,
            reference_source_service=source_service,
            reference_import_service=import_service,
            long_running_job_service=long_running_job_service,
        )
    )

    assert response.message == "Reference import started"
    assert response.job.job_kind.value == "reference_import"
    assert response.job.status == "queued"
    assert response.job.result_resource_type is JobResultResourceType.REFERENCE_SOURCE
    assert response.job.result_resource_id == str(source_detail.id)
    assert response.import_run.status is ReferenceImportStatus.QUEUED
    assert response.source.latest_import_job_id == response.job.id
    assert response.source.latest_import_job_status == "queued"
    assert sent_tasks == [
        {
            "name": "app.workers.tasks.process_reference_source_import",
            "args": [str(response.job.id)],
            "kwargs": {},
            "task_id": str(response.job.id),
        }
    ]

    stored_import = db_session.get(reference_import_service_module.ReferenceSourceImport, response.job.id)
    assert stored_import is not None
    assert stored_import.storage_bucket == "book-originals"
    assert stored_import.storage_path is not None
    assert stored_import.rows_imported is None


def test_reference_import_worker_processes_stored_file_and_sets_result_link(
    session_factory,
    db_session: Session,
    monkeypatch,
) -> None:
    storage = StubStorageService()
    import_service = ReferenceImportService(storage_service=storage, ocr_service=StubOCRService())
    source_service = ReferenceSourceService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Worker Import Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_run = import_service.create_import_run(
        db_session,
        source=source,
        filename="worker.txt",
        mime_type="text/plain",
        file_size_bytes=len("Հայաստան\n".encode("utf-8")),
    )
    import_service.store_import_file(
        db_session,
        source=source,
        import_run=import_run,
        content="Հայաստան\n".encode("utf-8"),
        content_type="text/plain",
    )
    db_session.commit()

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

    monkeypatch.setattr(reference_import_service_module, "session_scope", fake_session_scope)
    import_service.process_import_run(import_run.id)

    verification_session = session_factory()
    try:
        stored_import = verification_session.get(reference_import_service_module.ReferenceSourceImport, import_run.id)
        assert stored_import is not None
        assert stored_import.status is ReferenceImportStatus.COMPLETED
        assert stored_import.result_resource_type is JobResultResourceType.REFERENCE_SOURCE
        assert stored_import.result_resource_id == str(source.id)
        assert stored_import.rows_imported == 1
        source_detail = asyncio.run(
            get_reference_source(
                source_id=source.id,
                current_user=_current_user(),
                session=verification_session,
                reference_source_service=source_service,
            )
        )
        assert source_detail.latest_import is not None
        assert source_detail.latest_import.id == import_run.id
        assert source_detail.latest_import.source_id == source.id
        assert source_detail.latest_import.rows_read == 1
        assert source_detail.latest_import.rows_imported == 1
        assert source_detail.latest_import.rows_skipped == 0
        assert source_detail.latest_import.result_resource_type is JobResultResourceType.REFERENCE_SOURCE
        assert source_detail.latest_import.result_resource_id == str(source.id)
        assert source_detail.latest_import_job_id == import_run.id
        assert source_detail.latest_import_job_status == "completed"
    finally:
        verification_session.close()


def test_reference_import_retry_endpoint_starts_new_background_attempt(db_session: Session, monkeypatch) -> None:
    storage = StubStorageService()
    import_service = ReferenceImportService(storage_service=storage, ocr_service=StubOCRService())
    source_service = ReferenceSourceService()
    long_running_job_service = LongRunningJobService()
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, task_id=None: sent_tasks.append(
            {"name": name, "args": args or [], "kwargs": kwargs or {}, "task_id": task_id}
        ),
    )

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Retry Import Source"),
    )
    failed_import = ReferenceSourceImport(
        id=uuid4(),
        source_id=source_detail.id,
        user_id=str(PRIMARY_USER_ID),
        file_name="retry.txt",
        file_type="txt",
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/reference-imports/{source_detail.id}/retry.txt",
        mime_type="text/plain",
        file_size_bytes=len("Հայաստան\n".encode("utf-8")),
        status=ReferenceImportStatus.FAILED,
        progress_percent=100,
        retry_count=1,
        can_retry=True,
        result_resource_type=JobResultResourceType.REFERENCE_SOURCE,
        result_resource_id=str(source_detail.id),
    )
    db_session.add(failed_import)
    db_session.commit()
    storage.objects[(failed_import.storage_bucket, failed_import.storage_path)] = "Հայաստան\n".encode("utf-8")

    response = asyncio.run(
        retry_reference_source_import(
            source_id=source_detail.id,
            import_id=failed_import.id,
            current_user=_current_user(),
            session=db_session,
            reference_source_service=source_service,
            reference_import_service=import_service,
            long_running_job_service=long_running_job_service,
        )
    )

    assert response.message == "Reference import retry started"
    assert response.job.job_kind.value == "reference_import"
    assert response.job.status == "queued"
    assert response.source.latest_import_job_id == response.job.id
    stored_retry = db_session.get(ReferenceSourceImport, response.import_run.id)
    assert stored_retry is not None
    assert stored_retry.retry_of_job_id == failed_import.id
    assert stored_retry.storage_path == failed_import.storage_path
    assert sent_tasks == [
        {
            "name": "app.workers.tasks.process_reference_source_import",
            "args": [str(response.job.id)],
            "kwargs": {},
            "task_id": str(response.job.id),
        }
    ]


def test_reference_matching_start_endpoint_and_jobs_lookup(db_session: Session, monkeypatch) -> None:
    matching_service = ReferenceMatchingService()
    source_service = ReferenceSourceService()
    long_running_job_service = LongRunningJobService()
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, task_id=None: sent_tasks.append(
            {"name": name, "args": args or [], "kwargs": kwargs or {}, "task_id": task_id}
        ),
    )

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Async Matching Source"),
    )

    response = asyncio.run(
        create_reference_matching_run(
            request=ReferenceMatchRunCreateRequest(source_id=source_detail.id),
            current_user=_current_user(),
            session=db_session,
            reference_matching_service=matching_service,
            long_running_job_service=long_running_job_service,
        )
    )

    assert response.message == "Reference matching started"
    assert response.job.job_kind.value == "reference_matching"
    assert response.job.status == "queued"
    assert response.job.result_resource_type is JobResultResourceType.REFERENCE_MATCH_RUN
    assert response.job.result_resource_id == str(response.run.id)
    assert response.run.status is ReferenceMatchRunStatus.QUEUED
    assert response.run.matching_direction is ReferenceMatchingDirection.SOURCE_TO_INTERNAL
    assert response.run.source_id == source_detail.id
    assert response.run.target_scope is ReferenceMatchTargetScope.ALL_INTERNAL

    list_response = asyncio.run(
        list_jobs(
            limit=20,
            offset=0,
            job_kind=None,
            status_filter=None,
            current_user=_current_user(),
            session=db_session,
            long_running_job_service=long_running_job_service,
        )
    )
    assert any(item.id == response.job.id for item in list_response.items)

    detail_response = asyncio.run(
        get_job(
            job_id=response.job.id,
            current_user=_current_user(),
            session=db_session,
            long_running_job_service=long_running_job_service,
        )
    )
    assert detail_response.job_kind.value == "reference_matching"

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            get_job(
                job_id=response.job.id,
                current_user=_current_user(SECONDARY_USER_ID),
                session=db_session,
                long_running_job_service=long_running_job_service,
            )
        )
    assert exc_info.value.status_code == 404


def test_reference_matching_retry_endpoint_reuses_saved_run_options(db_session: Session, monkeypatch) -> None:
    matching_service = ReferenceMatchingService()
    source_service = ReferenceSourceService()
    long_running_job_service = LongRunningJobService()
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, task_id=None: sent_tasks.append(
            {"name": name, "args": args or [], "kwargs": kwargs or {}, "task_id": task_id}
        ),
    )

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Retry Matching Source"),
    )
    failed_run_detail = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(
            source_id=source_detail.id,
            view="linked",
            include_fuzzy=True,
        ),
    )
    failed_run = db_session.get(ReferenceMatchRun, failed_run_detail.id)
    assert failed_run is not None
    failed_run.status = ReferenceMatchRunStatus.FAILED
    failed_run.can_retry = True
    failed_run.error_message = "old error"
    db_session.commit()

    response = asyncio.run(
        retry_reference_matching_run(
            run_id=failed_run.id,
            current_user=_current_user(),
            session=db_session,
            reference_matching_service=matching_service,
            long_running_job_service=long_running_job_service,
        )
    )

    assert response.message == "Reference matching retry started"
    assert response.job.job_kind.value == "reference_matching"
    assert response.job.status == "queued"
    stored_retry = db_session.get(ReferenceMatchRun, response.run.id)
    assert stored_retry is not None
    assert stored_retry.retry_of_job_id == failed_run.id
    assert stored_retry.requested_view == "linked"
    assert stored_retry.include_fuzzy is True
    assert sent_tasks == [
        {
            "name": "app.workers.tasks.process_reference_matching_run",
            "args": [str(response.job.id)],
            "kwargs": {"view": "linked", "include_fuzzy": True},
            "task_id": str(response.job.id),
        }
    ]
