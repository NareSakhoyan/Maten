from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, case, distinct, func, literal, select
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    LexiconGroupReview,
    LexiconGroupReviewStatus,
    Lexeme,
    LexemeForm,
    Occurrence,
    OccurrenceScriptType,
)
from app.schemas.lexicon import (
    LexiconGroupDetail,
    LexiconGroupOccurrenceRead,
    LexiconGroupState,
    LexiconGroupSummary,
    LexiconGroupView,
)
from app.utils.text_normalization import normalize_token
from app.utils.token_classification import is_suspicious_script_type, suspicion_reasons_for_script_type


class LexiconService:
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
    ) -> tuple[list[LexiconGroupSummary], int]:
        group_subquery = self._build_group_subquery(
            user_id=user_id,
            search=search,
            document_id=document_id,
        )
        filters = self._view_filters(group_subquery, view)

        total = session.scalar(select(func.count()).select_from(group_subquery).where(*filters)) or 0
        rows = session.execute(
            select(group_subquery)
            .where(*filters)
            .order_by(
                group_subquery.c.occurrence_count.desc(),
                group_subquery.c.normalized_form.asc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()

        items: list[LexiconGroupSummary] = []
        for row in rows:
            sample_tokens, sample_contexts, sample_document_titles = self._load_group_samples(
                session,
                user_id=user_id,
                normalized_form=row.normalized_form,
                document_id=document_id,
            )
            dominant_script_type = OccurrenceScriptType(row.dominant_script_type)
            items.append(
                LexiconGroupSummary(
                    normalized_form=row.normalized_form,
                    occurrence_count=row.occurrence_count,
                    document_count=row.document_count,
                    page_count=row.page_count,
                    sample_tokens=sample_tokens,
                    sample_contexts=sample_contexts,
                    sample_document_titles=sample_document_titles,
                    linked_lexeme_id=row.linked_lexeme_id,
                    linked_lexeme_canonical_form=row.linked_lexeme_canonical_form,
                    group_state=LexiconGroupState(row.group_state),
                    dominant_script_type=dominant_script_type,
                    is_suspicious=is_suspicious_script_type(dominant_script_type),
                    suspicion_reasons=suspicion_reasons_for_script_type(dominant_script_type),
                )
            )

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

        group_subquery = self._build_group_subquery(user_id=user_id, search=None, document_id=None)
        row = session.execute(
            select(group_subquery).where(group_subquery.c.normalized_form == normalized)
        ).one_or_none()
        if row is None:
            return None

        occurrences = [
            LexiconGroupOccurrenceRead(
                id=occurrence.id,
                document_id=occurrence.document_id,
                document_title=occurrence.document_title,
                original_filename=occurrence.original_filename,
                page_id=occurrence.page_id,
                page_number=occurrence.page_number,
                token=occurrence.token,
                normalized_token=occurrence.normalized_token,
                context_snippet=occurrence.context_snippet,
                created_at=occurrence.created_at,
            )
            for occurrence in session.execute(
                select(
                    Occurrence.id.label("id"),
                    Occurrence.document_id.label("document_id"),
                    Document.title.label("document_title"),
                    Document.original_filename.label("original_filename"),
                    Occurrence.page_id.label("page_id"),
                    Occurrence.page_number.label("page_number"),
                    Occurrence.token.label("token"),
                    Occurrence.normalized_token.label("normalized_token"),
                    Occurrence.context_snippet.label("context_snippet"),
                    Occurrence.created_at.label("created_at"),
                )
                .join(Document, Occurrence.document_id == Document.id)
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
        ]

        dominant_script_type = OccurrenceScriptType(row.dominant_script_type)
        return LexiconGroupDetail(
            normalized_form=row.normalized_form,
            occurrence_count=row.occurrence_count,
            document_count=row.document_count,
            page_count=row.page_count,
            linked_lexeme_id=row.linked_lexeme_id,
            linked_lexeme_canonical_form=row.linked_lexeme_canonical_form,
            group_state=LexiconGroupState(row.group_state),
            dominant_script_type=dominant_script_type,
            is_suspicious=is_suspicious_script_type(dominant_script_type),
            suspicion_reasons=suspicion_reasons_for_script_type(dominant_script_type),
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

        for row in rows:
            if row.token not in seen_tokens and len(sample_tokens) < 5:
                seen_tokens.add(row.token)
                sample_tokens.append(row.token)
            if row.context_snippet not in seen_contexts and len(sample_contexts) < 5:
                seen_contexts.add(row.context_snippet)
                sample_contexts.append(row.context_snippet)
            if row.title not in seen_titles and len(sample_document_titles) < 5:
                seen_titles.add(row.title)
                sample_document_titles.append(row.title)
            if (
                len(sample_tokens) >= 5
                and len(sample_contexts) >= 5
                and len(sample_document_titles) >= 5
            ):
                break

        return sample_tokens, sample_contexts, sample_document_titles


def get_lexicon_service() -> LexiconService:
    return LexiconService()
