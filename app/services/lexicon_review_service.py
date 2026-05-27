from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import LexiconGroupReview, LexiconGroupReviewStatus
from app.schemas.lexeme import LexemeMergeGroupsRequest
from app.utils.text_normalization import normalize_token_list


class LexiconReviewService:
    def ignore_groups(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: list[str],
        reviewer_note: str | None = None,
    ) -> list[str]:
        normalized_values = self._normalize_forms(normalized_forms)
        existing_reviews = {
            review.normalized_form: review
            for review in session.scalars(
                select(LexiconGroupReview).where(
                    LexiconGroupReview.user_id == str(user_id),
                    LexiconGroupReview.normalized_form.in_(normalized_values),
                )
            )
        }

        for normalized_form in normalized_values:
            existing_review = existing_reviews.get(normalized_form)
            if existing_review is None:
                session.add(
                    LexiconGroupReview(
                        user_id=str(user_id),
                        normalized_form=normalized_form,
                        review_status=LexiconGroupReviewStatus.IGNORED_NOISE,
                        reviewer_note=reviewer_note,
                    )
                )
                continue

            existing_review.review_status = LexiconGroupReviewStatus.IGNORED_NOISE
            existing_review.reviewer_note = reviewer_note

        from app.services.document_workflow_service import get_document_workflow_service

        get_document_workflow_service().sync_for_normalized_forms(
            session,
            user_id=user_id,
            normalized_forms=normalized_values,
        )
        from app.services.lexicon_group_index_service import get_lexicon_group_index_service

        get_lexicon_group_index_service().sync_metadata(
            session,
            user_id=user_id,
            normalized_forms=normalized_values,
        )
        session.commit()
        return normalized_values

    def unignore_groups(self, session: Session, *, user_id: UUID, normalized_forms: list[str]) -> list[str]:
        normalized_values = self._normalize_forms(normalized_forms)
        session.execute(
            delete(LexiconGroupReview).where(
                LexiconGroupReview.user_id == str(user_id),
                LexiconGroupReview.normalized_form.in_(normalized_values),
            )
        )
        from app.services.document_workflow_service import get_document_workflow_service

        get_document_workflow_service().sync_for_normalized_forms(
            session,
            user_id=user_id,
            normalized_forms=normalized_values,
        )
        from app.services.lexicon_group_index_service import get_lexicon_group_index_service

        get_lexicon_group_index_service().sync_metadata(
            session,
            user_id=user_id,
            normalized_forms=normalized_values,
        )
        session.commit()
        return normalized_values

    def link_groups_to_lexeme(
        self,
        session: Session,
        *,
        user_id: UUID,
        lexeme_id: UUID,
        normalized_forms: list[str],
    ) -> tuple[str, list[str]]:
        from app.services.lexeme_service import get_lexeme_service

        normalized_values = self._normalize_forms(normalized_forms)
        lexeme = get_lexeme_service().merge_groups(
            session,
            user_id=user_id,
            lexeme_id=lexeme_id,
            request=LexemeMergeGroupsRequest(normalized_forms=normalized_values),
        )
        if lexeme is None:
            raise ValueError("Lexeme not found.")
        return lexeme.canonical_form, normalized_values

    @staticmethod
    def _normalize_forms(values: list[str]) -> list[str]:
        normalized_values = normalize_token_list(values)
        if not normalized_values:
            raise ValueError("normalized_forms must contain at least one non-empty value.")
        return normalized_values


def get_lexicon_review_service() -> LexiconReviewService:
    return LexiconReviewService()
