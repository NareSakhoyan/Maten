from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Document, Lexeme, LexemeForm, Occurrence, OccurrenceScriptType, ReferenceEntry, ReferenceSource
from app.schemas.lexicon import LexiconGroupState, LexiconGroupView
from app.schemas.reference import ReferenceStatusFilter
from app.schemas.word import DocumentWordCandidateSummary, ReferenceSourceWordCandidateSummary, SourceWordStatusView
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
    ) -> None:
        self.reference_matching_service = reference_matching_service or get_reference_matching_service()
        self.lexicon_service = lexicon_service or LexiconService(reference_matching_service=self.reference_matching_service)
        self.word_evidence_service = word_evidence_service or get_word_evidence_service()

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
            self.lexicon_service._reference_status_filters(  # noqa: SLF001
                session,
                group_subquery,
                user_id=user_id,
                reference_status=reference_status,
            )
        )

        total = session.scalar(select(func.count()).select_from(group_subquery).where(*filters)) or 0
        rows = session.execute(
            select(group_subquery)
            .where(*filters)
            .order_by(group_subquery.c.occurrence_count.desc(), group_subquery.c.normalized_form.asc())
            .limit(limit)
            .offset(offset)
        ).all()
        normalized_forms = [row.normalized_form for row in rows]
        reference_summary_map = self.reference_matching_service.group_summary_map(
            session,
            user_id=user_id,
            normalized_forms=normalized_forms,
        )

        items: list[DocumentWordCandidateSummary] = []
        for row in rows:
            sample_tokens, sample_contexts, _ = self.lexicon_service._load_group_samples(  # noqa: SLF001
                session,
                user_id=user_id,
                normalized_form=row.normalized_form,
                document_id=document.id,
            )
            sample_pages = self._sample_pages_for_document_group(
                session,
                user_id=user_id,
                document_id=document.id,
                normalized_form=row.normalized_form,
            )
            dominant_script_type = OccurrenceScriptType(row.dominant_script_type)
            reference_summary = reference_summary_map[row.normalized_form]
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
                    has_reference_match=reference_summary.has_reference_match,
                    reference_match_count=reference_summary.reference_match_count,
                    best_reference_match=reference_summary.best_reference_match,
                )
            )
        return items, total

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
        if status_view is SourceWordStatusView.ALL:
            return []
        if status_view is SourceWordStatusView.LINKED:
            return [group_subquery.c.group_state == LexiconGroupState.LINKED.value]
        if status_view is SourceWordStatusView.UNLINKED:
            return [
                group_subquery.c.group_state == LexiconGroupState.UNREVIEWED.value,
                group_subquery.c.dominant_script_type == OccurrenceScriptType.ARMENIAN.value,
            ]
        if status_view is SourceWordStatusView.SUSPICIOUS:
            return [
                group_subquery.c.dominant_script_type != OccurrenceScriptType.ARMENIAN.value,
                group_subquery.c.group_state != LexiconGroupState.IGNORED_NOISE.value,
            ]
        if status_view is SourceWordStatusView.IGNORED:
            return [group_subquery.c.group_state == LexiconGroupState.IGNORED_NOISE.value]
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
