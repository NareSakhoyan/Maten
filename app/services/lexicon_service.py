from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, case, distinct, func, literal, select
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentPage,
    LexiconGroupReview,
    LexiconGroupReviewStatus,
    Lexeme,
    LexemeForm,
    Occurrence,
    OccurrenceScriptType,
    ReferenceMatch,
    ReferenceMatchTargetType,
)
from app.schemas.lexicon import (
    LexiconGroupDetail,
    LexiconGroupOccurrenceRead,
    LexiconGroupSortDirection,
    LexiconGroupSortKey,
    LexiconGroupState,
    LexiconGroupSummary,
    LexiconGroupView,
)
from app.schemas.reference import ReferenceStatusFilter
from app.db.models import LexiconGroupIndex
from app.services.lexicon_group_index_service import get_lexicon_group_index_service
from app.services.lexicon_index_query import (
    apply_reference_status_filter,
    build_index_list_query,
    build_index_sort_order,
    row_to_summary,
)
from app.utils.snippets import context_snippet_highlight_range
from app.utils.text_normalization import normalize_token
from app.utils.token_classification import is_suspicious_script_type, suspicion_reasons_for_script_type


class LexiconService:
    def __init__(self, *, reference_matching_service=None, index_service=None) -> None:
        if reference_matching_service is None:
            from app.services.reference_matching_service import ReferenceMatchingService

            reference_matching_service = ReferenceMatchingService(lexicon_service=self)
        self.reference_matching_service = reference_matching_service
        self.index_service = index_service or get_lexicon_group_index_service()

    def list_groups(
        self,
        session: Session,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
        search: str | None = None,
        view: LexiconGroupView = LexiconGroupView.CANDIDATES,
        document_id: UUID | None = None,
        reference_status: ReferenceStatusFilter = ReferenceStatusFilter.ALL,
        sort_by: LexiconGroupSortKey | None = None,
        sort_dir: LexiconGroupSortDirection = LexiconGroupSortDirection.DESC,
        include_reference_summary: bool = False,
    ) -> tuple[list[LexiconGroupSummary], int]:
        needs_reference_summary = include_reference_summary or reference_status is not ReferenceStatusFilter.ALL
        return self._list_groups_from_index(
            session,
            user_id=user_id,
            limit=limit,
            offset=offset,
            search=search,
            view=view,
            document_id=document_id,
            reference_status=reference_status,
            sort_by=sort_by,
            sort_dir=sort_dir,
            include_reference_summary=needs_reference_summary,
        )

    def _list_groups_from_index(
        self,
        session: Session,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
        search: str | None,
        view: LexiconGroupView,
        document_id: UUID | None,
        reference_status: ReferenceStatusFilter,
        sort_by: LexiconGroupSortKey | None,
        sort_dir: LexiconGroupSortDirection,
        include_reference_summary: bool = False,
    ) -> tuple[list[LexiconGroupSummary], int]:
        needs_reference_summary = include_reference_summary or reference_status is not ReferenceStatusFilter.ALL
        subquery = build_index_list_query(
            user_id=user_id,
            search=search,
            view=view,
            document_id=document_id,
        )
        filters: list[object] = []
        match_keys = None
        if reference_status is not ReferenceStatusFilter.ALL:
            match_keys = self.reference_matching_service.reference_status_filter_for_session(
                session,
                user_id=user_id,
                target_type=ReferenceMatchTargetType.LEXICON_GROUP,
            )
            filters.extend(
                apply_reference_status_filter(
                    subquery,
                    reference_status=reference_status,
                    match_keys=match_keys,
                )
            )

        total = session.scalar(select(func.count()).select_from(subquery).where(*filters)) or 0
        rows = session.execute(
            select(subquery)
            .where(*filters)
            .order_by(*build_index_sort_order(subquery, sort_by=sort_by, sort_dir=sort_dir))
            .limit(limit)
            .offset(offset)
        ).all()

        normalized_forms_page = [row.normalized_form for row in rows]
        if not normalized_forms_page:
            return [], total

        reference_summary_map = (
            self.reference_matching_service.group_summary_map(
                session,
                user_id=user_id,
                normalized_forms=normalized_forms_page,
            )
            if needs_reference_summary
            else {}
        )
        items = [
            row_to_summary(
                row,
                document_id=document_id,
                reference_summary=reference_summary_map.get(row.normalized_form),
            )
            for row in rows
        ]
        return items, total

    def get_group_detail(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        occurrence_cap: int = 100,
    ) -> LexiconGroupDetail | None:
        normalized = normalize_token(normalized_form)
        if not normalized:
            return None

        row = session.get(
            LexiconGroupIndex,
            {"user_id": user_id, "normalized_form": normalized},
        )
        if row is None:
            return None

        occurrence_rows = session.execute(
            select(
                Occurrence.id.label("id"),
                Occurrence.document_id.label("document_id"),
                Document.title.label("document_title"),
                Document.original_filename.label("original_filename"),
                Occurrence.page_id.label("page_id"),
                Occurrence.page_number.label("page_number"),
                DocumentPage.page_image_bucket.label("page_image_bucket"),
                DocumentPage.page_image_path.label("page_image_path"),
                Occurrence.token.label("token"),
                Occurrence.normalized_token.label("normalized_token"),
                Occurrence.context_snippet.label("context_snippet"),
                Occurrence.char_start.label("char_start"),
                Occurrence.char_end.label("char_end"),
                DocumentPage.raw_extracted_text.label("page_text"),
                Occurrence.created_at.label("created_at"),
            )
            .join(Document, Occurrence.document_id == Document.id)
            .join(DocumentPage, Occurrence.page_id == DocumentPage.id)
            .where(
                Document.user_id == user_id,
                Occurrence.normalized_token == normalized,
            )
            .order_by(
                Occurrence.page_number.asc(),
                Occurrence.char_start.asc().nullsfirst(),
                Occurrence.created_at.asc(),
            )
            .limit(occurrence_cap)
        ).all()

        occurrences = []
        for occurrence in occurrence_rows:
            highlight_start, highlight_end = context_snippet_highlight_range(
                occurrence.page_text,
                occurrence.char_start,
                occurrence.char_end,
                occurrence.context_snippet,
                token=occurrence.token,
            )
            occurrences.append(
                LexiconGroupOccurrenceRead(
                    id=occurrence.id,
                    document_id=occurrence.document_id,
                    document_title=occurrence.document_title,
                    original_filename=occurrence.original_filename,
                    page_id=occurrence.page_id,
                    page_number=occurrence.page_number,
                    page_image_available=bool(occurrence.page_image_bucket and occurrence.page_image_path),
                    page_image_api_path=(
                        f"/api/v1/documents/{occurrence.document_id}/pages/{occurrence.page_id}/image"
                        if occurrence.page_image_bucket and occurrence.page_image_path
                        else None
                    ),
                    token=occurrence.token,
                    normalized_token=occurrence.normalized_token,
                    context_snippet=occurrence.context_snippet,
                    context_highlight_start=highlight_start,
                    context_highlight_end=highlight_end,
                    created_at=occurrence.created_at,
                )
            )

        dominant_script_type = (
            row.dominant_script_type
            if isinstance(row.dominant_script_type, OccurrenceScriptType)
            else OccurrenceScriptType(row.dominant_script_type)
        )
        reference_summary = self.reference_matching_service.group_summary_map(
            session,
            user_id=user_id,
            normalized_forms=[normalized],
        )[normalized]
        group_state = (
            LexiconGroupState(row.group_state)
            if isinstance(row.group_state, str)
            else LexiconGroupState(row.group_state.value)
        )
        return LexiconGroupDetail(
            normalized_form=row.normalized_form,
            occurrence_count=row.occurrence_count,
            document_count=row.document_count,
            page_count=row.page_count,
            linked_lexeme_id=row.linked_lexeme_id,
            linked_lexeme_canonical_form=row.linked_lexeme_canonical_form,
            group_state=group_state,
            dominant_script_type=dominant_script_type,
            is_suspicious=is_suspicious_script_type(dominant_script_type),
            suspicion_reasons=suspicion_reasons_for_script_type(dominant_script_type),
            has_reference_match=reference_summary.has_reference_match,
            reference_match_count=reference_summary.reference_match_count,
            best_reference_match=reference_summary.best_reference_match,
            occurrences=occurrences,
        )

    def _build_group_subquery(
        self,
        *,
        user_id: UUID,
        search: str | None,
        document_id: UUID | None,
    ):
        user_key = str(user_id)
        filters = self._base_filters(user_id=user_id, search=search, document_id=document_id)
        lexeme_join = and_(
            LexemeForm.user_id == user_key,
            LexemeForm.normalized_form == Occurrence.normalized_token,
        )
        review_join = and_(
            LexiconGroupReview.user_id == user_key,
            LexiconGroupReview.normalized_form == Occurrence.normalized_token,
        )

        script_counts = (
            select(
                Occurrence.normalized_token.label("normalized_form"),
                Occurrence.script_type.label("script_type"),
                func.count(Occurrence.id).label("script_count"),
            )
            .join(Document, Occurrence.document_id == Document.id)
            .where(*filters)
            .group_by(Occurrence.normalized_token, Occurrence.script_type)
            .subquery()
        )
        dominant_script_ranked = (
            select(
                script_counts.c.normalized_form,
                script_counts.c.script_type,
                func.row_number()
                .over(
                    partition_by=script_counts.c.normalized_form,
                    order_by=(
                        script_counts.c.script_count.desc(),
                        script_counts.c.script_type.asc(),
                    ),
                )
                .label("script_rank"),
            )
            .subquery()
        )
        dominant_script = (
            select(
                dominant_script_ranked.c.normalized_form,
                dominant_script_ranked.c.script_type,
            )
            .where(dominant_script_ranked.c.script_rank == 1)
            .subquery()
        )

        group_state = case(
            (
                Lexeme.id.is_not(None),
                literal(LexiconGroupState.LINKED.value),
            ),
            (
                LexiconGroupReview.review_status == LexiconGroupReviewStatus.IGNORED_NOISE,
                literal(LexiconGroupState.IGNORED_NOISE.value),
            ),
            else_=literal(LexiconGroupState.UNREVIEWED.value),
        ).label("group_state")

        return (
            select(
                Occurrence.normalized_token.label("normalized_form"),
                func.count(Occurrence.id).label("occurrence_count"),
                func.count(distinct(Occurrence.document_id)).label("document_count"),
                func.count(distinct(Occurrence.page_id)).label("page_count"),
                Lexeme.id.label("linked_lexeme_id"),
                Lexeme.canonical_form.label("linked_lexeme_canonical_form"),
                dominant_script.c.script_type.label("dominant_script_type"),
                group_state,
            )
            .join(Document, Occurrence.document_id == Document.id)
            .outerjoin(LexemeForm, lexeme_join)
            .outerjoin(Lexeme, Lexeme.id == LexemeForm.lexeme_id)
            .outerjoin(LexiconGroupReview, review_join)
            .outerjoin(dominant_script, dominant_script.c.normalized_form == Occurrence.normalized_token)
            .where(*filters)
            .group_by(
                Occurrence.normalized_token,
                Lexeme.id,
                Lexeme.canonical_form,
                dominant_script.c.script_type,
                LexiconGroupReview.review_status,
            )
            .subquery()
        )

    @staticmethod
    def _base_filters(
        *,
        user_id: UUID,
        search: str | None,
        document_id: UUID | None,
    ) -> list[object]:
        filters: list[object] = [Document.user_id == user_id]
        if document_id is not None:
            filters.append(Occurrence.document_id == document_id)
        if search:
            normalized_search = normalize_token(search)
            if normalized_search:
                filters.append(Occurrence.normalized_token.ilike(f"%{normalized_search}%"))
        return filters

    @staticmethod
    def _view_filters(group_subquery, view: LexiconGroupView) -> list[object]:
        if view is LexiconGroupView.CANDIDATES:
            return [
                group_subquery.c.group_state == LexiconGroupState.UNREVIEWED.value,
                group_subquery.c.dominant_script_type == OccurrenceScriptType.ARMENIAN.value,
            ]
        if view is LexiconGroupView.LINKED:
            return [group_subquery.c.group_state == LexiconGroupState.LINKED.value]
        if view is LexiconGroupView.SUSPICIOUS:
            return [
                group_subquery.c.dominant_script_type != OccurrenceScriptType.ARMENIAN.value,
                group_subquery.c.group_state != LexiconGroupState.IGNORED_NOISE.value,
            ]
        if view is LexiconGroupView.IGNORED:
            return [group_subquery.c.group_state == LexiconGroupState.IGNORED_NOISE.value]
        return []

    def _load_group_samples(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        document_id: UUID | None,
    ) -> tuple[list[str], list[str], list[str]]:
        filters = [
            Document.user_id == user_id,
            Occurrence.normalized_token == normalized_form,
        ]
        if document_id is not None:
            filters.append(Occurrence.document_id == document_id)

        rows = session.execute(
            select(Occurrence.token, Occurrence.context_snippet, Document.title)
            .join(Document, Occurrence.document_id == Document.id)
            .where(*filters)
            .order_by(
                Occurrence.page_number.asc(),
                Occurrence.char_start.asc().nullsfirst(),
                Occurrence.created_at.asc(),
            )
            .limit(100)
        ).all()

        sample_tokens: list[str] = []
        sample_contexts: list[str] = []
        sample_document_titles: list[str] = []
        seen_tokens: set[str] = set()
        seen_contexts: set[str] = set()
        seen_titles: set[str] = set()

        for token, context_snippet, title in rows:
            if token not in seen_tokens and len(sample_tokens) < 5:
                seen_tokens.add(token)
                sample_tokens.append(token)
            if context_snippet not in seen_contexts and len(sample_contexts) < 5:
                seen_contexts.add(context_snippet)
                sample_contexts.append(context_snippet)
            if title not in seen_titles and len(sample_document_titles) < 5:
                seen_titles.add(title)
                sample_document_titles.append(title)

        return sample_tokens, sample_contexts, sample_document_titles

    def _reference_status_filters(
        self,
        session: Session,
        group_subquery,
        *,
        user_id: UUID,
        reference_status: ReferenceStatusFilter,
    ) -> list[object]:
        if reference_status is ReferenceStatusFilter.ALL:
            return []

        match_keys = self.reference_matching_service.reference_status_filter_for_session(
            session,
            user_id=user_id,
            target_type=ReferenceMatchTargetType.LEXICON_GROUP,
        )
        if match_keys is None:
            return []
        if reference_status is ReferenceStatusFilter.MATCHED:
            return [group_subquery.c.normalized_form.in_(match_keys)]
        return [~group_subquery.c.normalized_form.in_(match_keys)]


def get_lexicon_service() -> LexiconService:
    from app.services.reference_matching_service import ReferenceMatchingService

    return LexiconService(reference_matching_service=ReferenceMatchingService())
