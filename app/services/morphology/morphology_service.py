from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import Settings, get_settings
from app.core.database import session_scope
from app.db.models import (
    Document,
    JobKind,
    JobResultResourceType,
    MorphologyAnalysis,
    MorphologyAnalysisStatus,
    MorphologyRun,
    MorphologyRunStatus,
    Occurrence,
    ReferenceEntry,
    ReferenceSource,
)
from app.schemas.morphology import (
    MorphologyCount,
    MorphologyRunCreateRequest,
    MorphologyRunRead,
    MorphologySummaryResponse,
    MorphologyWordEvidenceSummary,
    MorphologyWordResponse,
)
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.morphology.pie_adapter import PieAdapter, get_pie_adapter
from app.services.morphology.pie_runner import PieRunner, get_pie_runner
from app.services.tokenization_service import TokenizationService, get_tokenization_service
from app.utils.text_normalization import normalize_token
from app.utils.token_classification import classify_token


IMPORTED_BOOK_SOURCE_TYPE = "imported_book"
REFERENCE_SOURCE_ANALYSIS_TYPE = "reference_source"


@dataclass(frozen=True, slots=True)
class MorphologyTokenInput:
    user_id: str
    source_type: str
    sequence_key: str
    token_surface: str
    token_normalized: str
    occurrence_id: UUID | None = None
    document_id: UUID | None = None
    page_id: UUID | None = None
    reference_source_id: UUID | None = None
    reference_entry_id: UUID | None = None
    has_armenian: bool = False
    has_latin: bool = False
    has_digits: bool = False


@dataclass(frozen=True, slots=True)
class MorphologyScope:
    source_type: str
    language_stage: str | None
    morphology_profile: str | None


class MorphologyService:
    def __init__(
        self,
        *,
        pie_runner: PieRunner | None = None,
        pie_adapter: PieAdapter | None = None,
        tokenization_service: TokenizationService | None = None,
        job_progress_service: JobProgressService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.pie_runner = pie_runner or get_pie_runner()
        self.pie_adapter = pie_adapter or get_pie_adapter()
        self.tokenization_service = tokenization_service or get_tokenization_service()
        self.job_progress_service = job_progress_service or get_job_progress_service()

    def create_run(
        self,
        session: Session,
        *,
        user_id: UUID,
        request: MorphologyRunCreateRequest,
    ) -> MorphologyRun:
        analyzer = request.analyzer.strip().lower()
        if analyzer != "pie":
            raise ValueError("Only analyzer='pie' is currently supported.")

        document_id: UUID | None = None
        reference_source_id: UUID | None = None
        source_type: str
        result_resource_type: JobResultResourceType

        if request.document_id is not None:
            document = session.scalar(
                select(Document).where(
                    Document.id == request.document_id,
                    Document.user_id == user_id,
                )
            )
            if document is None:
                raise ValueError("Document not found.")
            document_id = document.id
            source_type = IMPORTED_BOOK_SOURCE_TYPE
            result_resource_type = JobResultResourceType.DOCUMENT
        else:
            reference_source = session.scalar(
                select(ReferenceSource).where(
                    ReferenceSource.id == request.reference_source_id,
                    ReferenceSource.user_id == str(user_id),
                )
            )
            if reference_source is None:
                raise ValueError("Reference source not found.")
            reference_source_id = reference_source.id
            source_type = REFERENCE_SOURCE_ANALYSIS_TYPE
            result_resource_type = JobResultResourceType.REFERENCE_SOURCE

        run = MorphologyRun(
            user_id=str(user_id),
            document_id=document_id,
            reference_source_id=reference_source_id,
            source_type=source_type,
            analyzer_provider="pie",
            analyzer_model_key=self.settings.pie_model_key,
            analyzer_version=self.pie_runner.resolve_analyzer_version(),
            status=MorphologyRunStatus.QUEUED,
            result_resource_type=result_resource_type,
            result_resource_id=str(document_id or reference_source_id),
        )
        session.add(run)
        session.flush()
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.MORPHOLOGY,
            job=run,
            stage_code="queued",
            progress_percent=0,
        )
        session.commit()
        session.refresh(run)
        return run

    def get_user_run(self, session: Session, *, user_id: UUID, run_id: UUID) -> MorphologyRun | None:
        return session.scalar(
            select(MorphologyRun).where(
                MorphologyRun.id == run_id,
                MorphologyRun.user_id == str(user_id),
            )
        )

    def build_run_read(self, run: MorphologyRun) -> MorphologyRunRead:
        return MorphologyRunRead.model_validate(run)

    def mark_run_failed(
        self,
        session: Session,
        *,
        run_id: UUID,
        error_message: str,
        error_code: str = "morphology_enqueue_failed",
        error_message_user: str | None = None,
    ) -> MorphologyRun:
        run = session.get(MorphologyRun, run_id)
        if run is None:
            raise ValueError(f"Morphology run {run_id} was not found.")
        run.status = MorphologyRunStatus.FAILED
        run.error_message = error_message
        run.error_code = error_code
        run.error_message_user = error_message_user or error_message
        run.next_steps = [
            "Retry the morphology run.",
            "Verify the PIE worker environment and model files.",
        ]
        self.job_progress_service.fail(
            session,
            job_kind=JobKind.MORPHOLOGY,
            job=run,
            message_user=run.error_message_user,
        )
        session.commit()
        session.refresh(run)
        return run

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
            run.progress_percent = 0
            run.items_processed = 0
            run.items_total = 0
            run.completed_count = 0
            run.skipped_count = 0
            run.failed_count = 0
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.MORPHOLOGY,
                job=run,
                stage_code="loading_scope",
                progress_percent=5,
            )

        with session_scope() as session:
            run = self._load_run(session, run_uuid)
            tokens, scope = self._load_scope_tokens(session, run)
            run.items_total = len(tokens)
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.MORPHOLOGY,
                job=run,
                stage_code="checking_eligibility",
                progress_percent=15,
                items_processed=0,
                items_total=len(tokens),
            )

        analyzer_version = self.pie_runner.resolve_analyzer_version()
        analysis_rows: list[MorphologyAnalysis] = []
        failure_for_run: str | None = None

        if not self._scope_is_eligible(scope):
            analysis_rows.extend(
                self._build_status_rows(
                    tokens,
                    status=MorphologyAnalysisStatus.SKIPPED,
                    analyzer_version=analyzer_version,
                    failure_reason="source_not_eligible",
                )
            )
            self._persist_rows(run_uuid, analysis_rows, run_status=MorphologyRunStatus.COMPLETED)
            return

        eligible_sequences: dict[str, list[MorphologyTokenInput]] = defaultdict(list)
        for token in tokens:
            reason = self._token_skip_reason(token)
            if reason is not None:
                analysis_rows.append(
                    self._build_status_row(
                        token,
                        status=MorphologyAnalysisStatus.SKIPPED,
                        analyzer_version=analyzer_version,
                        failure_reason=reason,
                    )
                )
                continue
            eligible_sequences[token.sequence_key].append(token)

        total_items = len(tokens)
        processed_items = len(analysis_rows)

        if not eligible_sequences:
            self._persist_rows(run_uuid, analysis_rows, run_status=MorphologyRunStatus.COMPLETED)
            return

        had_completed_prediction = False
        for sequence_batch in self._iter_sequence_batches(list(eligible_sequences.values())):
            batch_size = sum(len(sequence) for sequence in sequence_batch)
            try:
                raw_predictions = self.pie_runner.analyze_sequences(
                    [[token.token_surface for token in sequence] for sequence in sequence_batch]
                )
                for token_sequence, prediction_sequence in zip(sequence_batch, raw_predictions, strict=True):
                    for token, prediction in zip(token_sequence, prediction_sequence, strict=True):
                        adapted = self.pie_adapter.adapt_prediction(prediction)
                        if adapted.is_usable:
                            had_completed_prediction = True
                            analysis_rows.append(
                                MorphologyAnalysis(
                                    user_id=token.user_id,
                                    occurrence_id=token.occurrence_id,
                                    document_id=token.document_id,
                                    page_id=token.page_id,
                                    reference_source_id=token.reference_source_id,
                                    reference_entry_id=token.reference_entry_id,
                                    source_type=token.source_type,
                                    token_surface=token.token_surface,
                                    token_normalized=token.token_normalized,
                                    lemma=adapted.lemma,
                                    lemma_normalized=adapted.lemma_normalized,
                                    pos=adapted.pos,
                                    morph_features=adapted.morph_features,
                                    analyzer_provider="pie",
                                    analyzer_model_key=self.settings.pie_model_key,
                                    analyzer_version=analyzer_version,
                                    analysis_status=MorphologyAnalysisStatus.COMPLETED,
                                )
                            )
                        else:
                            analysis_rows.append(
                                self._build_status_row(
                                    token,
                                    status=MorphologyAnalysisStatus.FAILED,
                                    analyzer_version=analyzer_version,
                                    failure_reason="empty_prediction",
                                )
                            )
            except Exception as exc:
                if failure_for_run is None:
                    failure_for_run = str(exc)
                analysis_rows.extend(
                    self._build_status_rows(
                        [token for sequence in sequence_batch for token in sequence],
                        status=MorphologyAnalysisStatus.FAILED,
                        analyzer_version=analyzer_version,
                        failure_reason=str(exc),
                    )
                )

            processed_items += batch_size
            self._update_run_progress(
                run_uuid,
                processed_items=processed_items,
                total_items=total_items,
            )

        run_status = MorphologyRunStatus.COMPLETED
        if failure_for_run is not None and not had_completed_prediction:
            run_status = MorphologyRunStatus.FAILED
        self._persist_rows(
            run_uuid,
            analysis_rows,
            run_status=run_status,
            error_message=failure_for_run if run_status is MorphologyRunStatus.FAILED else None,
        )

    def get_document_summary(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
    ) -> MorphologySummaryResponse:
        document = session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.user_id == user_id,
            )
        )
        if document is None:
            raise ValueError("Document not found.")

        rows = list(
            session.scalars(
                select(MorphologyAnalysis).where(
                    MorphologyAnalysis.user_id == str(user_id),
                    MorphologyAnalysis.document_id == document_id,
                )
            )
        )
        analyzed_occurrence_count = len({row.occurrence_id for row in rows if row.occurrence_id is not None})
        completed_rows = [row for row in rows if row.analysis_status is MorphologyAnalysisStatus.COMPLETED]
        skipped_rows = [row for row in rows if row.analysis_status is MorphologyAnalysisStatus.SKIPPED]
        failed_rows = [row for row in rows if row.analysis_status is MorphologyAnalysisStatus.FAILED]
        distinct_lemma_count = len(
            {row.lemma_normalized for row in completed_rows if row.lemma_normalized}
        )
        return MorphologySummaryResponse(
            analyzed_occurrence_count=analyzed_occurrence_count,
            completed_count=len(completed_rows),
            skipped_count=len(skipped_rows),
            failed_count=len(failed_rows),
            distinct_lemma_count=distinct_lemma_count,
        )

    def get_word_morphology(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
    ) -> MorphologyWordResponse:
        normalized = normalize_token(normalized_form)
        if not normalized:
            raise ValueError("normalized_form must not be empty.")

        rows = list(
            session.scalars(
                select(MorphologyAnalysis).where(
                    MorphologyAnalysis.user_id == str(user_id),
                    MorphologyAnalysis.token_normalized == normalized,
                )
            )
        )
        completed_rows = [row for row in rows if row.analysis_status is MorphologyAnalysisStatus.COMPLETED]
        skipped_rows = [row for row in rows if row.analysis_status is MorphologyAnalysisStatus.SKIPPED]
        failed_rows = [row for row in rows if row.analysis_status is MorphologyAnalysisStatus.FAILED]

        lemma_counts: Counter[str] = Counter()
        pos_counts: Counter[str] = Counter()
        feature_counts: dict[str, Counter[str]] = defaultdict(Counter)
        for row in completed_rows:
            if row.lemma:
                lemma_counts[row.lemma] += 1
            elif row.lemma_normalized:
                lemma_counts[row.lemma_normalized] += 1
            if row.pos:
                pos_counts[row.pos] += 1
            for feature_name, feature_value in (row.morph_features or {}).items():
                values = feature_value if isinstance(feature_value, list) else [feature_value]
                for value in values:
                    feature_counts[feature_name][str(value)] += 1

        return MorphologyWordResponse(
            normalized_form=normalized,
            analyzed_occurrence_count=len({row.occurrence_id for row in rows if row.occurrence_id is not None}),
            completed_count=len(completed_rows),
            skipped_count=len(skipped_rows),
            failed_count=len(failed_rows),
            lemma_candidates=self._ordered_counts(lemma_counts),
            pos_distribution=self._ordered_counts(pos_counts),
            morph_feature_summaries={
                feature: self._ordered_counts(counter)
                for feature, counter in sorted(feature_counts.items())
            },
        )

    def get_word_evidence_summary(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
    ) -> MorphologyWordEvidenceSummary:
        word_summary = self.get_word_morphology(session, user_id=user_id, normalized_form=normalized_form)
        lemma_candidates = [candidate.value for candidate in word_summary.lemma_candidates]
        pos_candidates = [candidate.value for candidate in word_summary.pos_distribution]
        return MorphologyWordEvidenceSummary(
            morphology_available=word_summary.completed_count > 0,
            best_lemma=lemma_candidates[0] if lemma_candidates else None,
            lemma_candidates=lemma_candidates,
            pos_candidates=pos_candidates,
        )

    def _load_scope_tokens(
        self,
        session: Session,
        run: MorphologyRun,
    ) -> tuple[list[MorphologyTokenInput], MorphologyScope]:
        if run.document_id is not None:
            document = session.scalar(
                select(Document).where(
                    Document.id == run.document_id,
                    Document.user_id == UUID(run.user_id),
                )
            )
            if document is None:
                raise ValueError("Document not found for morphology run.")

            occurrences = list(
                session.scalars(
                    select(Occurrence)
                    .where(Occurrence.document_id == document.id)
                    .order_by(
                        Occurrence.page_number.asc(),
                        Occurrence.char_start.asc().nullsfirst(),
                        Occurrence.created_at.asc(),
                        Occurrence.id.asc(),
                    )
                )
            )
            tokens = [
                MorphologyTokenInput(
                    user_id=run.user_id,
                    source_type=IMPORTED_BOOK_SOURCE_TYPE,
                    sequence_key=f"page:{occurrence.page_id}",
                    token_surface=occurrence.token,
                    token_normalized=occurrence.normalized_token,
                    occurrence_id=occurrence.id,
                    document_id=occurrence.document_id,
                    page_id=occurrence.page_id,
                    has_armenian=occurrence.has_armenian,
                    has_latin=occurrence.has_latin,
                    has_digits=occurrence.has_digits,
                )
                for occurrence in occurrences
            ]
            scope = MorphologyScope(
                source_type=IMPORTED_BOOK_SOURCE_TYPE,
                language_stage=document.language_stage,
                morphology_profile=document.morphology_profile,
            )
            return tokens, scope

        source = session.scalar(
            select(ReferenceSource)
            .where(
                ReferenceSource.id == run.reference_source_id,
                ReferenceSource.user_id == run.user_id,
            )
        )
        if source is None:
            raise ValueError("Reference source not found for morphology run.")

        tokens: list[MorphologyTokenInput] = []
        entries = list(
            session.scalars(
                select(ReferenceEntry)
                .where(ReferenceEntry.source_id == source.id)
                .order_by(ReferenceEntry.created_at.asc(), ReferenceEntry.id.asc())
            )
        )
        for entry in entries:
            token_matches = self.tokenization_service.tokenize(entry.surface_form)
            for token_match in token_matches:
                classification = classify_token(token_match.token)
                tokens.append(
                    MorphologyTokenInput(
                        user_id=run.user_id,
                        source_type=REFERENCE_SOURCE_ANALYSIS_TYPE,
                        sequence_key=f"entry:{entry.id}",
                        token_surface=token_match.token,
                        token_normalized=token_match.normalized_token,
                        reference_source_id=source.id,
                        reference_entry_id=entry.id,
                        has_armenian=classification.has_armenian,
                        has_latin=classification.has_latin,
                        has_digits=classification.has_digits,
                    )
                )
        scope = MorphologyScope(
            source_type=REFERENCE_SOURCE_ANALYSIS_TYPE,
            language_stage=source.language_stage,
            morphology_profile=source.morphology_profile,
        )
        return tokens, scope

    def _scope_is_eligible(self, scope: MorphologyScope) -> bool:
        profile = (scope.morphology_profile or "").strip().lower()
        if profile and profile == f"{self.settings.pie_model_key.lower()}_pie":
            return True
        if profile and profile != f"{self.settings.pie_model_key.lower()}_pie":
            return False
        if not self.settings.pie_run_only_for_classical:
            return True
        return (scope.language_stage or "").strip().lower() == "classical"

    @staticmethod
    def _token_skip_reason(token: MorphologyTokenInput) -> str | None:
        if not token.token_normalized:
            return "token_normalized_empty"
        if not token.has_armenian:
            return "token_not_armenian"
        if token.has_latin or token.has_digits:
            return "token_not_supported_for_pie"
        return None

    def _iter_sequence_batches(
        self,
        sequences: list[list[MorphologyTokenInput]],
    ) -> list[list[list[MorphologyTokenInput]]]:
        batches: list[list[list[MorphologyTokenInput]]] = []
        current_batch: list[list[MorphologyTokenInput]] = []
        current_token_count = 0

        for sequence in sequences:
            sequence_token_count = len(sequence)
            exceeds_batch_size = len(current_batch) >= self.settings.pie_batch_size
            exceeds_token_limit = current_token_count + sequence_token_count > self.settings.pie_max_tokens_per_batch
            if current_batch and (exceeds_batch_size or exceeds_token_limit):
                batches.append(current_batch)
                current_batch = []
                current_token_count = 0
            current_batch.append(sequence)
            current_token_count += sequence_token_count

        if current_batch:
            batches.append(current_batch)
        return batches

    def _build_status_rows(
        self,
        tokens: list[MorphologyTokenInput],
        *,
        status: MorphologyAnalysisStatus,
        analyzer_version: str | None,
        failure_reason: str | None,
    ) -> list[MorphologyAnalysis]:
        return [
            self._build_status_row(
                token,
                status=status,
                analyzer_version=analyzer_version,
                failure_reason=failure_reason,
            )
            for token in tokens
        ]

    def _build_status_row(
        self,
        token: MorphologyTokenInput,
        *,
        status: MorphologyAnalysisStatus,
        analyzer_version: str | None,
        failure_reason: str | None,
    ) -> MorphologyAnalysis:
        return MorphologyAnalysis(
            user_id=token.user_id,
            occurrence_id=token.occurrence_id,
            document_id=token.document_id,
            page_id=token.page_id,
            reference_source_id=token.reference_source_id,
            reference_entry_id=token.reference_entry_id,
            source_type=token.source_type,
            token_surface=token.token_surface,
            token_normalized=token.token_normalized,
            analyzer_provider="pie",
            analyzer_model_key=self.settings.pie_model_key,
            analyzer_version=analyzer_version,
            analysis_status=status,
            failure_reason=failure_reason,
        )

    def _persist_rows(
        self,
        run_id: UUID,
        rows: list[MorphologyAnalysis],
        *,
        run_status: MorphologyRunStatus,
        error_message: str | None = None,
    ) -> None:
        with session_scope() as session:
            run = self._load_run(session, run_id)
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.MORPHOLOGY,
                job=run,
                stage_code="saving_results",
                progress_percent=90,
                items_processed=run.items_total,
                items_total=run.items_total,
            )
            self._delete_existing_scope_rows(session, run)
            if rows:
                session.add_all(rows)

            completed_count = sum(1 for row in rows if row.analysis_status is MorphologyAnalysisStatus.COMPLETED)
            skipped_count = sum(1 for row in rows if row.analysis_status is MorphologyAnalysisStatus.SKIPPED)
            failed_count = sum(1 for row in rows if row.analysis_status is MorphologyAnalysisStatus.FAILED)

            run.completed_count = completed_count
            run.skipped_count = skipped_count
            run.failed_count = failed_count
            run.items_processed = len(rows)
            run.items_total = len(rows)

            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.MORPHOLOGY,
                job=run,
                stage_code="finalizing",
                progress_percent=97,
                items_processed=len(rows),
                items_total=len(rows),
            )

            if run_status is MorphologyRunStatus.FAILED:
                run.status = MorphologyRunStatus.FAILED
                run.error_message = error_message
                run.error_code = "morphology_run_failed"
                run.error_message_user = error_message or "Morphology analysis failed."
                run.next_steps = [
                    "Check that the PIE model files are available to the worker.",
                    "Retry the morphology run once the environment is fixed.",
                ]
                self.job_progress_service.fail(
                    session,
                    job_kind=JobKind.MORPHOLOGY,
                    job=run,
                    message_user=run.error_message_user,
                )
            else:
                run.status = MorphologyRunStatus.COMPLETED
                self.job_progress_service.complete(
                    session,
                    job_kind=JobKind.MORPHOLOGY,
                    job=run,
                )

    def _delete_existing_scope_rows(self, session: Session, run: MorphologyRun) -> None:
        delete_stmt = delete(MorphologyAnalysis).where(
            MorphologyAnalysis.user_id == run.user_id,
            MorphologyAnalysis.analyzer_provider == run.analyzer_provider,
            MorphologyAnalysis.analyzer_model_key == run.analyzer_model_key,
            MorphologyAnalysis.source_type == run.source_type,
        )
        if run.document_id is not None:
            delete_stmt = delete_stmt.where(MorphologyAnalysis.document_id == run.document_id)
        if run.reference_source_id is not None:
            delete_stmt = delete_stmt.where(MorphologyAnalysis.reference_source_id == run.reference_source_id)
        session.execute(delete_stmt)

    def _update_run_progress(self, run_id: UUID, *, processed_items: int, total_items: int) -> None:
        with session_scope() as session:
            run = self._load_run(session, run_id)
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.MORPHOLOGY,
                job=run,
                stage_code="running_pie",
                progress_percent=self.job_progress_service.ranged_progress(
                    processed_items,
                    total_items,
                    start_percent=20,
                    end_percent=82,
                ),
                items_processed=processed_items,
                items_total=total_items,
                append_event=False,
            )

    @staticmethod
    def _ordered_counts(counter: Counter[str]) -> list[MorphologyCount]:
        return [
            MorphologyCount(value=value, count=count)
            for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        ]

    @staticmethod
    def _load_run(session: Session, run_id: UUID) -> MorphologyRun:
        run = session.scalar(
            select(MorphologyRun)
            .options(
                joinedload(MorphologyRun.document),
                joinedload(MorphologyRun.reference_source),
            )
            .where(MorphologyRun.id == run_id)
        )
        if run is None:
            raise ValueError(f"Morphology run {run_id} was not found.")
        return run


def get_morphology_service() -> MorphologyService:
    return MorphologyService()
