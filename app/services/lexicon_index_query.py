from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.db.models import LexiconGroupIndex, LexiconGroupIndexDocument, OccurrenceScriptType
from app.schemas.lexicon import (
    LexiconGroupSortDirection,
    LexiconGroupSortKey,
    LexiconGroupState,
    LexiconGroupSummary,
    LexiconGroupView,
)
from app.schemas.reference import ReferenceStatusFilter
from app.utils.text_normalization import normalize_token
from app.utils.token_classification import is_suspicious_script_type, suspicion_reasons_for_script_type


def build_index_list_query(
    *,
    user_id: UUID,
    search: str | None,
    view: LexiconGroupView,
    document_id: UUID | None,
):
    if document_id is not None:
        base = (
            select(
                LexiconGroupIndex.normalized_form.label("normalized_form"),
                LexiconGroupIndexDocument.occurrence_count.label("occurrence_count"),
                LexiconGroupIndexDocument.page_count.label("page_count"),
                LexiconGroupIndex.linked_lexeme_id.label("linked_lexeme_id"),
                LexiconGroupIndex.linked_lexeme_canonical_form.label("linked_lexeme_canonical_form"),
                LexiconGroupIndex.group_state.label("group_state"),
                LexiconGroupIndex.dominant_script_type.label("dominant_script_type"),
                LexiconGroupIndex.sample_tokens.label("sample_tokens"),
                LexiconGroupIndex.sample_contexts.label("sample_contexts"),
                LexiconGroupIndexDocument.sample_tokens.label("document_sample_tokens"),
                LexiconGroupIndexDocument.sample_contexts.label("document_sample_contexts"),
            )
            .join(
                LexiconGroupIndexDocument,
                and_(
                    LexiconGroupIndex.user_id == LexiconGroupIndexDocument.user_id,
                    LexiconGroupIndex.normalized_form == LexiconGroupIndexDocument.normalized_form,
                ),
            )
            .where(
                LexiconGroupIndex.user_id == user_id,
                LexiconGroupIndexDocument.document_id == document_id,
            )
        )
    else:
        base = select(
            LexiconGroupIndex.normalized_form.label("normalized_form"),
            LexiconGroupIndex.occurrence_count.label("occurrence_count"),
            LexiconGroupIndex.document_count.label("document_count"),
            LexiconGroupIndex.page_count.label("page_count"),
            LexiconGroupIndex.linked_lexeme_id.label("linked_lexeme_id"),
            LexiconGroupIndex.linked_lexeme_canonical_form.label("linked_lexeme_canonical_form"),
            LexiconGroupIndex.group_state.label("group_state"),
            LexiconGroupIndex.dominant_script_type.label("dominant_script_type"),
            LexiconGroupIndex.sample_tokens.label("sample_tokens"),
            LexiconGroupIndex.sample_contexts.label("sample_contexts"),
            LexiconGroupIndex.sample_document_titles.label("sample_document_titles"),
        ).where(LexiconGroupIndex.user_id == user_id)

    if search:
        normalized_search = normalize_token(search)
        if normalized_search:
            base = base.where(LexiconGroupIndex.normalized_form.ilike(f"%{normalized_search}%"))

    filters = _view_filters(view)
    if filters:
        base = base.where(*filters)
    return base.subquery()


def _view_filters(view: LexiconGroupView) -> list[object]:
    if view is LexiconGroupView.CANDIDATES:
        return [
            LexiconGroupIndex.group_state == LexiconGroupState.UNREVIEWED.value,
            LexiconGroupIndex.dominant_script_type == OccurrenceScriptType.ARMENIAN,
        ]
    if view is LexiconGroupView.LINKED:
        return [LexiconGroupIndex.group_state == LexiconGroupState.LINKED.value]
    if view is LexiconGroupView.SUSPICIOUS:
        return [
            LexiconGroupIndex.dominant_script_type != OccurrenceScriptType.ARMENIAN,
            LexiconGroupIndex.group_state != LexiconGroupState.IGNORED_NOISE.value,
        ]
    if view is LexiconGroupView.IGNORED:
        return [LexiconGroupIndex.group_state == LexiconGroupState.IGNORED_NOISE.value]
    return []


def build_index_sort_order(
    subquery,
    *,
    sort_by: LexiconGroupSortKey | None,
    sort_dir: LexiconGroupSortDirection,
) -> tuple[object, ...]:
    descending = sort_dir is LexiconGroupSortDirection.DESC

    def _column_order(column) -> object:
        return column.desc() if descending else column.asc()

    if sort_by is LexiconGroupSortKey.NORMALIZED_FORM:
        return (_column_order(subquery.c.normalized_form),)
    if sort_by is LexiconGroupSortKey.OCCURRENCE_COUNT:
        return (_column_order(subquery.c.occurrence_count), subquery.c.normalized_form.asc())
    if sort_by is LexiconGroupSortKey.PAGE_COUNT:
        return (_column_order(subquery.c.page_count), subquery.c.normalized_form.asc())
    if sort_by is LexiconGroupSortKey.GROUP_STATE:
        return (_column_order(subquery.c.group_state), subquery.c.normalized_form.asc())
    if sort_by is LexiconGroupSortKey.DOMINANT_SCRIPT_TYPE:
        return (_column_order(subquery.c.dominant_script_type), subquery.c.normalized_form.asc())
    return (subquery.c.occurrence_count.desc(), subquery.c.normalized_form.asc())


def row_to_summary(row, *, document_id: UUID | None, reference_summary=None) -> LexiconGroupSummary:
    if document_id is not None:
        sample_tokens = list(row.document_sample_tokens or row.sample_tokens or [])
        sample_contexts = list(row.document_sample_contexts or row.sample_contexts or [])
        sample_document_titles: list[str] = []
    else:
        sample_tokens = list(row.sample_tokens or [])
        sample_contexts = list(row.sample_contexts or [])
        sample_document_titles = list(row.sample_document_titles or [])

    dominant_script_type = (
        row.dominant_script_type
        if isinstance(row.dominant_script_type, OccurrenceScriptType)
        else OccurrenceScriptType(row.dominant_script_type)
    )
    return LexiconGroupSummary(
        normalized_form=row.normalized_form,
        occurrence_count=row.occurrence_count,
        document_count=1 if document_id is not None else int(row.document_count),
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
        has_reference_match=reference_summary.has_reference_match if reference_summary else False,
        reference_match_count=reference_summary.reference_match_count if reference_summary else 0,
        best_reference_match=reference_summary.best_reference_match if reference_summary else None,
    )


def apply_reference_status_filter(subquery, *, reference_status: ReferenceStatusFilter, match_keys: set[str] | None):
    if reference_status is ReferenceStatusFilter.ALL or match_keys is None:
        return []
    if reference_status is ReferenceStatusFilter.MATCHED:
        return [subquery.c.normalized_form.in_(match_keys)]
    return [~subquery.c.normalized_form.in_(match_keys)]
