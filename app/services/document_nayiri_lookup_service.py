from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentNayiriLookupRun,
    ExternalLookupCache,
    ExternalLookupResult,
    ExternalLookupSearchMode,
    ExternalLookupStatus,
    ExternalProvider,
    JobKind,
    JobResultResourceType,
    MorphologyRunStatus,
    ReferenceMatchType,
)
from app.core.database import session_scope
from app.schemas.word import DocumentTrustedExternalStatus, WordSearchMode
from app.services.document_trusted_external_service import (
    DocumentTrustedExternalService,
    get_document_trusted_external_service,
)
from app.services.external_lookup_service import ExternalLookupService, get_external_lookup_service
from app.services.job_orchestrator import JobOrchestrator, get_job_orchestrator
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.morphology.morphology_service import MorphologyService, get_morphology_service
from app.services.nayiri_corpus_service import NayiriCorpusService, get_nayiri_corpus_service
from app.utils.text_normalization import normalize_token


class DocumentNayiriLookupService:
    def __init__(
        self,
        *,
        document_trusted_external_service: DocumentTrustedExternalService | None = None,
        external_lookup_service: ExternalLookupService | None = None,
        morphology_service: MorphologyService | None = None,
        nayiri_corpus_service: NayiriCorpusService | None = None,
        job_progress_service: JobProgressService | None = None,
        job_orchestrator: JobOrchestrator | None = None,
    ) -> None:
        self.document_trusted_external_service = (
            document_trusted_external_service or get_document_trusted_external_service()
        )
        self.external_lookup_service = external_lookup_service or get_external_lookup_service()
        self.morphology_service = morphology_service or get_morphology_service()
        self.nayiri_corpus_service = nayiri_corpus_service or get_nayiri_corpus_service()
        self.job_progress_service = job_progress_service or get_job_progress_service()
        self.job_orchestrator = job_orchestrator or get_job_orchestrator()

    def start_document_run(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
    ) -> DocumentNayiriLookupRun:
        document = session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.user_id == user_id,
            )
        )
        if document is None:
            raise ValueError("Document not found.")

        active_run = session.scalar(
            select(DocumentNayiriLookupRun)
            .where(
                DocumentNayiriLookupRun.user_id == str(user_id),
                DocumentNayiriLookupRun.document_id == document_id,
                DocumentNayiriLookupRun.status.in_(
                    (MorphologyRunStatus.QUEUED, MorphologyRunStatus.RUNNING),
                ),
            )
            .order_by(DocumentNayiriLookupRun.created_at.desc(), DocumentNayiriLookupRun.id.desc())
            .limit(1)
        )
        if active_run is not None:
            return active_run

        run = DocumentNayiriLookupRun(
            user_id=str(user_id),
            document_id=document_id,
            status=MorphologyRunStatus.QUEUED,
            result_resource_type=JobResultResourceType.DOCUMENT,
            result_resource_id=str(document_id),
        )
        session.add(run)
        session.flush()
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.NAYIRI_TRUSTED_LOOKUP,
            job=run,
            stage_code="queued",
            progress_percent=0,
        )
        session.commit()
        session.refresh(run)
        self.job_orchestrator.enqueue(JobKind.NAYIRI_TRUSTED_LOOKUP, run.id)
        return run

    def get_user_run(self, session: Session, *, user_id: UUID, run_id: UUID) -> DocumentNayiriLookupRun | None:
        return session.scalar(
            select(DocumentNayiriLookupRun).where(
                DocumentNayiriLookupRun.id == run_id,
                DocumentNayiriLookupRun.user_id == str(user_id),
            )
        )

    def process_run(self, run_id: UUID | str) -> None:
        run_uuid = UUID(str(run_id))

        with session_scope() as session:
            run = self._load_run(session, run_uuid)
            run.status = MorphologyRunStatus.RUNNING
            run.error_message = None
            run.error_code = None
            run.error_message_user = None
            run.next_steps = None
            run.started_at = datetime.now(timezone.utc)
            run.finished_at = None
            run.checked_count = 0
            run.skipped_count = 0
            run.progress_percent = 0
            run.items_processed = 0
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.NAYIRI_TRUSTED_LOOKUP,
                job=run,
                stage_code="loading_words",
                progress_percent=5,
            )

        with session_scope() as session:
            run = self._load_run(session, run_uuid)
            forms = self.document_trusted_external_service.list_document_normalized_forms(
                session,
                user_id=UUID(run.user_id),
                document_id=run.document_id,
            )
            pending_forms = [
                form
                for form in forms
                if self.document_trusted_external_service.needs_nayiri_lookup(
                    session,
                    normalized_form=form,
                )
            ]
            run.items_total = len(pending_forms)
            run.skipped_count = max(len(forms) - len(pending_forms), 0)
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.NAYIRI_TRUSTED_LOOKUP,
                job=run,
                stage_code="checking_nayiri",
                progress_percent=10,
                items_processed=0,
                items_total=len(pending_forms),
            )

        failure_message: str | None = None
        for index, normalized_form in enumerate(pending_forms, start=1):
            try:
                with session_scope() as session:
                    run = self._load_run(session, run_uuid)
                    self.external_lookup_service.lookup(
                        session,
                        user_id=UUID(run.user_id),
                        query=normalized_form,
                        mode=WordSearchMode.NORMALIZED,
                        provider_keys=["nayiri_web"],
                    )
                    if self._should_try_morphology_fallback(session, normalized_form=normalized_form):
                        local_matches = self.nayiri_corpus_service.lookup(normalized_form, limit=1)
                        if local_matches:
                            self._store_local_corpus_cache(
                                session,
                                original_query=normalized_form,
                                canonical_form=local_matches[0].canonical_form,
                                source_count=local_matches[0].source_count,
                            )
                            run.checked_count += 1
                            run.items_processed = index
                            progress = 10 + int((index / max(len(pending_forms), 1)) * 85)
                            self.job_progress_service.set_stage(
                                session,
                                job_kind=JobKind.NAYIRI_TRUSTED_LOOKUP,
                                job=run,
                                stage_code="checking_nayiri",
                                progress_percent=progress,
                                items_processed=index,
                                items_total=len(pending_forms),
                                append_event=index == len(pending_forms) or index % 5 == 0,
                            )
                            continue

                        best_lemma = self._best_morphology_lemma(
                            session,
                            user_id=UUID(run.user_id),
                            normalized_form=normalized_form,
                        )
                        if best_lemma is not None:
                            fallback_batch = self.external_lookup_service.lookup(
                                session,
                                user_id=UUID(run.user_id),
                                query=best_lemma,
                                mode=WordSearchMode.NORMALIZED,
                                provider_keys=["nayiri_web"],
                            )
                            if fallback_batch.items:
                                self._store_morphology_assisted_cache(
                                    session,
                                    original_query=normalized_form,
                                    canonical_query=best_lemma,
                                    batch=fallback_batch,
                                )
                    run.checked_count += 1
                    run.items_processed = index
                    progress = 10 + int((index / max(len(pending_forms), 1)) * 85)
                    self.job_progress_service.set_stage(
                        session,
                        job_kind=JobKind.NAYIRI_TRUSTED_LOOKUP,
                        job=run,
                        stage_code="checking_nayiri",
                        progress_percent=progress,
                        items_processed=index,
                        items_total=len(pending_forms),
                        append_event=index == len(pending_forms) or index % 5 == 0,
                    )
            except Exception as exc:
                failure_message = str(exc)
                break

        with session_scope() as session:
            run = self._load_run(session, run_uuid)
            if failure_message:
                run.status = MorphologyRunStatus.FAILED
                run.error_message = failure_message
                run.error_code = "nayiri_lookup_failed"
                run.error_message_user = "Nayiri lookup could not finish for this document."
                run.next_steps = [
                    "Retry the Nayiri check when your connection is stable.",
                    "Verify Nayiri is enabled in server settings.",
                ]
                run.finished_at = datetime.now(timezone.utc)
                self.job_progress_service.fail(
                    session,
                    job_kind=JobKind.NAYIRI_TRUSTED_LOOKUP,
                    job=run,
                    message_user=run.error_message_user,
                )
                return

            run.status = MorphologyRunStatus.COMPLETED
            run.finished_at = datetime.now(timezone.utc)
            self.job_progress_service.complete(
                session,
                job_kind=JobKind.NAYIRI_TRUSTED_LOOKUP,
                job=run,
                message_user="Nayiri lookup is complete for this document.",
            )

    @staticmethod
    def _load_run(session: Session, run_id: UUID) -> DocumentNayiriLookupRun:
        run = session.get(DocumentNayiriLookupRun, run_id)
        if run is None:
            raise ValueError(f"Document Nayiri lookup run {run_id} was not found.")
        return run

    def _best_morphology_lemma(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
    ) -> str | None:
        summary = self.morphology_service.get_word_evidence_summary(
            session,
            user_id=user_id,
            normalized_form=normalized_form,
        )
        lemma = (summary.best_lemma or "").strip()
        if not lemma:
            return None
        if normalize_token(lemma) == normalize_token(normalized_form):
            return None
        return lemma

    def _should_try_morphology_fallback(self, session: Session, *, normalized_form: str) -> bool:
        status_map = self.document_trusted_external_service.nayiri_status_map(
            session,
            normalized_forms=[normalized_form],
        )
        snapshot = status_map.get(normalized_form)
        if snapshot is None:
            return True
        return snapshot.status in {
            DocumentTrustedExternalStatus.NOT_FOUND,
            DocumentTrustedExternalStatus.UNCHECKED,
        }

    def _store_morphology_assisted_cache(
        self,
        session: Session,
        *,
        original_query: str,
        canonical_query: str,
        batch,
    ) -> None:
        provider_row = session.scalar(
            select(ExternalProvider).where(ExternalProvider.key == "nayiri_web")
        )
        if provider_row is None:
            return

        cache_row = ExternalLookupCache(
            user_id=None,
            provider_id=provider_row.id,
            query_text=original_query,
            normalized_query=normalize_token(original_query) or original_query,
            search_mode=ExternalLookupSearchMode.NORMALIZED,
            status=ExternalLookupStatus.COMPLETED,
            fetched_at=batch.items[0].fetched_at or datetime.now(timezone.utc),
            expires_at=None,
        )
        session.add(cache_row)
        session.flush()

        for item in batch.items:
            metadata_json = {
                **(item.metadata_json or {}),
                "morphology_fallback": True,
                "canonical_lookup_query": canonical_query,
            }
            session.add(
                ExternalLookupResult(
                    cache_id=cache_row.id,
                    provider_id=provider_row.id,
                    matched_form=item.matched_form,
                    normalized_form=item.normalized_form,
                    source_title=item.source_title,
                    source_subtitle=item.source_subtitle,
                    snippet=item.snippet,
                    reference_link=item.reference_link,
                    metadata_json=metadata_json,
                    match_type=item.match_type,
                    match_score=item.match_score,
                )
            )

    def _store_local_corpus_cache(
        self,
        session: Session,
        *,
        original_query: str,
        canonical_form: str,
        source_count: int,
    ) -> None:
        provider_row = session.scalar(
            select(ExternalProvider).where(ExternalProvider.key == "nayiri_web")
        )
        if provider_row is None:
            return

        now = datetime.now(timezone.utc)
        cache_row = ExternalLookupCache(
            user_id=None,
            provider_id=provider_row.id,
            query_text=original_query,
            normalized_query=normalize_token(original_query) or original_query,
            search_mode=ExternalLookupSearchMode.NORMALIZED,
            status=ExternalLookupStatus.COMPLETED,
            fetched_at=now,
            expires_at=None,
        )
        session.add(cache_row)
        session.flush()
        session.add(
            ExternalLookupResult(
                cache_id=cache_row.id,
                provider_id=provider_row.id,
                matched_form=canonical_form,
                normalized_form=normalize_token(canonical_form),
                source_title="Nayiri Corpus (Local Dataset)",
                source_subtitle=None,
                snippet=f"Matched across {source_count} corpus document(s).",
                reference_link=None,
                metadata_json={
                    "local_corpus_match": True,
                    "source_count": source_count,
                },
                match_type=ReferenceMatchType.NORMALIZED,
                match_score=100.0,
            )
        )


def get_document_nayiri_lookup_service() -> DocumentNayiriLookupService:
    return DocumentNayiriLookupService()
