from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from decimal import Decimal
import logging
import time
from uuid import UUID

from sqlalchemy import delete, func, inspect, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import session_scope
from app.db.models import (
    JobKind,
    JobResultResourceType,
    Document,
    Lexeme,
    LexemeForm,
    Occurrence,
    ReferenceEntry,
    ReferenceImportMethod,
    ReferenceMatchingDirection,
    ReferenceMatch,
    ReferenceMatchRun,
    ReferenceMatchRunResult,
    ReferenceMatchRunResultMatch,
    ReferenceMatchRunScope,
    ReferenceMatchRunStatus,
    ReferenceMatchStatus,
    ReferenceMatchTargetScope,
    ReferenceMatchTargetType,
    ReferenceMatchType,
    ReferenceSource,
)
from app.schemas.lexicon import LexiconGroupView
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.reference_status_service import (
    ReferenceEntryStatusSummary,
    ReferenceStatusService,
    StoredReferenceSummary,
    get_reference_status_service,
)
from app.services.retry_errors import RetryStartError
from app.schemas.reference import (
    ReferenceMatchBest,
    ReferenceMatchDetail,
    ReferenceMatchingBookContext,
    ReferenceMatchingLexemeSummary,
    ReferenceMatchRunCreateRequest,
    ReferenceMatchRunEntryResultDetail,
    ReferenceMatchRunEntryResultSummary,
    ReferenceMatchRunEntryResultScopeFilter,
    ReferenceMatchRunEntrySourceDetail,
    ReferenceMatchRunDetail,
    ReferenceMatchRunResultDetail,
    ReferenceMatchRunResultSummary,
    ReferenceMatchRunResultTargetTypeFilter,
    ReferenceMatchRunSummary,
    ReferenceSourceEntrySummary,
    ReferenceStatusFilter,
    ReferenceTargetMatchesResponse,
)
from app.services.lexicon_service import LexiconService
from app.services.reference_source_service import ReferenceSourceService, get_reference_source_service
from app.utils.text_normalization import normalize_token

try:  # pragma: no cover - optional runtime acceleration
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover - fallback keeps MVP working without optional wheel
    fuzz = None


MATCH_TYPE_PRIORITY = {
    ReferenceMatchType.EXACT: 0,
    ReferenceMatchType.NORMALIZED: 1,
    ReferenceMatchType.FUZZY: 2,
}

REFERENCE_TABLES = (
    "reference_sources",
    "reference_entries",
    "reference_match_runs",
    "reference_matches",
    "reference_match_run_results",
    "reference_match_run_result_matches",
)
REFERENCE_REQUIRED_COLUMNS = {
    "reference_sources": {
        "id",
        "display_name",
        "last_import_method",
        "last_import_warning",
    },
    "reference_entries": {"id", "source_id", "surface_form", "normalized_form"},
    "reference_match_runs": {
        "id",
        "user_id",
        "matching_direction",
        "run_scope",
        "source_id",
        "retry_of_job_id",
        "target_scope",
        "requested_view",
        "include_fuzzy",
        "status",
        "retry_count",
        "can_retry",
        "last_retried_at",
        "unmatched_items",
        "current_stage_code",
        "current_stage_label",
        "progress_percent",
    },
    "reference_matches": {"id", "user_id", "target_type", "target_key", "source_id", "reference_entry_id"},
    "reference_match_run_results": {
        "id",
        "run_id",
        "user_id",
        "matching_direction",
        "source_id",
        "reference_entry_id",
        "target_type",
        "target_key",
        "target_label",
        "normalized_form",
        "match_status",
        "match_count",
        "exists_in_lexicon",
        "matching_lexeme_count",
        "found_in_books",
        "matching_book_occurrence_count",
    },
    "reference_match_run_result_matches": {
        "id",
        "result_id",
        "run_id",
        "user_id",
        "source_id",
        "reference_entry_id",
        "match_type",
    },
}


@dataclass(slots=True)
class LiveReferenceMatch:
    source_id: UUID
    source_display_name: str
    reference_entry_id: UUID
    surface_form: str
    normalized_form: str
    match_type: ReferenceMatchType
    match_score: float | None
    source_import_method: ReferenceImportMethod | None
    source_warning: str | None
    metadata_json: dict[str, object] | None


@dataclass(slots=True)
class ReferenceCatalogEntry:
    source_id: UUID
    source_display_name: str
    reference_entry_id: UUID
    surface_form: str
    normalized_form: str
    source_import_method: ReferenceImportMethod | None
    source_warning: str | None
    metadata_json: dict[str, object] | None


@dataclass(slots=True)
class ReferenceCatalog:
    by_surface: dict[str, list[ReferenceCatalogEntry]] = field(default_factory=dict)
    by_normalized: dict[str, list[ReferenceCatalogEntry]] = field(default_factory=dict)
    fuzzy_buckets: dict[tuple[int, str], list[ReferenceCatalogEntry]] = field(default_factory=dict)


@dataclass(slots=True)
class RunTargetRecord:
    target_type: ReferenceMatchTargetType
    target_key: str
    target_label: str
    target_values: list[str]
    related_resource_type: ReferenceMatchTargetType
    related_resource_id: str


@dataclass(slots=True)
class RunResultStats:
    matched_items: int = 0
    unmatched_items: int = 0
    exact_match_count: int = 0
    normalized_match_count: int = 0
    fuzzy_match_count: int = 0


@dataclass(slots=True)
class ReferenceEntryDocumentEvidence:
    occurrence_count: int = 0
    contexts: list[ReferenceMatchingBookContext] = field(default_factory=list)


class ReferenceSchemaNotReadyError(RuntimeError):
    pass


logger = logging.getLogger(__name__)
PROGRESS_LOG_EVERY = 25
MATCH_QUERY_CHUNK_SIZE = 500
MATCH_INSERT_CHUNK_SIZE = 500


class ReferenceMatchingService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        lexicon_service: LexiconService | None = None,
        reference_source_service: ReferenceSourceService | None = None,
        job_progress_service: JobProgressService | None = None,
        reference_status_service: ReferenceStatusService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.lexicon_service = lexicon_service or LexiconService(reference_matching_service=self)
        self.reference_source_service = reference_source_service or get_reference_source_service()
        self.job_progress_service = job_progress_service or get_job_progress_service()
        self.reference_status_service = reference_status_service or get_reference_status_service()

    def match_group(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        allow_fuzzy: bool = True,
    ) -> ReferenceTargetMatchesResponse:
        self.ensure_reference_schema(session)
        self.reference_source_service.ensure_default_source(session, user_id=user_id)
        matches = self._find_matches_for_values(
            session,
            user_id=user_id,
            target_values=[normalized_form],
            allow_fuzzy=allow_fuzzy,
        )
        return ReferenceTargetMatchesResponse(
            target_type=ReferenceMatchTargetType.LEXICON_GROUP,
            target_key=normalized_form,
            has_match=bool(matches),
            matches=[self._to_match_detail(match) for match in matches],
        )

    def match_lexeme(
        self,
        session: Session,
        *,
        user_id: UUID,
        lexeme: Lexeme,
        allow_fuzzy: bool = True,
    ) -> ReferenceTargetMatchesResponse:
        self.ensure_reference_schema(session)
        self.reference_source_service.ensure_default_source(session, user_id=user_id)
        target_values = self._lexeme_target_values(session, user_id=user_id, lexeme=lexeme)
        matches = self._find_matches_for_values(
            session,
            user_id=user_id,
            target_values=target_values,
            allow_fuzzy=allow_fuzzy,
        )
        return ReferenceTargetMatchesResponse(
            target_type=ReferenceMatchTargetType.LEXEME,
            target_key=str(lexeme.id),
            has_match=bool(matches),
            matches=[self._to_match_detail(match) for match in matches],
        )

    def create_run(
        self,
        session: Session,
        *,
        user_id: UUID,
        request: ReferenceMatchRunCreateRequest,
    ) -> ReferenceMatchRunDetail:
        self.ensure_reference_schema(session)
        source = None
        if request.matching_direction is ReferenceMatchingDirection.SOURCE_TO_INTERNAL:
            if request.source_id is None:
                raise ValueError("source_id is required for source_to_internal matching.")
            source = self.reference_source_service.get_user_source(
                session,
                user_id=user_id,
                source_id=request.source_id,
            )
            if source is None:
                raise ValueError("Reference source not found.")
        else:
            self.reference_source_service.ensure_default_source(session, user_id=user_id)
            self._parse_group_view(request.view)

        run = ReferenceMatchRun(
            user_id=str(user_id),
            matching_direction=request.matching_direction,
            run_scope=request.run_scope,
            source_id=source.id if source is not None else None,
            target_scope=(
                request.target_scope
                if request.matching_direction is ReferenceMatchingDirection.SOURCE_TO_INTERNAL
                else None
            ),
            requested_view=request.view,
            include_fuzzy=request.include_fuzzy,
            status=ReferenceMatchRunStatus.QUEUED,
            progress_percent=0,
            result_resource_type=JobResultResourceType.REFERENCE_MATCH_RUN,
        )
        session.add(run)
        session.flush()
        run.result_resource_id = str(run.id)
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=run,
            stage_code="queued",
            progress_percent=0,
        )
        session.commit()
        session.refresh(run)
        return ReferenceMatchRunDetail.model_validate(run)

    def create_retry_run(self, session: Session, *, user_id: UUID, failed_run_id: UUID) -> ReferenceMatchRun:
        self.ensure_reference_schema(session)
        run = self.get_user_run(session, user_id=user_id, run_id=failed_run_id)
        if run is None:
            raise RetryStartError(status_code=404, message="Reference matching run not found.")
        if run.status is not ReferenceMatchRunStatus.FAILED:
            raise RetryStartError(status_code=409, message="Only failed reference matching runs can be retried.")
        if not run.can_retry:
            raise RetryStartError(status_code=409, message="This reference matching run cannot be retried.")

        existing_active_retry = session.scalar(
            select(ReferenceMatchRun.id).where(
                ReferenceMatchRun.retry_of_job_id == run.id,
                ReferenceMatchRun.status.in_([ReferenceMatchRunStatus.QUEUED, ReferenceMatchRunStatus.RUNNING]),
            )
        )
        if existing_active_retry is not None:
            raise RetryStartError(status_code=409, message="A retry is already running for this reference matching run.")

        retry_run = ReferenceMatchRun(
            user_id=run.user_id,
            matching_direction=run.matching_direction,
            run_scope=run.run_scope,
            source_id=run.source_id,
            retry_of_job_id=run.id,
            target_scope=run.target_scope,
            requested_view=run.requested_view,
            include_fuzzy=run.include_fuzzy,
            status=ReferenceMatchRunStatus.QUEUED,
            progress_percent=0,
            retry_count=run.retry_count + 1,
            can_retry=True,
            result_resource_type=JobResultResourceType.REFERENCE_MATCH_RUN,
        )
        session.add(retry_run)
        session.flush()
        retry_run.result_resource_id = str(retry_run.id)
        run.last_retried_at = datetime.now(timezone.utc)
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=retry_run,
            stage_code="queued",
            progress_percent=0,
        )
        session.commit()
        session.refresh(retry_run)
        return retry_run

    def list_runs(
        self,
        session: Session,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[ReferenceMatchRunSummary], int]:
        self.ensure_reference_schema(session)
        filters = [ReferenceMatchRun.user_id == str(user_id)]
        total = session.scalar(select(func.count(ReferenceMatchRun.id)).where(*filters)) or 0
        runs = list(
            session.scalars(
                select(ReferenceMatchRun)
                .where(*filters)
                .order_by(ReferenceMatchRun.created_at.desc(), ReferenceMatchRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        return [ReferenceMatchRunSummary.model_validate(run) for run in runs], total

    def get_user_run(self, session: Session, *, user_id: UUID, run_id: UUID) -> ReferenceMatchRun | None:
        self.ensure_reference_schema(session)
        return session.scalar(
            select(ReferenceMatchRun).where(
                ReferenceMatchRun.id == run_id,
                ReferenceMatchRun.user_id == str(user_id),
            )
        )

    def get_run_detail(self, session: Session, *, user_id: UUID, run_id: UUID) -> ReferenceMatchRunDetail | None:
        run = self.get_user_run(session, user_id=user_id, run_id=run_id)
        if run is None:
            return None
        return ReferenceMatchRunDetail.model_validate(run)

    def list_run_results(
        self,
        session: Session,
        *,
        user_id: UUID,
        run_id: UUID,
        match_status: ReferenceStatusFilter,
        target_type: ReferenceMatchRunResultTargetTypeFilter,
        search: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[ReferenceMatchRunResultSummary], int]:
        self.ensure_reference_schema(session)
        filters = [
            ReferenceMatchRunResult.user_id == str(user_id),
            ReferenceMatchRunResult.run_id == run_id,
        ]
        if match_status is ReferenceStatusFilter.MATCHED:
            filters.append(ReferenceMatchRunResult.match_status == ReferenceMatchStatus.MATCHED)
        elif match_status is ReferenceStatusFilter.UNMATCHED:
            filters.append(ReferenceMatchRunResult.match_status == ReferenceMatchStatus.UNMATCHED)

        if target_type is ReferenceMatchRunResultTargetTypeFilter.LEXICON_GROUP:
            filters.append(ReferenceMatchRunResult.target_type == ReferenceMatchTargetType.LEXICON_GROUP)
        elif target_type is ReferenceMatchRunResultTargetTypeFilter.LEXEME:
            filters.append(ReferenceMatchRunResult.target_type == ReferenceMatchTargetType.LEXEME)
        else:
            filters.append(
                ReferenceMatchRunResult.target_type.in_(
                    [ReferenceMatchTargetType.LEXICON_GROUP, ReferenceMatchTargetType.LEXEME]
                )
            )

        normalized_search = search.strip().lower() if search and search.strip() else None
        if normalized_search:
            filters.append(func.lower(ReferenceMatchRunResult.target_label).contains(normalized_search))

        total = session.scalar(select(func.count(ReferenceMatchRunResult.id)).where(*filters)) or 0
        rows = list(
            session.scalars(
                select(ReferenceMatchRunResult)
                .where(*filters)
                .order_by(
                    ReferenceMatchRunResult.target_type.asc(),
                    ReferenceMatchRunResult.target_label.asc(),
                    ReferenceMatchRunResult.id.asc(),
                )
                .limit(limit)
                .offset(offset)
            )
        )
        return [self._to_run_result_summary(row) for row in rows], total

    def list_run_reference_entry_results(
        self,
        session: Session,
        *,
        user_id: UUID,
        run_id: UUID,
        search: str | None,
        match_status: ReferenceStatusFilter,
        target_scope: ReferenceMatchRunEntryResultScopeFilter,
        limit: int,
        offset: int,
    ) -> tuple[list[ReferenceMatchRunEntryResultSummary], int]:
        self.ensure_reference_schema(session)
        run = self.get_user_run(session, user_id=user_id, run_id=run_id)
        if run is None:
            return [], 0
        if run.matching_direction is not ReferenceMatchingDirection.SOURCE_TO_INTERNAL:
            return [], 0

        filters: list[object] = [
            ReferenceMatchRunResult.user_id == str(user_id),
            ReferenceMatchRunResult.run_id == run_id,
            ReferenceMatchRunResult.matching_direction == ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
            ReferenceMatchRunResult.target_type == ReferenceMatchTargetType.REFERENCE_ENTRY,
        ]
        if match_status is ReferenceStatusFilter.MATCHED:
            filters.append(ReferenceMatchRunResult.match_status == ReferenceMatchStatus.MATCHED)
        elif match_status is ReferenceStatusFilter.UNMATCHED:
            filters.append(ReferenceMatchRunResult.match_status == ReferenceMatchStatus.UNMATCHED)

        if target_scope is ReferenceMatchRunEntryResultScopeFilter.LEXICON_ONLY:
            filters.append(ReferenceMatchRunResult.exists_in_lexicon.is_(True))
        elif target_scope is ReferenceMatchRunEntryResultScopeFilter.BOOKS_ONLY:
            filters.append(ReferenceMatchRunResult.found_in_books.is_(True))

        raw_search = search.strip() if search else None
        normalized_search = normalize_token(raw_search) if raw_search else None
        if raw_search:
            lowered_search = raw_search.lower()
            search_filter = func.lower(ReferenceMatchRunResult.target_label).contains(lowered_search)
            if normalized_search:
                search_filter = search_filter | func.lower(ReferenceMatchRunResult.normalized_form).contains(
                    normalized_search
                )
            filters.append(search_filter)

        total = session.scalar(select(func.count(ReferenceMatchRunResult.id)).where(*filters)) or 0
        rows = list(
            session.scalars(
                select(ReferenceMatchRunResult)
                .where(*filters)
                .order_by(
                    ReferenceMatchRunResult.target_label.asc(),
                    ReferenceMatchRunResult.normalized_form.asc(),
                    ReferenceMatchRunResult.id.asc(),
                )
                .limit(limit)
                .offset(offset)
            )
        )
        return [self._to_reference_entry_run_result_summary(row) for row in rows], total

    def get_user_run_result(
        self,
        session: Session,
        *,
        user_id: UUID,
        run_id: UUID,
        result_id: UUID,
    ) -> ReferenceMatchRunResult | None:
        self.ensure_reference_schema(session)
        return session.scalar(
            select(ReferenceMatchRunResult).where(
                ReferenceMatchRunResult.id == result_id,
                ReferenceMatchRunResult.run_id == run_id,
                ReferenceMatchRunResult.user_id == str(user_id),
            )
        )

    def get_run_result_detail(
        self,
        session: Session,
        *,
        user_id: UUID,
        run_id: UUID,
        result_id: UUID,
    ) -> ReferenceMatchRunResultDetail | None:
        result = self.get_user_run_result(
            session,
            user_id=user_id,
            run_id=run_id,
            result_id=result_id,
        )
        if result is None:
            return None
        matches = list(
            session.scalars(
                select(ReferenceMatchRunResultMatch).where(
                    ReferenceMatchRunResultMatch.user_id == str(user_id),
                    ReferenceMatchRunResultMatch.run_id == run_id,
                    ReferenceMatchRunResultMatch.result_id == result_id,
                )
            )
        )
        ordered_matches = sorted(matches, key=self._run_result_match_sort_key)
        return ReferenceMatchRunResultDetail(
            **self._to_run_result_summary(result).model_dump(),
            matches=[self._to_stored_run_match_detail(match) for match in ordered_matches],
        )

    def get_run_reference_entry_result_detail(
        self,
        session: Session,
        *,
        user_id: UUID,
        run_id: UUID,
        result_id: UUID,
    ) -> ReferenceMatchRunEntryResultDetail | None:
        self.ensure_reference_schema(session)
        run = self.get_user_run(session, user_id=user_id, run_id=run_id)
        if run is None or run.matching_direction is not ReferenceMatchingDirection.SOURCE_TO_INTERNAL:
            return None
        result = session.scalar(
            select(ReferenceMatchRunResult).where(
                ReferenceMatchRunResult.id == result_id,
                ReferenceMatchRunResult.run_id == run_id,
                ReferenceMatchRunResult.user_id == str(user_id),
                ReferenceMatchRunResult.matching_direction == ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
                ReferenceMatchRunResult.target_type == ReferenceMatchTargetType.REFERENCE_ENTRY,
            )
        )
        if result is None or result.reference_entry_id is None or result.source_id is None:
            return None

        row = session.execute(
            select(ReferenceEntry, ReferenceSource)
            .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
            .where(
                ReferenceEntry.id == result.reference_entry_id,
                ReferenceSource.id == result.source_id,
                ReferenceSource.user_id == str(user_id),
                ReferenceSource.is_active.is_(True),
            )
        ).first()
        if row is None:
            return None

        entry = row.ReferenceEntry
        source = row.ReferenceSource
        lexeme_map = self._matching_lexeme_map(session, user_id=user_id, normalized_forms=[entry.normalized_form])
        matching_lexemes = lexeme_map.get(entry.normalized_form, [])
        document_evidence = self._document_evidence_map(
            session,
            user_id=user_id,
            normalized_forms=[entry.normalized_form],
            sample_limit_per_form=None,
        ).get(entry.normalized_form)
        summary = self._to_reference_entry_run_result_summary(result)
        return ReferenceMatchRunEntryResultDetail(
            **summary.model_dump(),
            source_entry=ReferenceMatchRunEntrySourceDetail(
                reference_entry_id=entry.id,
                surface_form=result.target_label,
                normalized_form=result.normalized_form,
                source_id=source.id,
                source_display_name=source.display_name,
                source_description=source.description,
                source_import_method=result.source_import_method,
                source_warning=result.source_warning,
                source_metadata=entry.metadata_json,
            ),
            matching_lexemes=[
                ReferenceMatchingLexemeSummary(
                    lexeme_id=lexeme.id,
                    canonical_form=lexeme.canonical_form,
                    canonical_normalized_form=lexeme.canonical_normalized_form,
                )
                for lexeme in matching_lexemes
            ],
            book_evidence=list(document_evidence.contexts if document_evidence is not None else []),
        )

    def list_source_entries(
        self,
        session: Session,
        *,
        user_id: UUID,
        source_id: UUID,
        search: str | None,
        match_status: ReferenceStatusFilter,
        limit: int,
        offset: int,
    ) -> tuple[list[ReferenceSourceEntrySummary], int]:
        self.ensure_reference_schema(session)
        filters: list[object] = [
            ReferenceEntry.source_id == source_id,
        ]
        raw_search = search.strip() if search else None
        normalized_search = normalize_token(raw_search) if raw_search else None
        if raw_search:
            lowered_search = raw_search.lower()
            search_filter = func.lower(ReferenceEntry.surface_form).contains(lowered_search)
            if normalized_search:
                search_filter = search_filter | func.lower(ReferenceEntry.normalized_form).contains(normalized_search)
            filters.append(search_filter)

        matched_entry_ids = self.reference_status_service.matched_reference_entry_ids_subquery(
            user_id=user_id,
            source_id=source_id,
        )
        if match_status is ReferenceStatusFilter.MATCHED:
            filters.append(ReferenceEntry.id.in_(matched_entry_ids))
        elif match_status is ReferenceStatusFilter.UNMATCHED:
            filters.append(~ReferenceEntry.id.in_(matched_entry_ids))

        total = session.scalar(select(func.count(ReferenceEntry.id)).where(*filters)) or 0
        rows = list(
            session.scalars(
                select(ReferenceEntry)
                .where(*filters)
                .order_by(ReferenceEntry.normalized_form.asc(), ReferenceEntry.surface_form.asc(), ReferenceEntry.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        if not rows:
            return [], total

        result_map = self.reference_status_service.reference_entry_summary_map(
            session,
            user_id=user_id,
            reference_entry_ids=[row.id for row in rows],
        )

        items: list[ReferenceSourceEntrySummary] = []
        for entry in rows:
            result = result_map.get(entry.id)
            items.append(
                ReferenceSourceEntrySummary(
                    reference_entry_id=entry.id,
                    surface_form=entry.surface_form,
                    normalized_form=entry.normalized_form,
                    source_import_method=result.source_import_method if result is not None else None,
                    source_warning=result.source_warning if result is not None else None,
                    latest_match_status=result.latest_match_status if result is not None else None,
                    latest_match_count=result.reference_match_count if result is not None else None,
                    exists_in_lexicon=result.exists_in_lexicon if result is not None else None,
                    found_in_books=result.found_in_books if result is not None else None,
                    best_lexeme_id=result.best_lexeme_id if result is not None else None,
                    best_lexeme_canonical_form=result.best_lexeme_canonical_form if result is not None else None,
                    best_document_id=result.best_document_id if result is not None else None,
                    best_document_title=result.best_document_title if result is not None else None,
                    best_page_number=result.best_page_number if result is not None else None,
                    best_context_snippet=result.best_context_snippet if result is not None else None,
                    created_at=entry.created_at,
                    updated_at=entry.updated_at,
                )
            )
        return items, total

    def mark_run_failed(self, session: Session, *, run_id: UUID, error_message: str) -> None:
        if not self.reference_schema_available(session):
            return
        run = session.get(ReferenceMatchRun, run_id)
        if run is None:
            return
        self._clear_run_results(session, run_id=run_id)
        run.status = ReferenceMatchRunStatus.FAILED
        run.error_code = "reference_matching_failed"
        run.error_message = error_message
        run.error_message_user = "The reference matching run could not be completed."
        run.next_steps = [
            "Try the matching run again.",
            "If it fails again, review your reference sources and try later.",
        ]
        run.can_retry = True
        self.job_progress_service.fail(session, job_kind=JobKind.REFERENCE_MATCHING, job=run)
        session.commit()

    def process_run(self, run_id: str, *, view: str = "candidates", include_fuzzy: bool = False) -> None:
        run_uuid = UUID(run_id)
        logger.info(
            "Processing reference matching run run_id=%s view=%s include_fuzzy=%s",
            run_uuid,
            view,
            include_fuzzy,
        )
        try:
            with session_scope() as session:
                self.ensure_reference_schema(session)
                run = session.get(ReferenceMatchRun, run_uuid)
                if run is None:
                    raise ValueError("Reference match run not found.")
                resolved_view: str | LexiconGroupView = view
                if run.matching_direction is ReferenceMatchingDirection.INTERNAL_TO_REFERENCE:
                    resolved_view = self._parse_group_view(view)
                self.process_run_in_session(
                    session,
                    run_id=run_uuid,
                    view=resolved_view,
                    include_fuzzy=include_fuzzy,
                )
        except Exception as exc:
            with session_scope() as session:
                run = session.get(ReferenceMatchRun, run_uuid)
                if run is not None:
                    self._clear_run_results(session, run_id=run_uuid)
                    run.status = ReferenceMatchRunStatus.FAILED
                    run.error_code = "reference_matching_failed"
                    run.error_message = str(exc)
                    run.error_message_user = "The reference matching run could not be completed."
                    run.next_steps = [
                        "Try the matching run again.",
                        "If it fails again, review your reference sources and try later.",
                    ]
                    run.can_retry = True
                    self.job_progress_service.fail(session, job_kind=JobKind.REFERENCE_MATCHING, job=run)
            logger.exception("Reference matching run failed run_id=%s", run_uuid)
            raise
        logger.info("Reference matching run completed run_id=%s", run_uuid)

    def process_run_in_session(
        self,
        session: Session,
        *,
        run_id: UUID,
        view: str | LexiconGroupView = "candidates",
        include_fuzzy: bool = False,
    ) -> None:
        self.ensure_reference_schema(session)
        run = session.get(ReferenceMatchRun, run_id)
        if run is None:
            raise ValueError("Reference match run not found.")
        if run.matching_direction is ReferenceMatchingDirection.SOURCE_TO_INTERNAL:
            self._process_source_to_internal_run_in_session(
                session,
                run=run,
                include_fuzzy=include_fuzzy,
            )
            return

        group_view = view if isinstance(view, LexiconGroupView) else self._parse_group_view(view)
        self._process_internal_to_reference_run_in_session(
            session,
            run=run,
            view=group_view,
            include_fuzzy=include_fuzzy,
        )

    def _process_internal_to_reference_run_in_session(
        self,
        session: Session,
        *,
        run: ReferenceMatchRun,
        view: LexiconGroupView,
        include_fuzzy: bool,
    ) -> None:
        group_view = view

        started_monotonic = time.monotonic()
        run.status = ReferenceMatchRunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc)
        run.finished_at = None
        run.error_message = None
        run.error_code = None
        run.error_message_user = None
        run.next_steps = None
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=run,
            stage_code="loading_targets",
        )
        session.flush()

        user_id = UUID(run.user_id)
        self.reference_source_service.ensure_default_source(session, user_id=user_id)
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=run,
            stage_code="loading_reference_sources",
            progress_percent=20,
        )
        catalog = self._build_reference_catalog(session, user_id=user_id)
        group_target_records = self._collect_group_target_records(
            session,
            user_id=user_id,
            view=group_view,
        ) if run.run_scope in {ReferenceMatchRunScope.LEXICON_GROUPS, ReferenceMatchRunScope.ALL} else []
        lexeme_target_records = self._collect_lexeme_target_records(session, user_id=user_id) if run.run_scope in {
            ReferenceMatchRunScope.LEXEMES,
            ReferenceMatchRunScope.ALL,
        } else []

        matched_items = 0
        unmatched_items = 0
        exact_match_count = 0
        normalized_match_count = 0
        fuzzy_match_count = 0
        total_items = len(group_target_records) + len(lexeme_target_records)
        run.total_items = total_items
        run.matched_items = 0
        run.unmatched_items = 0
        run.exact_match_count = 0
        run.normalized_match_count = 0
        run.fuzzy_match_count = 0
        run.items_total = total_items
        run.items_processed = 0
        self._clear_run_results(session, run_id=run.id)
        session.flush()

        logger.info(
            "Reference matching run started run_id=%s user_id=%s scope=%s groups=%s lexemes=%s total=%s include_fuzzy=%s reference_entries=%s",
            run.id,
            run.user_id,
            run.run_scope.value,
            len(group_target_records),
            len(lexeme_target_records),
            total_items,
            include_fuzzy,
            sum(len(entries) for entries in catalog.by_normalized.values()),
        )

        processed_items = 0
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=run,
            stage_code="running_exact_match",
            progress_percent=35,
            items_processed=0,
            items_total=total_items,
        )
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=run,
            stage_code="running_normalized_match",
            progress_percent=55,
            items_processed=0,
            items_total=total_items,
        )
        if group_target_records:
            group_target_map = self._target_value_map(group_target_records)
            if include_fuzzy:
                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.REFERENCE_MATCHING,
                    job=run,
                    stage_code="running_fuzzy_match",
                    progress_percent=75,
                    items_processed=processed_items,
                    items_total=total_items,
                )
            group_matches = self._find_matches_for_target_map(
                catalog,
                target_values_by_key=group_target_map,
                allow_fuzzy=include_fuzzy,
                progress_label="lexicon_groups",
            )
            stored_group_matches = self._replace_stored_matches_batch(
                session,
                user_id=user_id,
                target_type=ReferenceMatchTargetType.LEXICON_GROUP,
                target_keys=[record.target_key for record in group_target_records],
                matches_by_target=group_matches,
            )
            group_stats = self._store_run_results_batch(
                session,
                run=run,
                user_id=user_id,
                target_records=group_target_records,
                matches_by_target=group_matches,
                stored_matches_by_target=stored_group_matches,
            )
            matched_items += group_stats.matched_items
            unmatched_items += group_stats.unmatched_items
            exact_match_count += group_stats.exact_match_count
            normalized_match_count += group_stats.normalized_match_count
            fuzzy_match_count += group_stats.fuzzy_match_count
            processed_items = len(group_target_records)
            self.job_progress_service.update_progress(
                session,
                job_kind=JobKind.REFERENCE_MATCHING,
                job=run,
                progress_percent=self.job_progress_service.ranged_progress(
                    processed_items,
                    total_items,
                    start_percent=60 if include_fuzzy else 55,
                    end_percent=80,
                ),
                items_processed=processed_items,
                items_total=total_items,
            )
            self._log_run_progress(
                session,
                run=run,
                processed_items=processed_items,
                total_items=total_items,
                matched_items=matched_items,
                started_monotonic=started_monotonic,
            )

        if lexeme_target_records:
            if include_fuzzy:
                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.REFERENCE_MATCHING,
                    job=run,
                    stage_code="running_fuzzy_match",
                    progress_percent=max(getattr(run, "progress_percent", 75), 75),
                    items_processed=processed_items,
                    items_total=total_items,
                )
            lexeme_target_map = self._target_value_map(lexeme_target_records)
            lexeme_matches = self._find_matches_for_target_map(
                catalog,
                target_values_by_key=lexeme_target_map,
                allow_fuzzy=include_fuzzy,
                progress_label="lexemes",
            )
            stored_lexeme_matches = self._replace_stored_matches_batch(
                session,
                user_id=user_id,
                target_type=ReferenceMatchTargetType.LEXEME,
                target_keys=list(lexeme_target_map),
                matches_by_target=lexeme_matches,
            )
            lexeme_stats = self._store_run_results_batch(
                session,
                run=run,
                user_id=user_id,
                target_records=lexeme_target_records,
                matches_by_target=lexeme_matches,
                stored_matches_by_target=stored_lexeme_matches,
            )
            matched_items += lexeme_stats.matched_items
            unmatched_items += lexeme_stats.unmatched_items
            exact_match_count += lexeme_stats.exact_match_count
            normalized_match_count += lexeme_stats.normalized_match_count
            fuzzy_match_count += lexeme_stats.fuzzy_match_count
            processed_items += len(lexeme_target_records)
            self.job_progress_service.update_progress(
                session,
                job_kind=JobKind.REFERENCE_MATCHING,
                job=run,
                progress_percent=self.job_progress_service.ranged_progress(
                    processed_items,
                    total_items,
                    start_percent=80 if include_fuzzy else 60,
                    end_percent=90,
                ),
                items_processed=processed_items,
                items_total=total_items,
            )
            self._log_run_progress(
                session,
                run=run,
                processed_items=processed_items,
                total_items=total_items,
                matched_items=matched_items,
                started_monotonic=started_monotonic,
            )

        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=run,
            stage_code="saving_matches",
            progress_percent=92,
            items_processed=processed_items,
            items_total=total_items,
        )
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=run,
            stage_code="finalizing",
            progress_percent=97,
            items_processed=processed_items,
            items_total=total_items,
        )
        run.status = ReferenceMatchRunStatus.COMPLETED
        run.total_items = total_items
        run.matched_items = matched_items
        run.unmatched_items = unmatched_items
        run.exact_match_count = exact_match_count
        run.normalized_match_count = normalized_match_count
        run.fuzzy_match_count = fuzzy_match_count
        run.error_message = None
        run.error_code = None
        run.error_message_user = None
        run.next_steps = None
        self.job_progress_service.complete(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=run,
        )
        session.flush()
        logger.info(
            "Reference matching run finished run_id=%s processed=%s matched=%s unmatched=%s total=%s exact=%s normalized=%s fuzzy=%s elapsed_seconds=%.2f",
            run.id,
            processed_items,
            matched_items,
            unmatched_items,
            total_items,
            exact_match_count,
            normalized_match_count,
            fuzzy_match_count,
            time.monotonic() - started_monotonic,
        )

    def _process_source_to_internal_run_in_session(
        self,
        session: Session,
        *,
        run: ReferenceMatchRun,
        include_fuzzy: bool,
    ) -> None:
        del include_fuzzy
        if run.source_id is None:
            raise ValueError("source_id is required for source_to_internal matching.")

        source = session.scalar(
            select(ReferenceSource).where(
                ReferenceSource.id == run.source_id,
                ReferenceSource.user_id == run.user_id,
            )
        )
        if source is None:
            raise ValueError("Reference source not found.")

        started_monotonic = time.monotonic()
        run.status = ReferenceMatchRunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc)
        run.finished_at = None
        run.error_message = None
        run.error_code = None
        run.error_message_user = None
        run.next_steps = None
        self._clear_run_results(session, run_id=run.id)
        session.flush()

        entry_rows = list(
            session.execute(
                select(ReferenceEntry)
                .where(ReferenceEntry.source_id == source.id)
                .order_by(ReferenceEntry.normalized_form.asc(), ReferenceEntry.surface_form.asc(), ReferenceEntry.id.asc())
            ).scalars()
        )
        normalized_forms = [entry.normalized_form for entry in entry_rows]
        total_items = len(entry_rows)
        run.total_items = total_items
        run.items_total = total_items
        run.items_processed = 0
        run.matched_items = 0
        run.unmatched_items = 0
        run.exact_match_count = 0
        run.normalized_match_count = 0
        run.fuzzy_match_count = 0
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=run,
            stage_code="loading_reference_entries",
            progress_percent=20,
            items_processed=0,
            items_total=total_items,
        )

        should_check_lexicon = run.target_scope in {
            ReferenceMatchTargetScope.LEXICON,
            ReferenceMatchTargetScope.ALL_INTERNAL,
            None,
        }
        should_check_books = run.target_scope in {
            ReferenceMatchTargetScope.IMPORTED_BOOKS,
            ReferenceMatchTargetScope.ALL_INTERNAL,
            None,
        }

        lexeme_map: dict[str, list[Lexeme]] = {}
        if should_check_lexicon and normalized_forms:
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.REFERENCE_MATCHING,
                job=run,
                stage_code="checking_lexicon",
                progress_percent=40,
                items_processed=0,
                items_total=total_items,
            )
            lexeme_map = self._matching_lexeme_map(session, user_id=UUID(run.user_id), normalized_forms=normalized_forms)

        document_evidence_map: dict[str, ReferenceEntryDocumentEvidence] = {}
        if should_check_books and normalized_forms:
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.REFERENCE_MATCHING,
                job=run,
                stage_code="checking_imported_books",
                progress_percent=65,
                items_processed=0,
                items_total=total_items,
            )
            document_evidence_map = self._document_evidence_map(
                session,
                user_id=UUID(run.user_id),
                normalized_forms=normalized_forms,
                sample_limit_per_form=1,
            )

        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=run,
            stage_code="saving_results",
            progress_percent=90,
            items_processed=0,
            items_total=total_items,
        )
        result_rows: list[ReferenceMatchRunResult] = []
        matched_items = 0
        unmatched_items = 0
        normalized_match_count = 0
        for index, entry in enumerate(entry_rows, start=1):
            matching_lexemes = lexeme_map.get(entry.normalized_form, []) if should_check_lexicon else []
            document_evidence = document_evidence_map.get(entry.normalized_form) if should_check_books else None
            best_lexeme = matching_lexemes[0] if matching_lexemes else None
            best_context = document_evidence.contexts[0] if document_evidence and document_evidence.contexts else None
            match_count = len(matching_lexemes) + (
                document_evidence.occurrence_count if document_evidence is not None else 0
            )
            match_status = ReferenceMatchStatus.MATCHED if match_count else ReferenceMatchStatus.UNMATCHED
            if match_status is ReferenceMatchStatus.MATCHED:
                matched_items += 1
                normalized_match_count += 1
            else:
                unmatched_items += 1

            result_rows.append(
                ReferenceMatchRunResult(
                    run_id=run.id,
                    user_id=run.user_id,
                    matching_direction=ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
                    source_id=source.id,
                    reference_entry_id=entry.id,
                    target_type=ReferenceMatchTargetType.REFERENCE_ENTRY,
                    target_key=str(entry.id),
                    target_label=entry.surface_form,
                    normalized_form=entry.normalized_form,
                    match_status=match_status,
                    match_count=match_count,
                    exists_in_lexicon=bool(matching_lexemes),
                    matching_lexeme_count=len(matching_lexemes),
                    best_lexeme_id=best_lexeme.id if best_lexeme is not None else None,
                    best_lexeme_canonical_form=best_lexeme.canonical_form if best_lexeme is not None else None,
                    found_in_books=bool(document_evidence and document_evidence.occurrence_count),
                    matching_book_occurrence_count=document_evidence.occurrence_count if document_evidence is not None else 0,
                    best_document_id=best_context.document_id if best_context is not None else None,
                    best_document_title=best_context.document_title if best_context is not None else None,
                    best_page_number=best_context.page_number if best_context is not None else None,
                    best_context_snippet=best_context.context_snippet if best_context is not None else None,
                    source_import_method=source.last_import_method,
                    source_warning=source.last_import_warning,
                )
            )
            if index == total_items or index % PROGRESS_LOG_EVERY == 0:
                self.job_progress_service.update_progress(
                    session,
                    job_kind=JobKind.REFERENCE_MATCHING,
                    job=run,
                    progress_percent=self.job_progress_service.ranged_progress(
                        index,
                        max(total_items, 1),
                        start_percent=90,
                        end_percent=96,
                    ),
                    items_processed=index,
                    items_total=total_items,
                )

        if result_rows:
            session.add_all(result_rows)
            session.flush()

        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_MATCHING,
            job=run,
            stage_code="finalizing",
            progress_percent=97,
            items_processed=total_items,
            items_total=total_items,
        )
        run.status = ReferenceMatchRunStatus.COMPLETED
        run.total_items = total_items
        run.matched_items = matched_items
        run.unmatched_items = unmatched_items
        run.exact_match_count = 0
        run.normalized_match_count = normalized_match_count
        run.fuzzy_match_count = 0
        run.items_processed = total_items
        run.items_total = total_items
        self.job_progress_service.complete(session, job_kind=JobKind.REFERENCE_MATCHING, job=run)
        session.flush()
        logger.info(
            "Source-first reference matching run finished run_id=%s source_id=%s matched=%s unmatched=%s total=%s elapsed_seconds=%.2f",
            run.id,
            source.id,
            matched_items,
            unmatched_items,
            total_items,
            time.monotonic() - started_monotonic,
        )

    def group_summary_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: Sequence[str],
    ) -> dict[str, StoredReferenceSummary]:
        if not self.reference_schema_available(session):
            return self._empty_summary_map(normalized_forms)
        return self.reference_status_service.group_summary_map(
            session,
            user_id=user_id,
            normalized_forms=normalized_forms,
        )

    def lexeme_summary_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        lexeme_ids: Sequence[UUID],
    ) -> dict[str, StoredReferenceSummary]:
        if not self.reference_schema_available(session):
            return self._empty_summary_map([str(lexeme_id) for lexeme_id in lexeme_ids])
        return self.reference_status_service.lexeme_summary_map(
            session,
            user_id=user_id,
            lexeme_ids=lexeme_ids,
        )

    def reference_entry_summary_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        reference_entry_ids: Sequence[UUID],
    ) -> dict[UUID, ReferenceEntryStatusSummary]:
        if not self.reference_schema_available(session):
            return {}
        return self.reference_status_service.reference_entry_summary_map(
            session,
            user_id=user_id,
            reference_entry_ids=reference_entry_ids,
        )

    def reference_status_filter(
        self,
        *,
        user_id: UUID,
        target_type: ReferenceMatchTargetType,
    ):
        return self._reference_status_filter_for_session(
            None,
            user_id=user_id,
            target_type=target_type,
        )

    def reference_status_filter_for_session(
        self,
        session: Session,
        *,
        user_id: UUID,
        target_type: ReferenceMatchTargetType,
    ):
        return self._reference_status_filter_for_session(
            session,
            user_id=user_id,
            target_type=target_type,
        )

    def _reference_status_filter_for_session(
        self,
        session: Session | None,
        *,
        user_id: UUID,
        target_type: ReferenceMatchTargetType,
    ):
        if session is not None and not self.reference_schema_available(session):
            return None
        return self.reference_status_service.matched_target_keys_subquery(
            user_id=user_id,
            target_type=target_type,
        )

    def reference_entry_status_filter_for_session(
        self,
        session: Session,
        *,
        user_id: UUID,
        source_id: UUID | None = None,
    ):
        if not self.reference_schema_available(session):
            return None
        return self.reference_status_service.matched_reference_entry_ids_subquery(
            user_id=user_id,
            source_id=source_id,
        )

    def _find_matches_for_values(
        self,
        session: Session,
        *,
        user_id: UUID,
        target_values: Sequence[str],
        allow_fuzzy: bool,
    ) -> list[LiveReferenceMatch]:
        unique_targets = [value for value in dict.fromkeys(value.strip() for value in target_values) if value]
        if not unique_targets:
            return []
        catalog = self._build_reference_catalog(session, user_id=user_id)
        return self._match_values_from_catalog(catalog, unique_targets, allow_fuzzy=allow_fuzzy)

    def _build_reference_catalog(self, session: Session, *, user_id: UUID) -> ReferenceCatalog:
        catalog = ReferenceCatalog()
        offset = 0
        while True:
            rows = session.execute(
                select(ReferenceEntry, ReferenceSource)
                .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
                .where(
                    ReferenceSource.user_id == str(user_id),
                    ReferenceSource.is_active.is_(True),
                )
                .order_by(ReferenceSource.display_name.asc(), ReferenceEntry.surface_form.asc(), ReferenceEntry.id.asc())
                .limit(MATCH_QUERY_CHUNK_SIZE)
                .offset(offset)
            ).all()
            if not rows:
                break

            for row in rows:
                entry = ReferenceCatalogEntry(
                    source_id=row.ReferenceSource.id,
                    source_display_name=row.ReferenceSource.display_name,
                    reference_entry_id=row.ReferenceEntry.id,
                    surface_form=row.ReferenceEntry.surface_form,
                    normalized_form=row.ReferenceEntry.normalized_form,
                    source_import_method=row.ReferenceSource.last_import_method,
                    source_warning=row.ReferenceSource.last_import_warning,
                    metadata_json=row.ReferenceEntry.metadata_json,
                )
                catalog.by_surface.setdefault(entry.surface_form, []).append(entry)
                catalog.by_normalized.setdefault(entry.normalized_form, []).append(entry)
                if entry.source_import_method is ReferenceImportMethod.PDF_OCR:
                    continue
                for prefix_length in (1, 2):
                    if len(entry.normalized_form) < prefix_length:
                        continue
                    catalog.fuzzy_buckets.setdefault(
                        (prefix_length, entry.normalized_form[:prefix_length]),
                        [],
                    ).append(entry)

            if len(rows) < MATCH_QUERY_CHUNK_SIZE:
                break
            offset += MATCH_QUERY_CHUNK_SIZE

        return catalog

    def _find_matches_for_target_map(
        self,
        catalog: ReferenceCatalog,
        *,
        target_values_by_key: dict[str, Sequence[str]],
        allow_fuzzy: bool,
        progress_label: str | None = None,
    ) -> dict[str, list[LiveReferenceMatch]]:
        normalized_target_map = {
            target_key: [value for value in dict.fromkeys(item.strip() for item in values) if value]
            for target_key, values in target_values_by_key.items()
        }
        match_maps: dict[str, dict[tuple[UUID, UUID, ReferenceMatchType], LiveReferenceMatch]] = {
            target_key: {}
            for target_key in normalized_target_map
        }
        values_to_targets: dict[str, list[str]] = {}
        for target_key, values in normalized_target_map.items():
            for value in values:
                values_to_targets.setdefault(value, []).append(target_key)

        for target_value, target_keys in values_to_targets.items():
            for entry in catalog.by_surface.get(target_value, []):
                self._record_match_for_targets(
                    match_maps,
                    target_keys=target_keys,
                    match=self._catalog_entry_to_match(entry, match_type=ReferenceMatchType.EXACT),
                )
            for entry in catalog.by_normalized.get(target_value, []):
                self._record_match_for_targets(
                    match_maps,
                    target_keys=target_keys,
                    match=self._catalog_entry_to_match(entry, match_type=ReferenceMatchType.NORMALIZED),
                )

        if allow_fuzzy:
            unmatched_target_keys = [target_key for target_key, matches in match_maps.items() if not matches]
            total_unmatched = len(unmatched_target_keys)
            for index, target_key in enumerate(unmatched_target_keys, start=1):
                for match in self._find_fuzzy_matches_in_catalog(catalog, normalized_target_map[target_key]):
                    match_maps[target_key][(match.source_id, match.reference_entry_id, match.match_type)] = match
                if progress_label and (index == total_unmatched or index % PROGRESS_LOG_EVERY == 0):
                    logger.info(
                        "Reference matching fuzzy progress label=%s processed=%s/%s",
                        progress_label,
                        index,
                        total_unmatched,
                    )

        return {
            target_key: sorted(matches.values(), key=self._match_sort_key)
            for target_key, matches in match_maps.items()
        }

    def _match_values_from_catalog(
        self,
        catalog: ReferenceCatalog,
        target_values: Sequence[str],
        *,
        allow_fuzzy: bool,
    ) -> list[LiveReferenceMatch]:
        matches_by_target = self._find_matches_for_target_map(
            catalog,
            target_values_by_key={"__single__": target_values},
            allow_fuzzy=allow_fuzzy,
        )
        return matches_by_target["__single__"]

    def _find_fuzzy_matches_in_catalog(
        self,
        catalog: ReferenceCatalog,
        target_values: Sequence[str],
    ) -> list[LiveReferenceMatch]:
        matches: dict[tuple[UUID, UUID, ReferenceMatchType], LiveReferenceMatch] = {}
        threshold = float(self.settings.reference_fuzzy_threshold_default)
        for target_value in target_values:
            prefix_length = 2 if len(target_value) >= 4 else 1
            candidates = catalog.fuzzy_buckets.get((prefix_length, target_value[:prefix_length]), [])
            for entry in candidates:
                if entry.normalized_form == target_value:
                    continue
                if not max(1, len(target_value) - 2) <= len(entry.normalized_form) <= len(target_value) + 2:
                    continue
                score = self._similarity_score(target_value, entry.normalized_form)
                if score < threshold:
                    continue
                match = self._catalog_entry_to_match(
                    entry,
                    match_type=ReferenceMatchType.FUZZY,
                    match_score=round(score, 2),
                )
                key = (match.source_id, match.reference_entry_id, match.match_type)
                existing = matches.get(key)
                if existing is None or (match.match_score or 0.0) > (existing.match_score or 0.0):
                    matches[key] = match
        return sorted(matches.values(), key=self._match_sort_key)

    def _replace_stored_matches_batch(
        self,
        session: Session,
        *,
        user_id: UUID,
        target_type: ReferenceMatchTargetType,
        target_keys: Sequence[str],
        matches_by_target: dict[str, Sequence[LiveReferenceMatch]],
    ) -> dict[str, list[ReferenceMatch]]:
        unique_target_keys = [target_key for target_key in dict.fromkeys(target_keys) if target_key]
        for target_key_chunk in self._chunked(unique_target_keys, MATCH_QUERY_CHUNK_SIZE):
            session.execute(
                delete(ReferenceMatch).where(
                    ReferenceMatch.user_id == str(user_id),
                    ReferenceMatch.target_type == target_type,
                    ReferenceMatch.target_key.in_(target_key_chunk),
                )
            )

        stored_matches_by_target: dict[str, list[ReferenceMatch]] = {target_key: [] for target_key in unique_target_keys}
        pending_rows: list[ReferenceMatch] = []
        for target_key in unique_target_keys:
            for match in matches_by_target.get(target_key, []):
                stored_match = ReferenceMatch(
                    user_id=str(user_id),
                    target_type=target_type,
                    target_key=target_key,
                    source_id=match.source_id,
                    reference_entry_id=match.reference_entry_id,
                    match_type=match.match_type,
                    match_score=match.match_score,
                    matched_form=match.surface_form,
                )
                pending_rows.append(stored_match)
                stored_matches_by_target.setdefault(target_key, []).append(stored_match)
                if len(pending_rows) >= MATCH_INSERT_CHUNK_SIZE:
                    session.add_all(pending_rows)
                    session.flush()
                    pending_rows.clear()

        if pending_rows:
            session.add_all(pending_rows)
            session.flush()
        return stored_matches_by_target

    def _clear_run_results(self, session: Session, *, run_id: UUID) -> None:
        session.execute(delete(ReferenceMatchRunResultMatch).where(ReferenceMatchRunResultMatch.run_id == run_id))
        session.execute(delete(ReferenceMatchRunResult).where(ReferenceMatchRunResult.run_id == run_id))

    def _store_run_results_batch(
        self,
        session: Session,
        *,
        run: ReferenceMatchRun,
        user_id: UUID,
        target_records: Sequence[RunTargetRecord],
        matches_by_target: dict[str, Sequence[LiveReferenceMatch]],
        stored_matches_by_target: dict[str, Sequence[ReferenceMatch]],
    ) -> RunResultStats:
        stats = RunResultStats()
        result_rows: list[ReferenceMatchRunResult] = []
        detail_rows: list[ReferenceMatchRunResultMatch] = []

        for record in target_records:
            target_matches = list(matches_by_target.get(record.target_key, []))
            stored_matches = list(stored_matches_by_target.get(record.target_key, []))
            best_match = target_matches[0] if target_matches else None
            match_status = ReferenceMatchStatus.MATCHED if target_matches else ReferenceMatchStatus.UNMATCHED

            if match_status is ReferenceMatchStatus.MATCHED:
                stats.matched_items += 1
                if best_match is not None:
                    if best_match.match_type is ReferenceMatchType.EXACT:
                        stats.exact_match_count += 1
                    elif best_match.match_type is ReferenceMatchType.NORMALIZED:
                        stats.normalized_match_count += 1
                    elif best_match.match_type is ReferenceMatchType.FUZZY:
                        stats.fuzzy_match_count += 1
            else:
                stats.unmatched_items += 1

            result_rows.append(
                ReferenceMatchRunResult(
                    run_id=run.id,
                    user_id=str(user_id),
                    matching_direction=run.matching_direction,
                    source_id=None,
                    reference_entry_id=None,
                    target_type=record.target_type,
                    target_key=record.target_key,
                    target_label=record.target_label,
                    normalized_form=record.target_values[0] if record.target_values else record.target_label,
                    match_status=match_status,
                    best_match_id=stored_matches[0].id if stored_matches else None,
                    best_match_type=best_match.match_type if best_match is not None else None,
                    best_match_score=best_match.match_score if best_match is not None else None,
                    best_source_id=best_match.source_id if best_match is not None else None,
                    best_source_display_name=best_match.source_display_name if best_match is not None else None,
                    best_matched_form=best_match.surface_form if best_match is not None else None,
                    match_count=len(target_matches),
                    exists_in_lexicon=False,
                    matching_lexeme_count=0,
                    best_lexeme_id=None,
                    best_lexeme_canonical_form=None,
                    found_in_books=False,
                    matching_book_occurrence_count=0,
                    best_document_id=None,
                    best_document_title=None,
                    best_page_number=None,
                    best_context_snippet=None,
                    source_import_method=None,
                    source_warning=None,
                    related_resource_type=record.related_resource_type,
                    related_resource_id=record.related_resource_id,
                )
            )

        if not result_rows:
            return stats

        session.add_all(result_rows)
        session.flush()

        result_map = {row.target_key: row for row in result_rows}
        for record in target_records:
            result_row = result_map[record.target_key]
            for match in matches_by_target.get(record.target_key, []):
                detail_rows.append(
                    ReferenceMatchRunResultMatch(
                        result_id=result_row.id,
                        run_id=run.id,
                        user_id=str(user_id),
                        source_id=match.source_id,
                        source_display_name=match.source_display_name,
                        reference_entry_id=match.reference_entry_id,
                        surface_form=match.surface_form,
                        normalized_form=match.normalized_form,
                        match_type=match.match_type,
                        match_score=match.match_score,
                        source_import_method=match.source_import_method,
                        source_warning=match.source_warning,
                    )
                )

        if detail_rows:
            session.add_all(detail_rows)
            session.flush()

        return stats

    @staticmethod
    def _lexicon_normalized_form_subquery(*, user_id: UUID):
        user_key = str(user_id)
        form_subquery = select(LexemeForm.normalized_form).where(LexemeForm.user_id == user_key)
        canonical_subquery = select(Lexeme.canonical_normalized_form).where(Lexeme.user_id == user_key)
        return canonical_subquery.union(form_subquery)

    @staticmethod
    def _document_normalized_form_subquery(*, user_id: UUID):
        return (
            select(Occurrence.normalized_token)
            .join(Document, Occurrence.document_id == Document.id)
            .where(Document.user_id == user_id)
            .distinct()
        )

    @staticmethod
    def _matching_lexeme_map(
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: Sequence[str],
    ) -> dict[str, list[Lexeme]]:
        forms = [form for form in dict.fromkeys(normalized_forms) if form]
        if not forms:
            return {}

        user_key = str(user_id)
        grouped: dict[str, list[Lexeme]] = {form: [] for form in forms}
        direct_matches = list(
            session.scalars(
                select(Lexeme)
                .where(
                    Lexeme.user_id == user_key,
                    Lexeme.canonical_normalized_form.in_(forms),
                )
                .order_by(Lexeme.created_at.asc(), Lexeme.id.asc())
            )
        )
        for lexeme in direct_matches:
            grouped.setdefault(lexeme.canonical_normalized_form, []).append(lexeme)

        form_rows = session.execute(
            select(LexemeForm.normalized_form, Lexeme)
            .join(Lexeme, Lexeme.id == LexemeForm.lexeme_id)
            .where(
                LexemeForm.user_id == user_key,
                Lexeme.user_id == user_key,
                LexemeForm.normalized_form.in_(forms),
            )
            .order_by(Lexeme.created_at.asc(), Lexeme.id.asc(), LexemeForm.normalized_form.asc())
        ).all()
        for normalized_form, lexeme in form_rows:
            existing = grouped.setdefault(normalized_form, [])
            if all(item.id != lexeme.id for item in existing):
                existing.append(lexeme)
        return grouped

    @staticmethod
    def _document_evidence_map(
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: Sequence[str],
        sample_limit_per_form: int | None,
    ) -> dict[str, ReferenceEntryDocumentEvidence]:
        forms = [form for form in dict.fromkeys(normalized_forms) if form]
        if not forms:
            return {}

        rows = session.execute(
            select(
                Occurrence.normalized_token,
                Document.id,
                Document.title,
                Occurrence.page_number,
                Occurrence.context_snippet,
                Occurrence.id,
                Occurrence.char_start,
            )
            .join(Document, Occurrence.document_id == Document.id)
            .where(
                Document.user_id == user_id,
                Occurrence.normalized_token.in_(forms),
            )
            .order_by(
                Occurrence.normalized_token.asc(),
                Document.title.asc(),
                Occurrence.page_number.asc(),
                Occurrence.char_start.asc().nullsfirst(),
                Occurrence.id.asc(),
            )
        ).all()

        evidence_map: dict[str, ReferenceEntryDocumentEvidence] = {
            form: ReferenceEntryDocumentEvidence()
            for form in forms
        }
        for (
            normalized_form,
            document_id,
            document_title,
            page_number,
            context_snippet,
            occurrence_id,
            _char_start,
        ) in rows:
            evidence = evidence_map.setdefault(normalized_form, ReferenceEntryDocumentEvidence())
            evidence.occurrence_count += 1
            if sample_limit_per_form is not None and len(evidence.contexts) >= sample_limit_per_form:
                continue
            evidence.contexts.append(
                ReferenceMatchingBookContext(
                    document_id=document_id,
                    document_title=document_title,
                    page_number=page_number,
                    context_snippet=context_snippet,
                    occurrence_id=occurrence_id,
                    reference_link=f"/documents/{document_id}" + (f"?page={page_number}" if page_number else ""),
                )
            )
        return evidence_map

    @staticmethod
    def _to_reference_entry_run_result_summary(result: ReferenceMatchRunResult) -> ReferenceMatchRunEntryResultSummary:
        assert result.reference_entry_id is not None
        assert result.source_id is not None
        return ReferenceMatchRunEntryResultSummary(
            id=result.id,
            run_id=result.run_id,
            reference_entry_id=result.reference_entry_id,
            source_id=result.source_id,
            target_label=result.target_label,
            normalized_form=result.normalized_form,
            match_status=result.match_status,
            match_count=result.match_count,
            source_import_method=result.source_import_method,
            source_warning=result.source_warning,
            exists_in_lexicon=result.exists_in_lexicon,
            best_lexeme_id=result.best_lexeme_id,
            best_lexeme_canonical_form=result.best_lexeme_canonical_form,
            matching_lexeme_count=result.matching_lexeme_count,
            found_in_books=result.found_in_books,
            matching_book_occurrence_count=result.matching_book_occurrence_count,
            best_document_id=result.best_document_id,
            best_document_title=result.best_document_title,
            best_page_number=result.best_page_number,
            best_context_snippet=result.best_context_snippet,
            created_at=result.created_at,
            updated_at=result.updated_at,
        )

    def _summary_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        target_type: ReferenceMatchTargetType,
        target_keys: Sequence[str],
    ) -> dict[str, StoredReferenceSummary]:
        keys = [key for key in dict.fromkeys(target_keys) if key]
        summaries = {
            key: StoredReferenceSummary(
                has_reference_match=False,
                reference_match_count=0,
                best_reference_match=None,
            )
            for key in keys
        }
        if not keys:
            return summaries

        rows = session.execute(
            select(ReferenceMatch, ReferenceSource.display_name)
            .join(ReferenceSource, ReferenceMatch.source_id == ReferenceSource.id)
            .where(
                ReferenceMatch.user_id == str(user_id),
                ReferenceMatch.target_type == target_type,
                ReferenceMatch.target_key.in_(keys),
            )
        ).all()
        grouped: dict[str, list[tuple[ReferenceMatch, str]]] = {key: [] for key in keys}
        for row in rows:
            grouped.setdefault(row.ReferenceMatch.target_key, []).append((row.ReferenceMatch, row.display_name))

        for key, values in grouped.items():
            if not values:
                continue
            best_match_row, best_display_name = min(
                values,
                key=lambda value: self._stored_match_sort_key(value[0], value[1]),
            )
            summaries[key] = StoredReferenceSummary(
                has_reference_match=True,
                reference_match_count=len(values),
                best_reference_match=ReferenceMatchBest(
                    source_display_name=best_display_name,
                    matched_form=best_match_row.matched_form,
                    match_type=best_match_row.match_type,
                    match_score=self._float_or_none(best_match_row.match_score),
                ),
            )
        return summaries

    @staticmethod
    def _empty_summary_map(target_keys: Sequence[str]) -> dict[str, StoredReferenceSummary]:
        return {
            key: StoredReferenceSummary(
                has_reference_match=False,
                reference_match_count=0,
                best_reference_match=None,
            )
            for key in dict.fromkeys(target_keys)
            if key
        }

    @staticmethod
    def _target_value_map(target_records: Sequence[RunTargetRecord]) -> dict[str, list[str]]:
        return {
            record.target_key: record.target_values
            for record in target_records
        }

    def _collect_group_target_records(
        self,
        session: Session,
        *,
        user_id: UUID,
        view: LexiconGroupView,
    ) -> list[RunTargetRecord]:
        group_subquery = self.lexicon_service._build_group_subquery(  # noqa: SLF001 - shared internal grouping logic
            user_id=user_id,
            search=None,
            document_id=None,
        )
        filters = self.lexicon_service._view_filters(group_subquery, view)  # noqa: SLF001
        values = list(
            session.scalars(
                select(group_subquery.c.normalized_form)
                .where(*filters)
                .order_by(group_subquery.c.occurrence_count.desc(), group_subquery.c.normalized_form.asc())
            )
        )
        return [
            RunTargetRecord(
                target_type=ReferenceMatchTargetType.LEXICON_GROUP,
                target_key=normalized_form,
                target_label=normalized_form,
                target_values=[normalized_form],
                related_resource_type=ReferenceMatchTargetType.LEXICON_GROUP,
                related_resource_id=normalized_form,
            )
            for normalized_form in values
        ]

    @staticmethod
    def _collect_lexeme_targets(session: Session, *, user_id: UUID) -> list[Lexeme]:
        return list(
            session.scalars(
                select(Lexeme)
                .where(Lexeme.user_id == str(user_id))
                .order_by(Lexeme.created_at.asc(), Lexeme.id.asc())
            )
        )

    def _collect_lexeme_target_records(self, session: Session, *, user_id: UUID) -> list[RunTargetRecord]:
        lexemes = self._collect_lexeme_targets(session, user_id=user_id)
        if not lexemes:
            return []

        lexeme_ids = [lexeme.id for lexeme in lexemes]
        form_rows = session.execute(
            select(LexemeForm.lexeme_id, LexemeForm.normalized_form)
            .where(
                LexemeForm.user_id == str(user_id),
                LexemeForm.lexeme_id.in_(lexeme_ids),
            )
            .order_by(LexemeForm.lexeme_id.asc(), LexemeForm.normalized_form.asc())
        ).all()
        form_map: dict[UUID, list[str]] = {}
        for lexeme_id, normalized_form in form_rows:
            form_map.setdefault(lexeme_id, []).append(normalized_form)

        target_records: list[RunTargetRecord] = []
        for lexeme in lexemes:
            values = [lexeme.canonical_normalized_form]
            values.extend(form_map.get(lexeme.id, []))
            target_records.append(
                RunTargetRecord(
                    target_type=ReferenceMatchTargetType.LEXEME,
                    target_key=str(lexeme.id),
                    target_label=lexeme.canonical_form,
                    target_values=[value for value in dict.fromkeys(values) if value],
                    related_resource_type=ReferenceMatchTargetType.LEXEME,
                    related_resource_id=str(lexeme.id),
                )
            )
        return target_records

    @staticmethod
    def _lexeme_target_values(session: Session, *, user_id: UUID, lexeme: Lexeme) -> list[str]:
        values = [lexeme.canonical_normalized_form]
        values.extend(
            session.scalars(
                select(LexemeForm.normalized_form)
                .where(
                    LexemeForm.lexeme_id == lexeme.id,
                    LexemeForm.user_id == str(user_id),
                )
                .order_by(LexemeForm.normalized_form.asc())
            )
        )
        return [value for value in dict.fromkeys(values) if value]

    def _parse_group_view(self, value: str) -> LexiconGroupView:
        try:
            return LexiconGroupView(value)
        except ValueError as exc:
            raise ValueError("Invalid lexicon group view for reference matching.") from exc

    @staticmethod
    def _to_match_detail(match: LiveReferenceMatch) -> ReferenceMatchDetail:
        return ReferenceMatchDetail(
            source_id=match.source_id,
            source_display_name=match.source_display_name,
            reference_entry_id=match.reference_entry_id,
            surface_form=match.surface_form,
            normalized_form=match.normalized_form,
            match_type=match.match_type,
            match_score=match.match_score,
            source_import_method=match.source_import_method,
            source_warning=match.source_warning,
            metadata_json=match.metadata_json,
        )

    @staticmethod
    def _to_run_result_summary(result: ReferenceMatchRunResult) -> ReferenceMatchRunResultSummary:
        target_lexeme_id = UUID(result.target_key) if result.target_type is ReferenceMatchTargetType.LEXEME else None
        return ReferenceMatchRunResultSummary(
            id=result.id,
            run_id=result.run_id,
            matching_direction=result.matching_direction,
            source_id=result.source_id,
            reference_entry_id=result.reference_entry_id,
            target_type=result.target_type,
            target_key=result.target_key,
            target_label=result.target_label,
            normalized_form=result.normalized_form,
            target_lexeme_id=target_lexeme_id,
            match_status=result.match_status,
            match_count=result.match_count,
            best_match_type=result.best_match_type,
            best_match_score=ReferenceMatchingService._float_or_none(result.best_match_score),
            best_source_id=result.best_source_id,
            best_source_display_name=result.best_source_display_name,
            best_matched_form=result.best_matched_form,
            related_resource_type=result.related_resource_type,
            related_resource_id=result.related_resource_id,
            created_at=result.created_at,
            updated_at=result.updated_at,
        )

    @staticmethod
    def _to_stored_run_match_detail(match: ReferenceMatchRunResultMatch) -> ReferenceMatchDetail:
        return ReferenceMatchDetail(
            source_id=match.source_id,
            source_display_name=match.source_display_name,
            reference_entry_id=match.reference_entry_id,
            surface_form=match.surface_form,
            normalized_form=match.normalized_form,
            match_type=match.match_type,
            match_score=ReferenceMatchingService._float_or_none(match.match_score),
            source_import_method=match.source_import_method,
            source_warning=match.source_warning,
            metadata_json=None,
        )

    @staticmethod
    def _match_sort_key(match: LiveReferenceMatch) -> tuple[int, float, str, str]:
        return (
            MATCH_TYPE_PRIORITY[match.match_type],
            -(match.match_score or 0.0),
            match.source_display_name,
            match.surface_form,
        )

    @staticmethod
    def _catalog_entry_to_match(
        entry: ReferenceCatalogEntry,
        *,
        match_type: ReferenceMatchType,
        match_score: float | None = None,
    ) -> LiveReferenceMatch:
        return LiveReferenceMatch(
            source_id=entry.source_id,
            source_display_name=entry.source_display_name,
            reference_entry_id=entry.reference_entry_id,
            surface_form=entry.surface_form,
            normalized_form=entry.normalized_form,
            match_type=match_type,
            match_score=match_score,
            source_import_method=entry.source_import_method,
            source_warning=entry.source_warning,
            metadata_json=entry.metadata_json,
        )

    @staticmethod
    def _record_match_for_targets(
        match_maps: dict[str, dict[tuple[UUID, UUID, ReferenceMatchType], LiveReferenceMatch]],
        *,
        target_keys: Sequence[str],
        match: LiveReferenceMatch,
    ) -> None:
        match_key = (match.source_id, match.reference_entry_id, match.match_type)
        for target_key in target_keys:
            target_matches = match_maps[target_key]
            existing = target_matches.get(match_key)
            if existing is None or (match.match_score or 0.0) > (existing.match_score or 0.0):
                target_matches[match_key] = match

    @staticmethod
    def _stored_match_sort_key(match: ReferenceMatch, source_display_name: str) -> tuple[int, float, str, str]:
        return (
            MATCH_TYPE_PRIORITY[match.match_type],
            -(ReferenceMatchingService._float_or_none(match.match_score) or 0.0),
            source_display_name,
            match.matched_form,
        )

    @staticmethod
    def _run_result_match_sort_key(match: ReferenceMatchRunResultMatch) -> tuple[int, float, str, str]:
        return (
            MATCH_TYPE_PRIORITY[match.match_type],
            -(ReferenceMatchingService._float_or_none(match.match_score) or 0.0),
            match.source_display_name,
            match.surface_form,
        )

    @staticmethod
    def _similarity_score(left: str, right: str) -> float:
        if fuzz is not None:
            return float(fuzz.ratio(left, right))
        return float(SequenceMatcher(a=left, b=right).ratio() * 100)

    @staticmethod
    def _float_or_none(value: Decimal | float | None) -> float | None:
        if value is None:
            return None
        return float(value)

    @staticmethod
    def _chunked(values: Sequence[str], chunk_size: int) -> Sequence[Sequence[str]]:
        return [values[index:index + chunk_size] for index in range(0, len(values), chunk_size)]

    def _log_run_progress(
        self,
        session: Session,
        *,
        run: ReferenceMatchRun,
        processed_items: int,
        total_items: int,
        matched_items: int,
        started_monotonic: float,
    ) -> None:
        if processed_items == 0:
            return
        if processed_items != total_items and processed_items % PROGRESS_LOG_EVERY != 0:
            return

        run.matched_items = matched_items
        run.total_items = total_items
        run.items_processed = processed_items
        run.items_total = total_items
        session.flush()
        logger.info(
            "Reference matching run progress run_id=%s processed=%s/%s matched=%s elapsed_seconds=%.2f",
            run.id,
            processed_items,
            total_items,
            matched_items,
            time.monotonic() - started_monotonic,
        )

    def ensure_reference_schema(self, session: Session) -> None:
        if not self.reference_schema_available(session):
            raise ReferenceSchemaNotReadyError(
                "Reference matching is temporarily unavailable."
            )

    def reference_schema_available(self, session: Session) -> bool:
        try:
            inspector = inspect(session.connection())
            missing_tables = [
                table_name for table_name in REFERENCE_TABLES if not inspector.has_table(table_name)
            ]
            missing_columns = {
                table_name: sorted(
                    REFERENCE_REQUIRED_COLUMNS[table_name]
                    - {column["name"] for column in inspector.get_columns(table_name)}
                )
                for table_name in REFERENCE_TABLES
                if table_name not in missing_tables
            }
            missing_columns = {
                table_name: columns
                for table_name, columns in missing_columns.items()
                if columns
            }
            if missing_tables or missing_columns:
                logger.error(
                    "Reference matching schema not ready: missing_tables=%s missing_columns=%s",
                    missing_tables,
                    missing_columns,
                )
                return False
            return True
        except ProgrammingError as exc:  # pragma: no cover - depends on live database state
            if self._is_missing_reference_table_error(exc):
                logger.error("Reference matching schema inspection failed due to database schema error: %s", exc)
                return False
            raise

    @staticmethod
    def _is_missing_reference_table_error(exc: ProgrammingError) -> bool:
        message = str(exc).lower()
        return "undefinedtable" in message or "does not exist" in message or "no such table" in message


def get_reference_matching_service() -> ReferenceMatchingService:
    return ReferenceMatchingService()
