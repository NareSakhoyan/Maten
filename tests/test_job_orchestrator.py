from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.db.models import JobKind
from app.services.job_orchestrator import JobOrchestrator


def test_enqueue_uses_job_id_as_celery_task_id() -> None:
    orchestrator = JobOrchestrator()
    job_id = uuid4()

    with patch("app.services.job_orchestrator.celery_app.send_task") as send_task:
        orchestrator.enqueue(
            JobKind.REFERENCE_MATCHING,
            job_id,
            kwargs={"view": "candidates", "include_fuzzy": False},
        )

    send_task.assert_called_once_with(
        "app.workers.tasks.process_reference_matching_run",
        args=[str(job_id)],
        kwargs={"view": "candidates", "include_fuzzy": False},
        task_id=str(job_id),
    )


def test_mark_completed_delegates_to_progress_service() -> None:
    progress = MagicMock()
    orchestrator = JobOrchestrator(job_progress_service=progress)
    session = MagicMock()
    job = MagicMock()

    orchestrator.mark_completed(session, job_kind=JobKind.INGESTION, job=job, message_user="Done")

    progress.complete.assert_called_once_with(
        session,
        job_kind=JobKind.INGESTION,
        job=job,
        message_user="Done",
    )
