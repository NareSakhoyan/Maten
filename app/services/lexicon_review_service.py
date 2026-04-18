from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import LexiconGroupReview, LexiconGroupReviewStatus
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
        session.commit()
        return normalized_values

    @staticmethod
    def _normalize_forms(values: list[str]) -> list[str]:
        normalized_values = normalize_token_list(values)
        if not normalized_values:
            raise ValueError("normalized_forms must contain at least one non-empty value.")
        return normalized_values


def get_lexicon_review_service() -> LexiconReviewService:
    return LexiconReviewService()
