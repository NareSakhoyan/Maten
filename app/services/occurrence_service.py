from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Occurrence
from app.services.tokenization_service import TokenizationService, get_tokenization_service
from app.utils.token_classification import classify_token


class OccurrenceService:
    def __init__(self, tokenization_service: TokenizationService | None = None) -> None:
        self.tokenization_service = tokenization_service or get_tokenization_service()

    def store_page_occurrences(
        self,
        session: Session,
        *,
        document_id,
        page_id,
        page_number: int,
        text: str,
    ) -> list[Occurrence]:
        tokens = self.tokenization_service.tokenize(text)
        occurrences = [
            Occurrence(
                document_id=document_id,
                page_id=page_id,
                page_number=page_number,
                token=token.token,
                normalized_token=token.normalized_token,
                script_type=classification.script_type,
                has_digits=classification.has_digits,
                has_latin=classification.has_latin,
                has_armenian=classification.has_armenian,
                token_length=classification.token_length,
                context_snippet=token.context_snippet,
                char_start=token.char_start,
                char_end=token.char_end,
            )
            for token in tokens
            for classification in [classify_token(token.token)]
        ]
        if occurrences:
            session.add_all(occurrences)
        return occurrences

    def backfill_missing_classification(self, session: Session, *, batch_size: int = 1000) -> int:
        rows = list(
            session.scalars(
                select(Occurrence)
                .where(Occurrence.script_type.is_(None))
                .order_by(Occurrence.created_at.asc(), Occurrence.id.asc())
                .limit(batch_size)
            )
        )
        for occurrence in rows:
            classification = classify_token(occurrence.token)
            occurrence.script_type = classification.script_type
            occurrence.has_digits = classification.has_digits
            occurrence.has_latin = classification.has_latin
            occurrence.has_armenian = classification.has_armenian
            occurrence.token_length = classification.token_length
        return len(rows)


def get_occurrence_service() -> OccurrenceService:
    return OccurrenceService()
