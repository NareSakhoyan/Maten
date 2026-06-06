from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routers.jobs import get_job
from app.core.celery_app import celery_app
from app.db.models import Document, DocumentStatus, IngestionJob, IngestionJobStatus, JobKind
from app.services.auth_service import AuthenticatedUser
from app.services.ingestion_job_service import IngestionJobService
from app.services.long_running_job_service import LongRunningJobService
from app.services.stale_job_recovery_service import STALE_ERROR_CODE, StaleJobRecoveryService
from conftest import PRIMARY_USER_ID, SECONDARY_USER_ID


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        job_stale_after_minutes=30,
        job_stale_auto_retry_limit=1,
    )


def _old_timestamp() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=45)


def _seed_document(session: Session, *, user_id=PRIMARY_USER_ID) -> Document:
    document = Document(
        id=uuid4(),
        user_id=user_id,
        title="stale doc",
        original_filename="stale.pdf",
        mime_type="application/pdf",
        file_size_bytes=100,
        storage_bucket="book-originals",
        storage_path=f"{user_id}/stale.pdf",
        sha256="b" * 64,
        page_count=None,
        status=DocumentStatus.PROCESSING,
    )
    session.add(document)
    session.flush()
    return document


def _seed_running_job(
    session: Session,
    *,
    document: Document,
    user_id=PRIMARY_USER_ID,
    stale: bool = True,
) -> IngestionJob:
    job = IngestionJob(
        id=uuid4(),
        document_id=document.id,
        user_id=user_id,
        status=IngestionJobStatus.RUNNING,
        step="storing_occurrences",
        current_stage_code="storing_occurrences",
        current_stage_label="Storing occurrences",
        stage_message_user="Saving word occurrences with page and context.",
        progress_percent=81,
        items_processed=44,
        items_total=299,
        can_retry=True,
    )
    session.add(job)
    session.flush()
    if stale:
        job.updated_at = _old_timestamp()
        session.flush()
    session.commit()
    session.refresh(job)
    return job


def _current_user(*, user_id=PRIMARY_USER_ID, role: str = "linguist") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        access_token="test-token",
        email="test@example.com",
        role=role,  # type: ignore[arg-type]
    )


def test_stale_running_ingestion_becomes_failed_and_retry_job_is_created(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _seed_document(db_session)
    job = _seed_running_job(db_session, document=document)
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, queue=None, routing_key=None, task_id=None: sent_tasks.append(
            {
                "name": name,
                "args": args or [],
                "kwargs": kwargs or {},
                "task_id": task_id,
            }
        ),
    )

    storage = MagicMock()
    storage.download_bytes.return_value = b"ok"
    service = StaleJobRecoveryService(
        settings=_settings(),
        ingestion_job_service=IngestionJobService(storage_service=storage),
    )

    assert service.reconcile_active_job(db_session, job_kind=JobKind.INGESTION, job=job) is True

    db_session.refresh(job)
    assert job.status is IngestionJobStatus.FAILED
    assert job.error_code == STALE_ERROR_CODE
    assert job.can_retry is True

    retry_job = db_session.scalar(select(IngestionJob).where(IngestionJob.retry_of_job_id == job.id))
    assert retry_job is not None
    assert retry_job.status is IngestionJobStatus.QUEUED
    assert sent_tasks
    assert sent_tasks[0]["task_id"] == str(retry_job.id)


def test_fresh_running_job_is_not_touched(db_session: Session) -> None:
    document = _seed_document(db_session)
    job = _seed_running_job(db_session, document=document, stale=False)
    service = StaleJobRecoveryService(settings=_settings())

    assert service.reconcile_active_job(db_session, job_kind=JobKind.INGESTION, job=job) is False

    db_session.refresh(job)
    assert job.status is IngestionJobStatus.RUNNING
    assert job.error_code is None


def test_auto_retry_limit_prevents_repeated_retries(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    document = _seed_document(db_session)
    failed_job = _seed_running_job(db_session, document=document)
    service = StaleJobRecoveryService(settings=_settings())
    monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: None)

    storage = MagicMock()
    storage.download_bytes.return_value = b"ok"
    service._ingestion_job_service = IngestionJobService(storage_service=storage)

    assert service.reconcile_active_job(db_session, job_kind=JobKind.INGESTION, job=failed_job) is True
    db_session.refresh(failed_job)
    assert service._count_auto_retries(db_session, job_kind=JobKind.INGESTION, job_id=failed_job.id) == 1
    assert service._auto_retry_stale_job(db_session, job_kind=JobKind.INGESTION, job=failed_job) is None


def test_stale_diagnostics_appear_in_job_detail_and_list(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    document = _seed_document(db_session)
    job = _seed_running_job(db_session, document=document, stale=False)
    job.updated_at = _old_timestamp()
    db_session.commit()

    monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: None)
    storage = MagicMock()
    storage.download_bytes.return_value = b"ok"
    stale_job_recovery_service = StaleJobRecoveryService(
        settings=_settings(),
        ingestion_job_service=IngestionJobService(storage_service=storage),
    )
    long_running_job_service = LongRunningJobService(
        stale_job_recovery_service=stale_job_recovery_service,
    )

    detail = asyncio.run(
        get_job(
            job_id=job.id,
            current_user=_current_user(),
            session=db_session,
            long_running_job_service=long_running_job_service,
        )
    )
    assert detail.status == "running"
    assert detail.is_stale is True
    assert detail.last_progress_at is not None
    assert detail.error_code is None

    items, _ = long_running_job_service.list_jobs(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=50,
        offset=0,
        job_kind=JobKind.INGESTION,
        include_all_users=True,
        include_owner_profile=False,
    )
    listed = next(item for item in items if item.id == job.id)
    assert listed.status == "running"
    assert listed.is_stale is True
    assert listed.error_code is None

    assert stale_job_recovery_service.reconcile_active_job(db_session, job_kind=JobKind.INGESTION, job=job) is True
    db_session.refresh(job)

    detail = asyncio.run(
        get_job(
            job_id=job.id,
            current_user=_current_user(),
            session=db_session,
            long_running_job_service=long_running_job_service,
        )
    )
    assert detail.error_code == STALE_ERROR_CODE
    assert detail.stale_detected_at is not None
    assert detail.recovery_note is not None

    items, _ = long_running_job_service.list_jobs(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=50,
        offset=0,
        job_kind=JobKind.INGESTION,
        include_all_users=True,
        include_owner_profile=False,
    )
    listed = next(item for item in items if item.id == job.id)
    assert listed.error_code == STALE_ERROR_CODE
    assert listed.recovery_note is not None


def test_non_admin_users_still_only_see_their_own_jobs(db_session: Session) -> None:
    own_document = _seed_document(db_session, user_id=PRIMARY_USER_ID)
    own_job = _seed_running_job(db_session, document=own_document, user_id=PRIMARY_USER_ID, stale=False)

    other_document = _seed_document(db_session, user_id=SECONDARY_USER_ID)
    other_job = _seed_running_job(db_session, document=other_document, user_id=SECONDARY_USER_ID, stale=False)

    long_running_job_service = LongRunningJobService()

    own_detail = asyncio.run(
        get_job(
            job_id=own_job.id,
            current_user=_current_user(user_id=PRIMARY_USER_ID),
            session=db_session,
            long_running_job_service=long_running_job_service,
        )
    )
    assert own_detail.id == own_job.id

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            get_job(
                job_id=other_job.id,
                current_user=_current_user(user_id=PRIMARY_USER_ID),
                session=db_session,
                long_running_job_service=long_running_job_service,
            )
        )
    assert exc_info.value.status_code == 404
