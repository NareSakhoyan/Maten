from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Document, Lexeme, LexemeForm, Occurrence, OccurrenceScriptType, ReferenceEntry, ReferenceSource
from app.schemas.lexicon import LexiconGroupState, LexiconGroupView
from app.schemas.reference import ReferenceStatusFilter
from app.db.models import ReferenceMatchTargetType
from app.schemas.word import (
    DocumentTrustedExternalStatus,
    DocumentWordCandidateSummary,
    ReferenceSourceWordCandidateSummary,
    SourceWordStatusView,
)
from app.services.document_trusted_external_service import (
    DocumentTrustedExternalService,
    NayiriLookupSnapshot,
    get_document_trusted_external_service,
)
from app.services.lexicon_service import LexiconService
from app.services.reference_matching_service import ReferenceMatchingService, get_reference_matching_service
from app.services.word_evidence_service import WordEvidenceService, get_word_evidence_service
from app.utils.text_normalization import normalize_token
from app.utils.token_classification import is_suspicious_script_type, suspicion_reasons_for_script_type


class SourceWordReviewService:
    def __init__(
        self,
        *,
        lexicon_service: LexiconService | None = None,
        reference_matching_service: ReferenceMatchingService | None = None,
        word_evidence_service: WordEvidenceService | None = None,
        document_trusted_external_service: DocumentTrustedExternalService | None = None,
    ) -> None:
        self.reference_matching_service = reference_matching_service or get_reference_matching_service()
        self.lexicon_service = lexicon_service or LexiconService(reference_matching_service=self.reference_matching_service)
        self.word_evidence_service = word_evidence_service or get_word_evidence_service()
        self.document_trusted_external_service = (
            document_trusted_external_service or get_document_trusted_external_service()
        )

    def list_document_word_candidates(
        self,
        session: Session,
        *,
        user_id: UUID,
        document: Document,
        search: str | None,
        status_view: SourceWordStatusView,
        reference_status: ReferenceStatusFilter,
        limit: int,
        offset: int,
    ) -> tuple[list[DocumentWordCandidateSummary], int]:
        group_subquery = self.lexicon_service._build_group_subquery(  # noqa: SLF001
            user_id=user_id,
            search=search,
            document_id=document.id,
        )
        filters = self._document_status_filters(group_subquery, status_view=status_view)
        filters.extend(
            self._document_reference_status_filters(
                session,
                group_subquery,
                user_id=user_id,
                document_id=document.id,
                reference_status=reference_status,
            )
        )

        rows = session.execute(
            select(group_subquery, func.count().over().label("total_count"))
            .where(*filters)
            .order_by(group_subquery.c.occurrence_count.desc(), group_subquery.c.normalized_form.asc())
            .limit(limit)
            .offset(offset)
        ).all()
        if rows:
            total = rows[0].total_count
        else:
            total = session.scalar(select(func.count()).select_from(group_subquery).where(*filters)) or 0
        normalized_forms = [row.normalized_form for row in rows]
        if not normalized_forms:
            return [], total

        reference_summary_map = self.reference_matching_service.group_summary_map(
            session,
            user_id=user_id,
            normalized_forms=normalized_forms,
        )
        nayiri_status_map = self.document_trusted_external_service.nayiri_status_map(
            session,
            normalized_forms=normalized_forms,
        )
        sample_map = self._sample_data_for_document_groups(
            session,
            user_id=user_id,
            document_id=document.id,
            normalized_forms=normalized_forms,
        )
        sample_pages_map = self._sample_pages_for_document_groups(
            session,
            user_id=user_id,
            document_id=document.id,
            normalized_forms=normalized_forms,
        )

        items: list[DocumentWordCandidateSummary] = []
        for row in rows:
            sample_tokens, sample_contexts = sample_map.get(row.normalized_form, ([], []))
            sample_pages = sample_pages_map.get(row.normalized_form, [])
            dominant_script_type = OccurrenceScriptType(row.dominant_script_type)
            reference_summary = reference_summary_map[row.normalized_form]
            nayiri_snapshot = nayiri_status_map.get(
                row.normalized_form,
                NayiriLookupSnapshot(status=DocumentTrustedExternalStatus.UNCHECKED),
            )
            has_reference_match = self.document_trusted_external_service.combined_has_reference_match(
                imported_has_match=reference_summary.has_reference_match,
                nayiri_snapshot=nayiri_snapshot,
            )
            items.append(
                DocumentWordCandidateSummary(
                    source_id=str(document.id),
                    source_title=document.title,
                    source_subtitle=document.original_filename,
                    reference_link=f"/documents/{document.id}" + (
                        f"?page={sample_pages[0]}" if sample_pages else ""
                    ),
                    normalized_form=row.normalized_form,
                    occurrence_count=row.occurrence_count,
                    page_count=row.page_count,
                    sample_tokens=sample_tokens,
                    sample_contexts=sample_contexts,
                    sample_pages=sample_pages,
                    linked_lexeme_id=row.linked_lexeme_id,
                    linked_lexeme_canonical_form=row.linked_lexeme_canonical_form,
                    group_state=LexiconGroupState(row.group_state),
                    dominant_script_type=dominant_script_type,
                    is_suspicious=is_suspicious_script_type(dominant_script_type),
                    suspicion_reasons=suspicion_reasons_for_script_type(dominant_script_type),
                    has_reference_match=has_reference_match,
                    reference_match_count=reference_summary.reference_match_count,
                    best_reference_match=reference_summary.best_reference_match,
                    trusted_external_status=nayiri_snapshot.status,
                    trusted_external_provider_display_name=nayiri_snapshot.provider_display_name,
                    trusted_external_match_count=nayiri_snapshot.match_count,
                    trusted_external_matched_form=nayiri_snapshot.matched_form,
                    trusted_external_source_title=nayiri_snapshot.source_title,
                    trusted_external_reference_link=nayiri_snapshot.reference_link,
                    trusted_external_snippet=nayiri_snapshot.snippet,
                    trusted_external_canonicalization_status=nayiri_snapshot.canonicalization_status,
                )
            )
        return items, total

    def _document_reference_status_filters(
        self,
        session: Session,
        group_subquery,
        *,
        user_id: UUID,
        document_id: UUID,
        reference_status: ReferenceStatusFilter,
    ) -> list[object]:
        if reference_status is ReferenceStatusFilter.ALL:
            return []

        imported_match_subquery = self.reference_matching_service.reference_status_filter_for_session(
            session,
            user_id=user_id,
            target_type=ReferenceMatchTargetType.LEXICON_GROUP,
        )
        forms = self.document_trusted_external_service.list_document_normalized_forms(
            session,
            user_id=user_id,
            document_id=document_id,
        )
        nayiri_found_forms = self.document_trusted_external_service.nayiri_found_normalized_forms(
            session,
            normalized_forms=forms,
        )
        nayiri_unmatched_forms = self.document_trusted_external_service.nayiri_not_found_normalized_forms(
            session,
            normalized_forms=forms,
        )

        if reference_status is ReferenceStatusFilter.MATCHED:
            matched_clauses = []
            if imported_match_subquery is not None:
                matched_clauses.append(group_subquery.c.normalized_form.in_(imported_match_subquery))
            if nayiri_found_forms:
                matched_clauses.append(group_subquery.c.normalized_form.in_(nayiri_found_forms))
            if not matched_clauses:
                return [group_subquery.c.normalized_form.in_(())]
            return [or_(*matched_clauses)]

        unmatched_clauses = [group_subquery.c.normalized_form.in_(nayiri_unmatched_forms)]
        excluded_clauses = []
        if imported_match_subquery is not None:
            excluded_clauses.append(group_subquery.c.normalized_form.in_(imported_match_subquery))
        if nayiri_found_forms:
            excluded_clauses.append(group_subquery.c.normalized_form.in_(nayiri_found_forms))
        if excluded_clauses:
            unmatched_clauses.append(~or_(*excluded_clauses))
        if not nayiri_unmatched_forms:
            return [group_subquery.c.normalized_form.in_(())]
        return unmatched_clauses

    def list_reference_source_word_candidates(
        self,
        session: Session,
        *,
        user_id: UUID,
        source: ReferenceSource,
        search: str | None,
        reference_status: ReferenceStatusFilter,
        limit: int,
        offset: int,
    ) -> tuple[list[ReferenceSourceWordCandidateSummary], int]:
        filters = [
            ReferenceEntry.source_id == source.id,
            ReferenceSource.user_id == str(user_id),
        ]
        normalized_search = normalize_token(search) if search else None
        if normalized_search:
            filters.append(
                (ReferenceEntry.normalized_form.ilike(f"%{normalized_search}%"))
                | (ReferenceEntry.surface_form.ilike(f"%{search.strip()}%"))
            )

        matched_entry_ids = self.reference_matching_service.reference_entry_status_filter_for_session(
            session,
            user_id=user_id,
            source_id=source.id,
        )
        if matched_entry_ids is not None:
            if reference_status is ReferenceStatusFilter.MATCHED:
                filters.append(ReferenceEntry.id.in_(matched_entry_ids))
            elif reference_status is ReferenceStatusFilter.UNMATCHED:
                filters.append(~ReferenceEntry.id.in_(matched_entry_ids))

        total = (
            session.scalar(
                select(func.count(ReferenceEntry.id))
                .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
                .where(*filters)
            )
            or 0
        )
        rows = session.execute(
            select(ReferenceEntry)
            .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
            .where(*filters)
            .order_by(ReferenceEntry.normalized_form.asc(), ReferenceEntry.surface_form.asc(), ReferenceEntry.id.asc())
            .limit(limit)
            .offset(offset)
        ).all()

        lexeme_map = self.word_evidence_service.matching_lexeme_map(
            session,
            user_id=user_id,
            normalized_forms=[row.ReferenceEntry.normalized_form for row in rows],
        )
        items = [
            self._to_reference_source_candidate(
                source=source,
                entry=row.ReferenceEntry,
                matching_lexemes=lexeme_map.get(row.ReferenceEntry.normalized_form, []),
            )
            for row in rows
        ]
        return items, total

    def count_document_workspace_summary(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
    ) -> dict[str, int]:
        group_subquery = self.lexicon_service._build_group_subquery(  # noqa: SLF001
            user_id=user_id,
            search=None,
            document_id=document_id,
        )
        total = session.scalar(select(func.count()).select_from(group_subquery)) or 0
        linked = session.scalar(
            select(func.count()).select_from(group_subquery).where(
                group_subquery.c.group_state == LexiconGroupState.LINKED.value
            )
        ) or 0
        suspicious = session.scalar(
            select(func.count()).select_from(group_subquery).where(
                *self.lexicon_service._view_filters(group_subquery, LexiconGroupView.SUSPICIOUS)  # noqa: SLF001
            )
        ) or 0
        unmatched = session.scalar(
            select(func.count()).select_from(group_subquery).where(
                *self.lexicon_service._reference_status_filters(  # noqa: SLF001
                    session,
                    group_subquery,
                    user_id=user_id,
                    reference_status=ReferenceStatusFilter.UNMATCHED,
                )
            )
        ) or 0
        return {
            "word_candidate_count": total,
            "linked_candidate_count": linked,
            "suspicious_candidate_count": suspicious,
            "unmatched_candidate_count": unmatched,
        }

    def count_reference_source_workspace_summary(
        self,
        session: Session,
        *,
        user_id: UUID,
        source: ReferenceSource,
    ) -> dict[str, int]:
        total = source.entry_count
        matched_entry_ids = self.reference_matching_service.reference_entry_status_filter_for_session(
            session,
            user_id=user_id,
            source_id=source.id,
        )
        matched = (
            session.scalar(
                select(func.count(ReferenceEntry.id)).where(
                    ReferenceEntry.source_id == source.id,
                    ReferenceEntry.id.in_(matched_entry_ids),
                )
            )
            or 0
        ) if matched_entry_ids is not None else 0
        return {
            "imported_entry_count": total,
            "matched_entry_count": matched,
            "unmatched_entry_count": max(total - matched, 0),
        }

    @staticmethod
    def _document_status_filters(group_subquery, *, status_view: SourceWordStatusView) -> list[object]:
        non_digit_mixed_filter = (
            group_subquery.c.dominant_script_type != OccurrenceScriptType.DIGIT_MIXED.value
        )
        if status_view is SourceWordStatusView.ALL:
            return [non_digit_mixed_filter]
        if status_view is SourceWordStatusView.LINKED:
            return [
                group_subquery.c.group_state == LexiconGroupState.LINKED.value,
                non_digit_mixed_filter,
            ]
        if status_view is SourceWordStatusView.UNLINKED:
            return [
                group_subquery.c.group_state == LexiconGroupState.UNREVIEWED.value,
                group_subquery.c.dominant_script_type == OccurrenceScriptType.ARMENIAN.value,
                non_digit_mixed_filter,
            ]
        if status_view is SourceWordStatusView.SUSPICIOUS:
            return [
                group_subquery.c.dominant_script_type != OccurrenceScriptType.ARMENIAN.value,
                group_subquery.c.group_state != LexiconGroupState.IGNORED_NOISE.value,
                non_digit_mixed_filter,
            ]
        if status_view is SourceWordStatusView.IGNORED:
            return [
                group_subquery.c.group_state == LexiconGroupState.IGNORED_NOISE.value,
                non_digit_mixed_filter,
            ]
        return []

    @staticmethod
    def _sample_pages_for_document_group(
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        normalized_form: str,
    ) -> list[int]:
        return list(
            session.scalars(
                select(Occurrence.page_number)
                .join(Document, Occurrence.document_id == Document.id)
                .where(
                    Document.user_id == user_id,
                    Occurrence.document_id == document_id,
                    Occurrence.normalized_token == normalized_form,
                )
                .distinct()
                .order_by(Occurrence.page_number.asc())
                .limit(5)
            )
        )

    @staticmethod
    def _sample_data_for_document_groups(
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        normalized_forms: list[str],
    ) -> dict[str, tuple[list[str], list[str]]]:
        if not normalized_forms:
            return {}

        sample_rank = (
            func.row_number()
            .over(
                partition_by=Occurrence.normalized_token,
                order_by=(
                    Occurrence.page_number.asc(),
                    Occurrence.char_start.asc().nullsfirst(),
                    Occurrence.created_at.asc(),
                ),
            )
            .label("sample_rank")
        )
        ranked_samples = (
            select(
                Occurrence.normalized_token.label("normalized_form"),
                Occurrence.token.label("token"),
                Occurrence.context_snippet.label("context_snippet"),
                sample_rank,
            )
            .join(Document, Occurrence.document_id == Document.id)
            .where(
                Document.user_id == user_id,
                Occurrence.document_id == document_id,
                Occurrence.normalized_token.in_(normalized_forms),
            )
            .subquery()
        )
        rows = session.execute(
            select(
                ranked_samples.c.normalized_form,
                ranked_samples.c.token,
                ranked_samples.c.context_snippet,
            )
            .where(ranked_samples.c.sample_rank <= 100)
            .order_by(ranked_samples.c.normalized_form.asc(), ranked_samples.c.sample_rank.asc())
        ).all()

        samples: dict[str, tuple[list[str], list[str]]] = {}
        seen_tokens: defaultdict[str, set[str]] = defaultdict(set)
        seen_contexts: defaultdict[str, set[str]] = defaultdict(set)
        for normalized_form, token, context_snippet in rows:
            sample_tokens, sample_contexts = samples.setdefault(normalized_form, ([], []))
            if token not in seen_tokens[normalized_form] and len(sample_tokens) < 5:
                seen_tokens[normalized_form].add(token)
                sample_tokens.append(token)
            if context_snippet not in seen_contexts[normalized_form] and len(sample_contexts) < 5:
                seen_contexts[normalized_form].add(context_snippet)
                sample_contexts.append(context_snippet)
        return samples

    @staticmethod
    def _sample_pages_for_document_groups(
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        normalized_forms: list[str],
    ) -> dict[str, list[int]]:
        if not normalized_forms:
            return {}

        distinct_pages = (
            select(
                Occurrence.normalized_token.label("normalized_form"),
                Occurrence.page_number.label("page_number"),
            )
            .join(Document, Occurrence.document_id == Document.id)
            .where(
                Document.user_id == user_id,
                Occurrence.document_id == document_id,
                Occurrence.normalized_token.in_(normalized_forms),
            )
            .distinct()
            .subquery()
        )
        page_rank = (
            func.row_number()
            .over(
                partition_by=distinct_pages.c.normalized_form,
                order_by=distinct_pages.c.page_number.asc(),
            )
            .label("page_rank")
        )
        ranked_pages = (
            select(
                distinct_pages.c.normalized_form,
                distinct_pages.c.page_number,
                page_rank,
            )
            .subquery()
        )
        rows = session.execute(
            select(ranked_pages.c.normalized_form, ranked_pages.c.page_number)
            .where(ranked_pages.c.page_rank <= 5)
            .order_by(ranked_pages.c.normalized_form.asc(), ranked_pages.c.page_number.asc())
        ).all()

        pages: defaultdict[str, list[int]] = defaultdict(list)
        for normalized_form, page_number in rows:
            pages[normalized_form].append(page_number)
        return dict(pages)

    @staticmethod
    def _to_reference_source_candidate(
        *,
        source: ReferenceSource,
        entry: ReferenceEntry,
        matching_lexemes: list[Lexeme],
    ) -> ReferenceSourceWordCandidateSummary:
        primary_lexeme = matching_lexemes[0] if matching_lexemes else None
        return ReferenceSourceWordCandidateSummary(
            source_id=str(source.id),
            source_title=source.display_name,
            source_subtitle=source.description,
            reference_link=f"/reference-sources/{source.id}",
            reference_entry_id=entry.id,
            surface_form=entry.surface_form,
            normalized_form=entry.normalized_form,
            import_method=source.last_import_method,
            warning_message=source.last_import_warning,
            linked_lexeme_id=primary_lexeme.id if primary_lexeme is not None else None,
            linked_lexeme_canonical_form=primary_lexeme.canonical_form if primary_lexeme is not None else None,
            matching_lexeme_count=len(matching_lexemes),
        )

def get_source_word_review_service() -> SourceWordReviewService:
    return SourceWordReviewService()
