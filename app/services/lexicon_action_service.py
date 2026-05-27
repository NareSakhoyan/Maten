from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import LexemeStatus
from app.schemas.lexeme import LexemeCreateRequest
from app.schemas.lexicon import (
    LexiconActionRequest,
    LexiconActionResponse,
    LexiconActionType,
    LexiconGroupState,
)
from app.services.lexeme_service import LexemeConflictError, get_lexeme_service
from app.services.lexicon_review_service import get_lexicon_review_service


class LexiconActionService:
    def run_action(
        self,
        session: Session,
        *,
        user_id: UUID,
        request: LexiconActionRequest,
    ) -> LexiconActionResponse:
        review_service = get_lexicon_review_service()
        normalized_forms = request.normalized_forms

        if request.action is LexiconActionType.IGNORE:
            forms = review_service.ignore_groups(
                session,
                user_id=user_id,
                normalized_forms=normalized_forms,
                reviewer_note=request.reviewer_note,
            )
            return LexiconActionResponse(
                action=request.action,
                normalized_forms=forms,
                group_state=LexiconGroupState.IGNORED_NOISE,
            )

        if request.action is LexiconActionType.UNIGNORE:
            forms = review_service.unignore_groups(
                session,
                user_id=user_id,
                normalized_forms=normalized_forms,
            )
            return LexiconActionResponse(
                action=request.action,
                normalized_forms=forms,
                group_state=LexiconGroupState.UNREVIEWED,
            )

        if request.action is LexiconActionType.MERGE_INTO_LEXEME:
            if request.lexeme_id is None:
                raise ValueError("lexeme_id is required for merge_into_lexeme.")
            canonical_form, forms = review_service.link_groups_to_lexeme(
                session,
                user_id=user_id,
                lexeme_id=request.lexeme_id,
                normalized_forms=normalized_forms,
            )
            return LexiconActionResponse(
                action=request.action,
                normalized_forms=forms,
                group_state=LexiconGroupState.LINKED,
                lexeme_id=request.lexeme_id,
                lexeme_canonical_form=canonical_form,
            )

        if request.action is LexiconActionType.CREATE_LEXEME:
            if not request.canonical_form:
                raise ValueError("canonical_form is required for create_lexeme.")
            detail = get_lexeme_service().create_lexeme(
                session,
                user_id=user_id,
                request=LexemeCreateRequest(
                    canonical_form=request.canonical_form,
                    normalized_forms=normalized_forms,
                    status=request.status or LexemeStatus.DRAFT,
                    notes=request.notes,
                ),
            )
            return LexiconActionResponse(
                action=request.action,
                normalized_forms=normalized_forms,
                group_state=LexiconGroupState.LINKED,
                lexeme_id=detail.id,
                lexeme_canonical_form=detail.canonical_form,
            )

        raise ValueError(f"Unsupported action: {request.action!r}")


def get_lexicon_action_service() -> LexiconActionService:
    return LexiconActionService()
