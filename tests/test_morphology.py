from __future__ import annotations

import asyncio
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

import app.services.morphology.morphology_service as morphology_service_module
from app.api.routers.documents import update_document_morphology_settings
from app.api.routers.morphology import create_morphology_run
from app.api.routers.reference_sources import update_reference_source_morphology_settings
from app.core.celery_app import celery_app
from app.db.models import (
    Document,
    DocumentPage,
    DocumentStatus,
    JobKind,
    MorphologyAnalysis,
    MorphologyAnalysisStatus,
    MorphologyRun,
    MorphologyRunStatus,
    ReferenceSourceType,
)
from app.schemas.morphology import MorphologyRunCreateRequest, MorphologySettingsUpdateRequest
from app.schemas.reference import ReferenceSourceCreateRequest
from app.services.auth_service import AuthenticatedUser
from app.services.document_service import DocumentService
from app.services.long_running_job_service import LongRunningJobService
from app.services.morphology.morphology_service import MorphologyService
from app.services.morphology.pie_adapter import PieAdapter
from app.services.morphology.pie_runner import PieRawPrediction, PieRuntimeError
from app.services.occurrence_service import OccurrenceService
from app.services.reference_source_service import ReferenceSourceService
from conftest import PRIMARY_USER_ID


class RecordingPieRunner:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[list[list[str]]] = []

    def analyze_sequences(self, sequences: list[list[str]], *, profile: str | None = None) -> list[list[PieRawPrediction]]:  # noqa: ARG002
        self.calls.append(sequences)
        if self.failure is not None:
            raise self.failure
        return [
            [
                PieRawPrediction(
                    token_surface=token,
                    lemma=f"{token}-lemma",
                    pos="NOUN",
                    morph_features="Case=Nom|Number=Sing",
                )
                for token in sequence
            ]
            for sequence in sequences
        ]

    def resolve_analyzer_version(self) -> str:
        return "test-model-v1"


def _current_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=PRIMARY_USER_ID,
        access_token="test-token",
        email="test@example.com",
    )


def _patch_session_scope(monkeypatch, session_factory) -> None:
    @contextmanager
    def _session_scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(morphology_service_module, "session_scope", _session_scope)


def _create_document_with_occurrences(
    db_session: Session,
    *,
    language_stage: str | None,
    morphology_profile: str | None = None,
    page_texts: list[str],
) -> Document:
    document = Document(
        id=uuid4(),
        user_id=PRIMARY_USER_ID,
        title="Morphology Test Document",
        original_filename="morphology.pdf",
        mime_type="application/pdf",
        file_size_bytes=123,
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/morphology.pdf",
        sha256="a" * 64,
        page_count=len(page_texts),
        language_stage=language_stage,
        morphology_profile=morphology_profile,
        status=DocumentStatus.COMPLETED,
    )
    db_session.add(document)
    db_session.flush()

    occurrence_service = OccurrenceService()
    for page_number, text in enumerate(page_texts, start=1):
        page = DocumentPage(
            id=uuid4(),
            document_id=document.id,
            page_number=page_number,
            extraction_method="pdf_text",
            raw_extracted_text=text,
            reconstructed_text=text,
            extracted_text=text,
            char_count=len(text),
        )
        db_session.add(page)
        db_session.flush()
        occurrence_service.store_page_occurrences(
            db_session,
            document_id=document.id,
            page_id=page.id,
            page_number=page_number,
            text=text,
        )

    db_session.commit()
    db_session.refresh(document)
    return document


def test_pie_adapter_maps_ud_features_and_normalizes_lemma() -> None:
    adapter = PieAdapter()

    prediction = adapter.adapt_prediction(
        PieRawPrediction(
            token_surface="աշխարհին",
            lemma="Աշխարհ",
            pos="NOUN",
            morph_features="Case=Dat|Number=Sing",
        )
    )

    assert prediction.lemma == "Աշխարհ"
    assert prediction.lemma_normalized == "աշխարհ"
    assert prediction.pos == "NOUN"
    assert prediction.morph_features == {"Case": "Dat", "Number": "Sing"}


def test_morphology_service_eligibility_uses_profile_override() -> None:
    service = MorphologyService()

    assert service._scope_is_eligible(  # noqa: SLF001
        morphology_service_module.MorphologyScope(
            source_type="imported_book",
            language_stage="modern",
            morphology_profile="xcl_pie",
        )
    )
    assert not service._scope_is_eligible(  # noqa: SLF001
        morphology_service_module.MorphologyScope(
            source_type="imported_book",
            language_stage="modern",
            morphology_profile=None,
        )
    )


def test_morphology_service_eligibility_allows_eastern_profile() -> None:
    service = MorphologyService()
    eastern_available = service.pie_runner.resource_registry.pie_model_path("eastern") is not None

    assert service._scope_is_eligible(  # noqa: SLF001
        morphology_service_module.MorphologyScope(
            source_type="imported_book",
            language_stage="modern",
            morphology_profile="eastern_pie",
        )
    ) == eastern_available
    assert service._scope_is_eligible(  # noqa: SLF001
        morphology_service_module.MorphologyScope(
            source_type="imported_book",
            language_stage="eastern",
            morphology_profile=None,
        )
    ) == eastern_available


def test_morphology_service_uses_pre_tokenized_page_batches_and_persists_lemma_normalized(
    db_session: Session,
    session_factory,
    monkeypatch,
) -> None:
    _patch_session_scope(monkeypatch, session_factory)
    document = _create_document_with_occurrences(
        db_session,
        language_stage="classical",
        page_texts=["Բան Գիրք", "Խոսք"],
    )
    runner = RecordingPieRunner()
    service = MorphologyService(pie_runner=runner)
    run = service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=MorphologyRunCreateRequest(document_id=document.id),
    )

    service.process_run(run.id)

    assert runner.calls == [[["Բան", "Գիրք"], ["Խոսք"]]]

    rows = list(
        db_session.scalars(
            select(MorphologyAnalysis)
            .where(MorphologyAnalysis.document_id == document.id)
            .order_by(MorphologyAnalysis.token_surface.asc())
        )
    )
    assert len(rows) == 3
    assert {row.analysis_status for row in rows} == {MorphologyAnalysisStatus.COMPLETED}
    assert {row.lemma_normalized for row in rows} == {"բան-lemma", "գիրք-lemma", "խոսք-lemma"}
    assert all(row.occurrence_id is not None for row in rows)

    db_session.expire_all()
    refreshed_run = db_session.get(MorphologyRun, run.id)
    assert refreshed_run is not None
    assert refreshed_run.status is MorphologyRunStatus.COMPLETED
    assert refreshed_run.completed_count == 3
    assert refreshed_run.failed_count == 0


def test_morphology_service_skips_ineligible_document_without_calling_pie(
    db_session: Session,
    session_factory,
    monkeypatch,
) -> None:
    _patch_session_scope(monkeypatch, session_factory)
    document = _create_document_with_occurrences(
        db_session,
        language_stage="modern",
        page_texts=["Հայաստան"],
    )
    runner = RecordingPieRunner()
    service = MorphologyService(pie_runner=runner)
    run = service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=MorphologyRunCreateRequest(document_id=document.id),
    )

    service.process_run(run.id)

    assert runner.calls == []
    row = db_session.scalar(
        select(MorphologyAnalysis).where(MorphologyAnalysis.document_id == document.id)
    )
    assert row is not None
    assert row.analysis_status is MorphologyAnalysisStatus.SKIPPED
    assert row.failure_reason == "source_not_eligible"

    summary = service.get_document_summary(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)
    assert summary.completed_count == 0
    assert summary.skipped_count == 1
    assert summary.failed_count == 0


def test_morphology_service_replaces_scope_rows_on_rerun(
    db_session: Session,
    session_factory,
    monkeypatch,
) -> None:
    _patch_session_scope(monkeypatch, session_factory)
    document = _create_document_with_occurrences(
        db_session,
        language_stage="classical",
        page_texts=["Բան"],
    )
    first_runner = RecordingPieRunner()
    first_service = MorphologyService(pie_runner=first_runner)
    first_run = first_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=MorphologyRunCreateRequest(document_id=document.id),
    )
    first_service.process_run(first_run.id)

    class SecondRunner(RecordingPieRunner):
        def analyze_sequences(self, sequences: list[list[str]]) -> list[list[PieRawPrediction]]:
            self.calls.append(sequences)
            return [[PieRawPrediction(token_surface="Բան", lemma="Երկրորդ", pos="NOUN", morph_features="Case=Nom")]]

    second_service = MorphologyService(pie_runner=SecondRunner())
    second_run = second_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=MorphologyRunCreateRequest(document_id=document.id),
    )
    second_service.process_run(second_run.id)

    rows = list(db_session.scalars(select(MorphologyAnalysis).where(MorphologyAnalysis.document_id == document.id)))
    assert len(rows) == 1
    assert rows[0].lemma == "Երկրորդ"
    assert rows[0].lemma_normalized == "երկրորդ"


def test_morphology_service_marks_failed_rows_when_pie_runtime_fails(
    db_session: Session,
    session_factory,
    monkeypatch,
) -> None:
    _patch_session_scope(monkeypatch, session_factory)
    document = _create_document_with_occurrences(
        db_session,
        language_stage="classical",
        page_texts=["Բան"],
    )
    service = MorphologyService(pie_runner=RecordingPieRunner(failure=RuntimeError("pie exploded")))
    run = service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=MorphologyRunCreateRequest(document_id=document.id),
    )

    service.process_run(run.id)

    row = db_session.scalar(
        select(MorphologyAnalysis).where(MorphologyAnalysis.document_id == document.id)
    )
    assert row is not None
    assert row.analysis_status is MorphologyAnalysisStatus.FAILED
    assert "pie exploded" in (row.failure_reason or "")

    db_session.expire_all()
    refreshed_run = db_session.get(MorphologyRun, run.id)
    assert refreshed_run is not None
    assert refreshed_run.status is MorphologyRunStatus.FAILED
    assert refreshed_run.failed_count == 1


def test_morphology_service_skips_rows_when_pie_is_unavailable(
    db_session: Session,
    session_factory,
    monkeypatch,
) -> None:
    _patch_session_scope(monkeypatch, session_factory)
    document = _create_document_with_occurrences(
        db_session,
        language_stage="classical",
        page_texts=["Բան"],
    )
    service = MorphologyService(
        pie_runner=RecordingPieRunner(failure=PieRuntimeError("The PIE executable could not be found."))
    )
    run = service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=MorphologyRunCreateRequest(document_id=document.id),
    )

    service.process_run(run.id)

    row = db_session.scalar(
        select(MorphologyAnalysis).where(MorphologyAnalysis.document_id == document.id)
    )
    assert row is not None
    assert row.analysis_status is MorphologyAnalysisStatus.SKIPPED
    assert row.failure_reason is not None
    assert row.failure_reason.startswith("pie_unavailable:")

    db_session.expire_all()
    refreshed_run = db_session.get(MorphologyRun, run.id)
    assert refreshed_run is not None
    assert refreshed_run.status is MorphologyRunStatus.COMPLETED
    assert refreshed_run.skipped_count == 1
    assert refreshed_run.failed_count == 0


def test_create_morphology_run_enqueues_background_job(
    db_session: Session,
    monkeypatch,
) -> None:
    document = _create_document_with_occurrences(
        db_session,
        language_stage="classical",
        page_texts=["Բան"],
    )
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, task_id=None: sent_tasks.append(
            {"name": name, "args": args or [], "kwargs": kwargs or {}, "task_id": task_id}
        ),
    )

    response = asyncio.run(
        create_morphology_run(
            request=MorphologyRunCreateRequest(document_id=document.id),
            current_user=_current_user(),
            session=db_session,
            morphology_service=MorphologyService(pie_runner=RecordingPieRunner()),
            long_running_job_service=LongRunningJobService(),
        )
    )

    assert response.message == "Morphology run started"
    assert response.job.job_kind is JobKind.MORPHOLOGY
    assert response.run.source_type == "imported_book"
    assert sent_tasks == [
        {
            "name": "app.workers.tasks.process_morphology_run",
            "args": [str(response.run.id)],
            "kwargs": {},
            "task_id": str(response.run.id),
        }
    ]


def test_create_reference_source_stores_morphology_metadata(db_session: Session) -> None:
    source_service = ReferenceSourceService()

    detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(
            display_name="Classical Source",
            source_type=ReferenceSourceType.IMPORTED_WORDLIST,
            language_stage="classical",
            morphology_profile="xcl_pie",
        ),
    )

    assert detail.language_stage == "classical"
    assert detail.morphology_profile == "xcl_pie"


def test_update_document_morphology_settings_can_enqueue_run(
    db_session: Session,
    monkeypatch,
) -> None:
    document = _create_document_with_occurrences(
        db_session,
        language_stage=None,
        page_texts=["Բան"],
    )
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, task_id=None: sent_tasks.append(
            {"name": name, "args": args or [], "kwargs": kwargs or {}, "task_id": task_id}
        ),
    )

    response = asyncio.run(
        update_document_morphology_settings(
            document_id=document.id,
            request=MorphologySettingsUpdateRequest(
                language_stage="classical",
                morphology_profile="xcl_pie",
                run_morphology=True,
            ),
            current_user=_current_user(),
            session=db_session,
            document_service=DocumentService(),
            morphology_service=MorphologyService(pie_runner=RecordingPieRunner()),
            long_running_job_service=LongRunningJobService(),
        )
    )

    assert response.document.language_stage == "classical"
    assert response.document.morphology_profile == "xcl_pie"
    assert response.run is not None
    assert response.job is not None
    assert response.job.job_kind is JobKind.MORPHOLOGY
    assert sent_tasks[0]["name"] == "app.workers.tasks.process_morphology_run"


def test_update_reference_source_morphology_settings_without_run_only_updates_metadata(
    db_session: Session,
    monkeypatch,
) -> None:
    sent_tasks: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args=None, kwargs=None, task_id=None: sent_tasks.append(
            {"name": name, "args": args or [], "kwargs": kwargs or {}, "task_id": task_id}
        ),
    )
    source_service = ReferenceSourceService()
    detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Existing Source"),
    )

    response = asyncio.run(
        update_reference_source_morphology_settings(
            source_id=detail.id,
            request=MorphologySettingsUpdateRequest(
                language_stage="classical",
                morphology_profile="xcl_pie",
                run_morphology=False,
            ),
            current_user=_current_user(),
            session=db_session,
            reference_source_service=source_service,
            morphology_service=MorphologyService(pie_runner=RecordingPieRunner()),
            long_running_job_service=LongRunningJobService(),
        )
    )

    assert response.source.language_stage == "classical"
    assert response.source.morphology_profile == "xcl_pie"
    assert response.run is None
    assert response.job is None
    assert sent_tasks == []
