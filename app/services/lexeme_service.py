from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Text, cast, func, select, update
from sqlalchemy.orm import Session

from app.db.models import Document, Lexeme, LexemeForm, Occurrence, ReferenceMatchTargetType
from app.schemas.lexeme import (
    LexemeCreateRequest,
    LexemeDetail,
    LexemeMergeGroupsRequest,
    LexemeSummary,
    LexemeUpdateRequest,
)
from app.schemas.reference import ReferenceStatusFilter
from app.utils.text_normalization import normalize_token, normalize_token_list


class LexemeConflictError(Exception):
    def __init__(
        self,
        *,
        message: str,
        conflicting_normalized_forms: list[str],
        conflicting_lexeme_ids: list[UUID],
    ) -> None:
        super().__init__(message)
        self.message = message
        self.conflicting_normalized_forms = conflicting_normalized_forms
        self.conflicting_lexeme_ids = conflicting_lexeme_ids

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "message": self.message,
            "conflicting_normalized_forms": self.conflicting_normalized_forms,
        }
        if self.conflicting_lexeme_ids:
            payload["conflicting_lexeme_ids"] = [str(lexeme_id) for lexeme_id in self.conflicting_lexeme_ids]
        return payload


class LexemeService:
    def __init__(self, *, reference_matching_service=None) -> None:
        if reference_matching_service is None:
            from app.services.reference_matching_service import ReferenceMatchingService

            reference_matching_service = ReferenceMatchingService()
        self.reference_matching_service = reference_matching_service

    def create_lexeme(
        self,
        session: Session,
        *,
        user_id: UUID,
        request: LexemeCreateRequest,
    ) -> LexemeDetail:
        user_key = str(user_id)
        canonical_form = self._clean_required_value(request.canonical_form, field_name="canonical_form")
        normalized_forms = self._normalize_forms(request.normalized_forms)

        conflicts = self._find_conflicts(session, user_key=user_key, normalized_forms=normalized_forms)
        if conflicts:
            raise self._conflict_error(conflicts)

        lexeme = Lexeme(
            user_id=user_key,
            canonical_form=canonical_form,
            canonical_normalized_form=normalize_token(canonical_form),
            notes=request.notes,
            status=request.status,
        )
        session.add(lexeme)
        session.flush()

        session.add_all(
            [
                LexemeForm(
                    lexeme_id=lexeme.id,
                    user_id=user_key,
                    normalized_form=normalized_form,
                )
                for normalized_form in normalized_forms
            ]
        )
        self._assign_occurrences(session, user_id=user_id, normalized_forms=normalized_forms, lexeme_id=lexeme.id)
        session.commit()
        return self.get_lexeme_detail(session, user_id=user_id, lexeme_id=lexeme.id)

    def list_lexemes(
        self,
        session: Session,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
        search: str | None = None,
        reference_status: ReferenceStatusFilter = ReferenceStatusFilter.ALL,
    ) -> tuple[list[LexemeSummary], int]:
        user_key = str(user_id)
        filters = [Lexeme.user_id == user_key]
        if search:
            normalized_search = normalize_token(search)
            if normalized_search:
                filters.append(
                    (Lexeme.canonical_normalized_form.ilike(f"%{normalized_search}%"))
                    | (Lexeme.canonical_form.ilike(f"%{search.strip()}%"))
                )
        filters.extend(
            self._reference_status_filters(
                session,
                user_id=user_id,
                reference_status=reference_status,
            )
        )

        total = session.scalar(select(func.count(Lexeme.id)).where(*filters)) or 0

        form_counts = (
            select(
                LexemeForm.lexeme_id.label("lexeme_id"),
                func.count(LexemeForm.id).label("form_count"),
            )
            .group_by(LexemeForm.lexeme_id)
            .subquery()
        )
        occurrence_counts = (
            select(
                Occurrence.lexeme_id.label("lexeme_id"),
                func.count(Occurrence.id).label("occurrence_count"),
            )
            .where(Occurrence.lexeme_id.is_not(None))
            .group_by(Occurrence.lexeme_id)
            .subquery()
        )

        rows = session.execute(
            select(
                Lexeme,
                func.coalesce(form_counts.c.form_count, 0).label("form_count"),
                func.coalesce(occurrence_counts.c.occurrence_count, 0).label("occurrence_count"),
            )
            .outerjoin(form_counts, form_counts.c.lexeme_id == Lexeme.id)
            .outerjoin(occurrence_counts, occurrence_counts.c.lexeme_id == Lexeme.id)
            .where(*filters)
            .order_by(Lexeme.created_at.desc(), Lexeme.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()

        reference_summary_map = self.reference_matching_service.lexeme_summary_map(
            session,
            user_id=user_id,
            lexeme_ids=[row.Lexeme.id for row in rows],
        )
        items = [
            LexemeSummary(
                id=row.Lexeme.id,
                canonical_form=row.Lexeme.canonical_form,
                canonical_normalized_form=row.Lexeme.canonical_normalized_form,
                status=row.Lexeme.status,
                notes=row.Lexeme.notes,
                form_count=row.form_count,
                occurrence_count=row.occurrence_count,
                created_at=row.Lexeme.created_at,
                updated_at=row.Lexeme.updated_at,
                has_reference_match=reference_summary_map[str(row.Lexeme.id)].has_reference_match,
                reference_match_count=reference_summary_map[str(row.Lexeme.id)].reference_match_count,
                best_reference_match=reference_summary_map[str(row.Lexeme.id)].best_reference_match,
            )
            for row in rows
        ]
        return items, total

    def get_lexeme_detail(self, session: Session, *, user_id: UUID, lexeme_id: UUID) -> LexemeDetail:
        lexeme = self.get_user_lexeme(session, user_id=user_id, lexeme_id=lexeme_id)
        if lexeme is None:
            raise ValueError("Lexeme not found.")

        normalized_forms = list(
            session.scalars(
                select(LexemeForm.normalized_form)
                .where(
                    LexemeForm.lexeme_id == lexeme.id,
                    LexemeForm.user_id == str(user_id),
                )
                .order_by(LexemeForm.normalized_form.asc())
            )
        )
        occurrence_count = session.scalar(
            select(func.count(Occurrence.id)).where(Occurrence.lexeme_id == lexeme.id)
        ) or 0
        context_rows = session.execute(
            select(Occurrence.context_snippet)
            .where(Occurrence.lexeme_id == lexeme.id)
            .order_by(Occurrence.created_at.desc())
            .limit(50)
        ).all()

        sample_contexts: list[str] = []
        seen_contexts: set[str] = set()
        for row in context_rows:
            if row.context_snippet in seen_contexts:
                continue
            seen_contexts.add(row.context_snippet)
            sample_contexts.append(row.context_snippet)
            if len(sample_contexts) >= 5:
                break

        reference_summary = self.reference_matching_service.lexeme_summary_map(
            session,
            user_id=user_id,
            lexeme_ids=[lexeme.id],
        )[str(lexeme.id)]

        return LexemeDetail(
            id=lexeme.id,
            canonical_form=lexeme.canonical_form,
            canonical_normalized_form=lexeme.canonical_normalized_form,
            status=lexeme.status,
            notes=lexeme.notes,
            normalized_forms=normalized_forms,
            occurrence_count=occurrence_count,
            sample_contexts=sample_contexts,
            created_at=lexeme.created_at,
            updated_at=lexeme.updated_at,
            has_reference_match=reference_summary.has_reference_match,
            reference_match_count=reference_summary.reference_match_count,
            best_reference_match=reference_summary.best_reference_match,
        )

    def update_lexeme(
        self,
        session: Session,
        *,
        user_id: UUID,
        lexeme_id: UUID,
        request: LexemeUpdateRequest,
    ) -> LexemeDetail | None:
        lexeme = self.get_user_lexeme(session, user_id=user_id, lexeme_id=lexeme_id)
        if lexeme is None:
            return None

        if request.canonical_form is not None:
            canonical_form = self._clean_required_value(request.canonical_form, field_name="canonical_form")
            lexeme.canonical_form = canonical_form
            lexeme.canonical_normalized_form = normalize_token(canonical_form)
        if request.notes is not None:
            lexeme.notes = request.notes
        if request.status is not None:
            lexeme.status = request.status

        session.commit()
        return self.get_lexeme_detail(session, user_id=user_id, lexeme_id=lexeme_id)

    def merge_groups(
        self,
        session: Session,
        *,
        user_id: UUID,
        lexeme_id: UUID,
        request: LexemeMergeGroupsRequest,
    ) -> LexemeDetail | None:
        lexeme = self.get_user_lexeme(session, user_id=user_id, lexeme_id=lexeme_id)
        if lexeme is None:
            return None

        normalized_forms = self._normalize_forms(request.normalized_forms)
        conflicts = self._find_conflicts(
            session,
            user_key=str(user_id),
            normalized_forms=normalized_forms,
            exclude_lexeme_id=lexeme_id,
        )
        if conflicts:
            raise self._conflict_error(conflicts)

        existing_forms = set(
            session.scalars(
                select(LexemeForm.normalized_form).where(
                    LexemeForm.lexeme_id == lexeme_id,
                    LexemeForm.user_id == str(user_id),
                    LexemeForm.normalized_form.in_(normalized_forms),
                )
            )
        )
        missing_forms = [form for form in normalized_forms if form not in existing_forms]
        if missing_forms:
            session.add_all(
                [
                    LexemeForm(
                        lexeme_id=lexeme_id,
                        user_id=str(user_id),
                        normalized_form=normalized_form,
                    )
                    for normalized_form in missing_forms
                ]
            )

        self._assign_occurrences(session, user_id=user_id, normalized_forms=normalized_forms, lexeme_id=lexeme_id)
        session.commit()
        return self.get_lexeme_detail(session, user_id=user_id, lexeme_id=lexeme_id)

    @staticmethod
    def get_user_lexeme(session: Session, *, user_id: UUID, lexeme_id: UUID) -> Lexeme | None:
        return session.scalar(
            select(Lexeme).where(
                Lexeme.id == lexeme_id,
                Lexeme.user_id == str(user_id),
            )
        )

    @staticmethod
    def _clean_required_value(value: str, *, field_name: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{field_name} must not be empty.")
        return cleaned

    @staticmethod
    def _normalize_forms(values: Sequence[str]) -> list[str]:
        normalized_forms = normalize_token_list(values)
        if not normalized_forms:
            raise ValueError("normalized_forms must contain at least one non-empty value.")
        return normalized_forms

    @staticmethod
    def _find_conflicts(
        session: Session,
        *,
        user_key: str,
        normalized_forms: Sequence[str],
        exclude_lexeme_id: UUID | None = None,
    ) -> list[LexemeForm]:
        statement = select(LexemeForm).where(
            LexemeForm.user_id == user_key,
            LexemeForm.normalized_form.in_(normalized_forms),
        )
        if exclude_lexeme_id is not None:
            statement = statement.where(LexemeForm.lexeme_id != exclude_lexeme_id)
        return list(session.scalars(statement))

    @staticmethod
    def _conflict_error(conflicts: Sequence[LexemeForm]) -> LexemeConflictError:
        normalized_forms = sorted({conflict.normalized_form for conflict in conflicts})
        conflicting_lexeme_ids = sorted({conflict.lexeme_id for conflict in conflicts}, key=str)
        return LexemeConflictError(
            message="One or more normalized forms already belong to another lexeme.",
            conflicting_normalized_forms=normalized_forms,
            conflicting_lexeme_ids=conflicting_lexeme_ids,
        )

    @staticmethod
    def _assign_occurrences(
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: Sequence[str],
        lexeme_id: UUID,
    ) -> None:
        document_ids = select(Document.id).where(Document.user_id == user_id)
        session.execute(
            update(Occurrence)
            .where(
                Occurrence.document_id.in_(document_ids),
                Occurrence.normalized_token.in_(normalized_forms),
            )
            .values(lexeme_id=lexeme_id)
        )

    def _reference_status_filters(
        self,
        session: Session,
        *,
        user_id: UUID,
        reference_status: ReferenceStatusFilter,
    ) -> list[object]:
        if reference_status is ReferenceStatusFilter.ALL:
            return []

        match_keys = self.reference_matching_service.reference_status_filter_for_session(
            session,
            user_id=user_id,
            target_type=ReferenceMatchTargetType.LEXEME,
        )
        if match_keys is None:
            return []
        lexeme_key = func.replace(cast(Lexeme.id, Text), "-", "")
        normalized_match_keys = select(func.replace(match_keys.subquery().c.target_key, "-", ""))
        if reference_status is ReferenceStatusFilter.MATCHED:
            return [lexeme_key.in_(normalized_match_keys)]
        return [~lexeme_key.in_(normalized_match_keys)]


def get_lexeme_service() -> LexemeService:
    from app.services.reference_matching_service import ReferenceMatchingService

    return LexemeService(reference_matching_service=ReferenceMatchingService())
