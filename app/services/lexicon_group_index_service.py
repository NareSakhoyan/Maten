from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.lexicon_index_projection import (
    rebuild_document_slices_sql,
    rebuild_global_rows_sql,
    sql_rebuild_available,
)
from app.db.models import (
    Document,
    Lexeme,
    LexemeForm,
    LexiconGroupIndex,
    LexiconGroupIndexDocument,
    LexiconGroupReview,
    LexiconGroupReviewStatus,
    Occurrence,
    OccurrenceScriptType,
)
from app.schemas.lexicon import LexiconGroupState
from app.utils.token_classification import classify_token


SAMPLE_LIMIT = 5
BATCH_FLUSH_SIZE = 25


class LexiconGroupIndexService:
    def clear_document_index(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
    ) -> None:
        affected_forms = list(
            session.scalars(
                select(LexiconGroupIndexDocument.normalized_form).where(
                    LexiconGroupIndexDocument.user_id == user_id,
                    LexiconGroupIndexDocument.document_id == document_id,
                )
            ).all()
        )
        if not affected_forms:
            return

        session.execute(
            delete(LexiconGroupIndexDocument).where(
                LexiconGroupIndexDocument.user_id == user_id,
                LexiconGroupIndexDocument.document_id == document_id,
            )
        )
        session.flush()
        self.rebuild_global_rows(
            session,
            user_id=user_id,
            normalized_forms=affected_forms,
        )

    def apply_page_occurrences(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        document_title: str,
        page_id: UUID,
        occurrences: list[Occurrence],
        rebuild_global: bool = True,
    ) -> list[str]:
        if not occurrences:
            return []

        grouped: dict[str, list[Occurrence]] = defaultdict(list)
        for occurrence in occurrences:
            grouped[occurrence.normalized_token].append(occurrence)

        affected_forms: list[str] = []
        for normalized_form, form_occurrences in grouped.items():
            self._apply_document_slice_delta(
                session,
                user_id=user_id,
                document_id=document_id,
                normalized_form=normalized_form,
                page_id=page_id,
                occurrences=form_occurrences,
            )
            affected_forms.append(normalized_form)

        if rebuild_global:
            self.rebuild_global_rows(
                session,
                user_id=user_id,
                normalized_forms=affected_forms,
                titles_by_document_id={document_id: document_title},
            )
        return affected_forms

    def load_user_script_counts(self, session: Session, *, user_id: UUID) -> dict[str, dict[str, int]]:
        script_counts_by_form: dict[str, dict[str, int]] = defaultdict(dict)
        script_rows = session.execute(
            select(Occurrence.normalized_token, Occurrence.script_type, func.count(Occurrence.id))
            .join(Document, Occurrence.document_id == Document.id)
            .where(Document.user_id == user_id)
            .group_by(Occurrence.normalized_token, Occurrence.script_type)
        ).all()
        for normalized_form, script_type, count in script_rows:
            script_counts_by_form[normalized_form][self._script_type_key(script_type)] = int(count)
        return dict(script_counts_by_form)

    def rebuild_document(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        document_title: str,
        user_script_counts: dict[str, dict[str, int]] | None = None,
        skip_global_rebuild: bool = False,
    ) -> list[str]:
        if sql_rebuild_available(session):
            forms_to_rebuild = rebuild_document_slices_sql(
                session,
                user_id=user_id,
                document_id=document_id,
            )
            if not skip_global_rebuild:
                self.rebuild_global_rows(
                    session,
                    user_id=user_id,
                    normalized_forms=forms_to_rebuild,
                    user_script_counts=user_script_counts,
                )
            return forms_to_rebuild

        affected_forms = session.scalars(
            select(LexiconGroupIndexDocument.normalized_form).where(
                LexiconGroupIndexDocument.user_id == user_id,
                LexiconGroupIndexDocument.document_id == document_id,
            )
        ).all()

        session.execute(
            delete(LexiconGroupIndexDocument).where(
                LexiconGroupIndexDocument.user_id == user_id,
                LexiconGroupIndexDocument.document_id == document_id,
            )
        )
        session.flush()

        rows = session.execute(
            select(
                Occurrence.normalized_token,
                Occurrence.page_id,
                Occurrence.token,
                Occurrence.context_snippet,
                Occurrence.script_type,
            )
            .where(Occurrence.document_id == document_id)
            .order_by(
                Occurrence.normalized_token.asc(),
                Occurrence.page_number.asc(),
                Occurrence.char_start.asc().nullsfirst(),
            )
        ).all()

        slice_data: dict[str, dict[str, object]] = {}
        for row in rows:
            normalized_form = row.normalized_token
            bucket = slice_data.setdefault(
                normalized_form,
                {
                    "occurrence_count": 0,
                    "page_ids": set(),
                    "sample_tokens": [],
                    "sample_contexts": [],
                    "seen_tokens": set(),
                    "seen_contexts": set(),
                    "script_counts": defaultdict(int),
                },
            )
            bucket["occurrence_count"] = int(bucket["occurrence_count"]) + 1
            page_ids = bucket["page_ids"]
            assert isinstance(page_ids, set)
            page_ids.add(str(row.page_id))
            script_counts = bucket["script_counts"]
            assert isinstance(script_counts, defaultdict)
            script_key = row.script_type.value if hasattr(row.script_type, "value") else str(row.script_type)
            script_counts[script_key] += 1
            seen_tokens = bucket["seen_tokens"]
            seen_contexts = bucket["seen_contexts"]
            sample_tokens = bucket["sample_tokens"]
            sample_contexts = bucket["sample_contexts"]
            assert isinstance(seen_tokens, set)
            assert isinstance(seen_contexts, set)
            assert isinstance(sample_tokens, list)
            assert isinstance(sample_contexts, list)
            if row.token not in seen_tokens and len(sample_tokens) < SAMPLE_LIMIT:
                seen_tokens.add(row.token)
                sample_tokens.append(row.token)
            if row.context_snippet not in seen_contexts and len(sample_contexts) < SAMPLE_LIMIT:
                seen_contexts.add(row.context_snippet)
                sample_contexts.append(row.context_snippet)

        pending_rows = 0
        for normalized_form, bucket in slice_data.items():
            page_ids = bucket["page_ids"]
            assert isinstance(page_ids, set)
            script_counts = bucket["script_counts"]
            assert isinstance(script_counts, defaultdict)
            session.add(
                LexiconGroupIndexDocument(
                    user_id=user_id,
                    normalized_form=normalized_form,
                    document_id=document_id,
                    occurrence_count=int(bucket["occurrence_count"]),
                    page_count=len(page_ids),
                    sample_tokens=list(bucket["sample_tokens"]),
                    sample_contexts=list(bucket["sample_contexts"]),
                    page_ids=sorted(page_ids),
                    script_counts=dict(script_counts),
                )
            )
            affected_forms.append(normalized_form)
            pending_rows += 1
            if pending_rows >= BATCH_FLUSH_SIZE:
                session.flush()
                pending_rows = 0

        if pending_rows:
            session.flush()
        forms_to_rebuild = list(set(affected_forms))
        if not skip_global_rebuild:
            self.rebuild_global_rows(
                session,
                user_id=user_id,
                normalized_forms=forms_to_rebuild,
                user_script_counts=user_script_counts,
            )
        return forms_to_rebuild

    def rebuild_global_rows(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: list[str],
        user_script_counts: dict[str, dict[str, int]] | None = None,
        titles_by_document_id: dict[UUID, str] | None = None,
    ) -> None:
        if not normalized_forms:
            return

        unique_forms = list(dict.fromkeys(normalized_forms))
        if sql_rebuild_available(session) and titles_by_document_id is None:
            rebuild_global_rows_sql(session, user_id=user_id, normalized_forms=unique_forms)
            self.sync_metadata(session, user_id=user_id, normalized_forms=unique_forms)
            self.remove_orphan_global_rows(session, user_id=user_id)
            return

        if user_script_counts is None:
            user_script_counts = self.load_user_script_counts(session, user_id=user_id)

        slice_rows = session.scalars(
            select(LexiconGroupIndexDocument).where(
                LexiconGroupIndexDocument.user_id == user_id,
                LexiconGroupIndexDocument.normalized_form.in_(unique_forms),
            )
        ).all()
        slices_by_form: dict[str, list[LexiconGroupIndexDocument]] = defaultdict(list)
        for slice_row in slice_rows:
            slices_by_form[slice_row.normalized_form].append(slice_row)

        document_ids = {slice_row.document_id for slice_row in slice_rows}
        resolved_titles = dict(titles_by_document_id or {})
        missing_document_ids = document_ids.difference(resolved_titles)
        if missing_document_ids:
            resolved_titles.update(
                {
                    row.id: row.title
                    for row in session.execute(
                        select(Document.id, Document.title).where(Document.id.in_(missing_document_ids))
                    ).all()
                }
            )

        existing_global_rows = {
            row.normalized_form: row
            for row in session.scalars(
                select(LexiconGroupIndex).where(
                    LexiconGroupIndex.user_id == user_id,
                    LexiconGroupIndex.normalized_form.in_(unique_forms),
                )
            ).all()
        }

        pending_rows = 0
        for normalized_form in unique_forms:
            form_slice_rows = slices_by_form.get(normalized_form, [])
            self._rebuild_global_row_from_slices(
                session,
                user_id=user_id,
                normalized_form=normalized_form,
                slice_rows=form_slice_rows,
                script_counts=self._resolve_script_counts(
                    session,
                    user_id=user_id,
                    normalized_form=normalized_form,
                    slice_rows=form_slice_rows,
                    user_script_counts=user_script_counts,
                ),
                titles_by_document_id=resolved_titles,
                existing_row=existing_global_rows.get(normalized_form),
            )
            pending_rows += 1
            if pending_rows >= BATCH_FLUSH_SIZE:
                session.flush()
                pending_rows = 0

        if pending_rows:
            session.flush()

        self.sync_metadata(session, user_id=user_id, normalized_forms=unique_forms)

    def rebuild_user(self, session: Session, *, user_id: UUID) -> int:
        session.execute(delete(LexiconGroupIndex).where(LexiconGroupIndex.user_id == user_id))
        session.execute(delete(LexiconGroupIndexDocument).where(LexiconGroupIndexDocument.user_id == user_id))
        session.flush()

        use_sql_rebuild = sql_rebuild_available(session)
        user_script_counts = (
            None if use_sql_rebuild else self.load_user_script_counts(session, user_id=user_id)
        )
        document_rows = session.execute(
            select(Document.id, Document.title).where(Document.user_id == user_id)
        ).all()
        affected_forms: list[str] = []
        for document_id, document_title in document_rows:
            affected_forms.extend(
                self.rebuild_document(
                    session,
                    user_id=user_id,
                    document_id=document_id,
                    document_title=document_title,
                    user_script_counts=user_script_counts,
                    skip_global_rebuild=True,
                )
            )
        self.rebuild_global_rows(
            session,
            user_id=user_id,
            normalized_forms=affected_forms,
            user_script_counts=user_script_counts,
        )
        return len(document_rows)

    def sync_metadata(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: list[str],
    ) -> None:
        if not normalized_forms:
            return

        user_key = str(user_id)
        lexeme_map = {
            row.normalized_form: row
            for row in session.execute(
                select(
                    LexemeForm.normalized_form,
                    Lexeme.id,
                    Lexeme.canonical_form,
                )
                .join(Lexeme, Lexeme.id == LexemeForm.lexeme_id)
                .where(
                    LexemeForm.user_id == user_key,
                    LexemeForm.normalized_form.in_(normalized_forms),
                )
            ).all()
        }
        ignored_forms = set(
            session.scalars(
                select(LexiconGroupReview.normalized_form).where(
                    LexiconGroupReview.user_id == user_key,
                    LexiconGroupReview.normalized_form.in_(normalized_forms),
                    LexiconGroupReview.review_status == LexiconGroupReviewStatus.IGNORED_NOISE,
                )
            ).all()
        )

        for normalized_form in normalized_forms:
            row = session.get(LexiconGroupIndex, {"user_id": user_id, "normalized_form": normalized_form})
            if row is None:
                continue

            lexeme = lexeme_map.get(normalized_form)
            if lexeme is not None:
                row.linked_lexeme_id = lexeme.id
                row.linked_lexeme_canonical_form = lexeme.canonical_form
                row.group_state = LexiconGroupState.LINKED.value
            elif normalized_form in ignored_forms:
                row.linked_lexeme_id = None
                row.linked_lexeme_canonical_form = None
                row.group_state = LexiconGroupState.IGNORED_NOISE.value
            else:
                row.linked_lexeme_id = None
                row.linked_lexeme_canonical_form = None
                row.group_state = LexiconGroupState.UNREVIEWED.value
            row.updated_at = datetime.now(timezone.utc)

    def remove_orphan_global_rows(self, session: Session, *, user_id: UUID) -> None:
        session.execute(
            delete(LexiconGroupIndex).where(
                LexiconGroupIndex.user_id == user_id,
                LexiconGroupIndex.occurrence_count <= 0,
            )
        )

    def _apply_document_slice_delta(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        normalized_form: str,
        page_id: UUID,
        occurrences: list[Occurrence],
    ) -> None:
        row = session.get(
            LexiconGroupIndexDocument,
            {
                "user_id": user_id,
                "normalized_form": normalized_form,
                "document_id": document_id,
            },
        )
        if row is None:
            row = LexiconGroupIndexDocument(
                user_id=user_id,
                normalized_form=normalized_form,
                document_id=document_id,
            )
            session.add(row)
            session.flush()

        page_id_key = str(page_id)
        page_ids = set(row.page_ids or [])
        row.occurrence_count += len(occurrences)
        page_ids.add(page_id_key)
        row.page_ids = sorted(page_ids)
        row.page_count = len(page_ids)

        seen_tokens = set(row.sample_tokens or [])
        seen_contexts = set(row.sample_contexts or [])
        sample_tokens = list(row.sample_tokens or [])
        sample_contexts = list(row.sample_contexts or [])
        script_counts = dict(row.script_counts or {})
        for occurrence in occurrences:
            if occurrence.token not in seen_tokens and len(sample_tokens) < SAMPLE_LIMIT:
                seen_tokens.add(occurrence.token)
                sample_tokens.append(occurrence.token)
            if occurrence.context_snippet not in seen_contexts and len(sample_contexts) < SAMPLE_LIMIT:
                seen_contexts.add(occurrence.context_snippet)
                sample_contexts.append(occurrence.context_snippet)
            script_key = self._script_type_key(occurrence.script_type)
            script_counts[script_key] = script_counts.get(script_key, 0) + 1
        row.sample_tokens = sample_tokens
        row.sample_contexts = sample_contexts
        row.script_counts = script_counts
        row.updated_at = datetime.now(timezone.utc)

    def _rebuild_global_row(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        document_title: str | None = None,
        script_counts: dict[str, int] | None = None,
        sync_metadata: bool = True,
    ) -> None:
        slice_rows = session.scalars(
            select(LexiconGroupIndexDocument).where(
                LexiconGroupIndexDocument.user_id == user_id,
                LexiconGroupIndexDocument.normalized_form == normalized_form,
            )
        ).all()
        titles_by_document_id: dict[UUID, str] = {}
        if document_title and slice_rows:
            titles_by_document_id = {slice_rows[0].document_id: document_title}
        self._rebuild_global_row_from_slices(
            session,
            user_id=user_id,
            normalized_form=normalized_form,
            slice_rows=slice_rows,
            script_counts=script_counts,
            titles_by_document_id=titles_by_document_id,
        )
        if sync_metadata and slice_rows:
            self.sync_metadata(session, user_id=user_id, normalized_forms=[normalized_form])

    def _rebuild_global_row_from_slices(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        slice_rows: list[LexiconGroupIndexDocument],
        script_counts: dict[str, int] | None,
        titles_by_document_id: dict[UUID, str],
        existing_row: LexiconGroupIndex | None = None,
    ) -> None:
        if not slice_rows:
            session.execute(
                delete(LexiconGroupIndex).where(
                    LexiconGroupIndex.user_id == user_id,
                    LexiconGroupIndex.normalized_form == normalized_form,
                )
            )
            return

        occurrence_count = sum(row.occurrence_count for row in slice_rows)
        document_count = len(slice_rows)
        page_ids: set[str] = set()
        sample_tokens: list[str] = []
        sample_contexts: list[str] = []
        sample_document_titles: list[str] = []
        seen_tokens: set[str] = set()
        seen_contexts: set[str] = set()
        seen_titles: set[str] = set()

        for slice_row in slice_rows:
            page_ids.update(slice_row.page_ids or [])
            for token in slice_row.sample_tokens or []:
                if token not in seen_tokens and len(sample_tokens) < SAMPLE_LIMIT:
                    seen_tokens.add(token)
                    sample_tokens.append(token)
            for context in slice_row.sample_contexts or []:
                if context not in seen_contexts and len(sample_contexts) < SAMPLE_LIMIT:
                    seen_contexts.add(context)
                    sample_contexts.append(context)
            title = titles_by_document_id.get(slice_row.document_id)
            if title and title not in seen_titles and len(sample_document_titles) < SAMPLE_LIMIT:
                seen_titles.add(title)
                sample_document_titles.append(title)

        resolved_script_counts = dict(script_counts or {})

        dominant_script_type = self._dominant_script_type(resolved_script_counts)

        row = existing_row
        if row is None:
            row = session.get(LexiconGroupIndex, {"user_id": user_id, "normalized_form": normalized_form})
        if row is None:
            row = LexiconGroupIndex(
                user_id=user_id,
                normalized_form=normalized_form,
                dominant_script_type=dominant_script_type,
            )
            session.add(row)
            session.flush()

        row.occurrence_count = occurrence_count
        row.document_count = document_count
        row.page_count = len(page_ids)
        row.dominant_script_type = dominant_script_type
        row.script_counts = dict(resolved_script_counts)
        row.sample_tokens = sample_tokens
        row.sample_contexts = sample_contexts
        row.sample_document_titles = sample_document_titles
        row.updated_at = datetime.now(timezone.utc)

    def _resolve_script_counts(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        slice_rows: list[LexiconGroupIndexDocument],
        user_script_counts: dict[str, dict[str, int]] | None,
    ) -> dict[str, int] | None:
        merged = self._merge_script_counts_from_slices(slice_rows)
        if merged:
            return merged
        if user_script_counts is not None:
            cached = user_script_counts.get(normalized_form)
            if cached:
                return cached

        script_rows = session.execute(
            select(Occurrence.script_type, func.count(Occurrence.id))
            .join(Document, Occurrence.document_id == Document.id)
            .where(
                Document.user_id == user_id,
                Occurrence.normalized_token == normalized_form,
            )
            .group_by(Occurrence.script_type)
        ).all()
        if not script_rows:
            return None
        resolved: dict[str, int] = {}
        for script_type, count in script_rows:
            resolved[self._script_type_key(script_type)] = int(count)
        return resolved

    @staticmethod
    def _merge_script_counts_from_slices(
        slice_rows: list[LexiconGroupIndexDocument],
    ) -> dict[str, int]:
        merged: dict[str, int] = defaultdict(int)
        has_counts = False
        for slice_row in slice_rows:
            for script_key, count in (slice_row.script_counts or {}).items():
                has_counts = True
                merged[script_key] += int(count)
        return dict(merged) if has_counts else {}

    @staticmethod
    def _script_type_key(script_type: OccurrenceScriptType | str | None) -> str:
        if script_type is None:
            return OccurrenceScriptType.OTHER.value
        if hasattr(script_type, "value"):
            return str(script_type.value)
        return str(script_type)

    @staticmethod
    def _dominant_script_type(script_counts: dict[str, int]) -> OccurrenceScriptType:
        if not script_counts:
            return OccurrenceScriptType.OTHER
        dominant_key = sorted(
            script_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[0][0]
        try:
            return OccurrenceScriptType(dominant_key)
        except ValueError:
            return classify_token("").script_type


def get_lexicon_group_index_service() -> LexiconGroupIndexService:
    return LexiconGroupIndexService()
