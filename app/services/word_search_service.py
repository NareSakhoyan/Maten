from __future__ import annotations

from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Document, Lexeme, LexemeForm, Occurrence, ReferenceEntry, ReferenceSource
from app.schemas.word import (
    TrustedExternalWordCheckSource,
    WordCheckLexeme,
    WordCheckResponse,
    WordEvidenceItem,
    TrustedExternalLookupStatus,
    WordSearchCategory,
    WordSearchMode,
    WordSearchResponse,
    WordSearchResultGroup,
    WordEvidenceSourceType,
)
from app.services.external_lookup_service import ExternalLookupBatch, ExternalLookupService, get_external_lookup_service
from app.services.word_evidence_service import WordEvidenceService, get_word_evidence_service
from app.utils.text_normalization import normalize_token


FUZZY_THRESHOLD = 85.0
FUZZY_FORM_LIMIT = 5


class WordSearchService:
    def __init__(
        self,
        *,
        word_evidence_service: WordEvidenceService | None = None,
        external_lookup_service: ExternalLookupService | None = None,
    ) -> None:
        self.word_evidence_service = word_evidence_service or get_word_evidence_service()
        self.external_lookup_service = external_lookup_service or get_external_lookup_service()

    def search(
        self,
        session: Session,
        *,
        user_id: UUID,
        query: str,
        mode: WordSearchMode,
        include_categories: list[WordSearchCategory] | None,
        include_external: bool = False,
        provider_keys: list[str] | None = None,
        limit_per_category: int,
    ) -> WordSearchResponse:
        normalized_query = normalize_token(query)
        if not normalized_query:
            raise ValueError("q must not be empty.")

        categories = include_categories if include_categories is not None else [
            WordSearchCategory.LEXICON,
            WordSearchCategory.IMPORTED_BOOKS,
            WordSearchCategory.REFERENCE_SOURCES,
        ]
        if include_external and WordSearchCategory.TRUSTED_EXTERNAL not in categories:
            categories = [*categories, WordSearchCategory.TRUSTED_EXTERNAL]
        groups: list[WordSearchResultGroup] = []
        for category in categories:
            items, external_status = self._category_items(
                session,
                user_id=user_id,
                query=query.strip(),
                normalized_query=normalized_query,
                category=category,
                mode=mode,
                provider_keys=provider_keys,
            )
            groups.append(
                WordSearchResultGroup(
                    category=category,
                    items=items[:limit_per_category],
                    total=len(items),
                    status=external_status,
                )
            )
        return WordSearchResponse(
            query=query.strip(),
            normalized_query=normalized_query,
            mode=mode,
            groups=groups,
        )

    def check(
        self,
        session: Session,
        *,
        user_id: UUID,
        query: str,
        include_external: bool = False,
        provider_keys: list[str] | None = None,
    ) -> WordCheckResponse:
        normalized_query = normalize_token(query)
        if not normalized_query:
            raise ValueError("q must not be empty.")

        user_key = str(user_id)
        lexeme_rows = list(
            session.scalars(
                select(Lexeme)
                .where(
                    Lexeme.user_id == user_key,
                    (
                        Lexeme.canonical_normalized_form == normalized_query
                    )
                    | Lexeme.id.in_(
                        select(LexemeForm.lexeme_id).where(
                            LexemeForm.user_id == user_key,
                            LexemeForm.normalized_form == normalized_query,
                        )
                    ),
                )
                .order_by(Lexeme.created_at.asc(), Lexeme.id.asc())
            )
        )
        trusted_external_batch = self.external_lookup_service.lookup(
            session,
            user_id=user_id,
            query=query.strip(),
            mode=WordSearchMode.NORMALIZED,
            provider_keys=provider_keys,
        ) if include_external else ExternalLookupBatch(items=[], status=TrustedExternalLookupStatus.UNAVAILABLE)
        return WordCheckResponse(
            query=query.strip(),
            normalized_query=normalized_query,
            exists_in_lexicon=bool(lexeme_rows),
            matching_lexeme_count=len(lexeme_rows),
            matching_lexemes=[
                WordCheckLexeme(
                    lexeme_id=lexeme.id,
                    canonical_form=lexeme.canonical_form,
                    canonical_normalized_form=lexeme.canonical_normalized_form,
                )
                for lexeme in lexeme_rows
            ],
            found_in_imported_books=bool(
                session.scalar(
                    select(func.count(Occurrence.id))
                    .join(Document, Occurrence.document_id == Document.id)
                    .where(
                        Document.user_id == user_id,
                        Occurrence.normalized_token == normalized_query,
                    )
                )
            ),
            found_in_reference_sources=bool(
                session.scalar(
                    select(func.count(ReferenceEntry.id))
                    .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
                    .where(
                        ReferenceSource.user_id == user_key,
                        ReferenceEntry.normalized_form == normalized_query,
                    )
                )
            ),
            found_in_trusted_external=bool(trusted_external_batch.items),
            trusted_external_status=trusted_external_batch.status if include_external else None,
            trusted_external_match_count=len(trusted_external_batch.items),
            trusted_external_sources=[
                TrustedExternalWordCheckSource(
                    provider_display_name=item.provider_display_name,
                    matched_form=item.matched_form,
                    reference_link=item.reference_link,
                )
                for item in trusted_external_batch.items
            ],
        )

    def _category_items(
        self,
        session: Session,
        *,
        user_id: UUID,
        query: str,
        normalized_query: str,
        category: WordSearchCategory,
        mode: WordSearchMode,
        provider_keys: list[str] | None = None,
    ) -> tuple[list[WordEvidenceItem], TrustedExternalLookupStatus | None]:
        if category is WordSearchCategory.TRUSTED_EXTERNAL:
            return self._trusted_external_items(
                session,
                user_id=user_id,
                query=query,
                normalized_query=normalized_query,
                mode=mode,
                provider_keys=provider_keys,
            )

        if mode is WordSearchMode.FUZZY:
            normalized_forms = self._fuzzy_forms_for_category(
                session,
                user_id=user_id,
                normalized_query=normalized_query,
                category=category,
            )
        else:
            normalized_forms = [normalized_query]

        items: list[WordEvidenceItem] = []
        for normalized_form in normalized_forms:
            if category is WordSearchCategory.LEXICON:
                items.extend(
                    self.word_evidence_service.lexicon_evidence(
                        session,
                        user_id=user_id,
                        normalized_form=normalized_form,
                    )
                )
            elif category is WordSearchCategory.IMPORTED_BOOKS:
                items.extend(
                    self.word_evidence_service.document_occurrence_evidence(
                        session,
                        user_id=user_id,
                        normalized_form=normalized_form,
                    )
                )
            elif category is WordSearchCategory.REFERENCE_SOURCES:
                items.extend(
                    self.word_evidence_service.reference_source_evidence(
                        session,
                        user_id=user_id,
                        normalized_form=normalized_form,
                    )
                )

        if mode is WordSearchMode.EXACT:
            items = [
                item
                for item in items
                if item.word_form == query or item.normalized_form == normalized_query
            ]
        elif mode is WordSearchMode.NORMALIZED:
            items = [item for item in items if item.normalized_form == normalized_query]

        items.sort(
            key=lambda item: (
                item.source_title,
                item.page_number or 0,
                item.word_form,
                str(item.occurrence_id or item.lexeme_id or item.source_id),
            )
        )
        return items, None

    def _trusted_external_items(
        self,
        session: Session,
        *,
        user_id: UUID,
        query: str,
        normalized_query: str,
        mode: WordSearchMode,
        provider_keys: list[str] | None,
    ) -> tuple[list[WordEvidenceItem], TrustedExternalLookupStatus]:
        batch = self.external_lookup_service.lookup(
            session,
            user_id=user_id,
            query=query,
            mode=mode,
            provider_keys=provider_keys,
        )
        result_items = [
            WordEvidenceItem(
                word_form=item.matched_form,
                matched_form=item.matched_form,
                normalized_form=item.normalized_form or normalized_query,
                source_type=WordEvidenceSourceType.TRUSTED_EXTERNAL,
                source_id=item.provider_key,
                source_title=item.source_title or item.provider_display_name,
                source_subtitle=item.source_subtitle,
                context_snippet=item.snippet,
                reference_link=item.reference_link,
                provider_key=item.provider_key,
                provider_display_name=item.provider_display_name,
                match_type=item.match_type,
                match_score=item.match_score,
                fetched_at=item.fetched_at,
                created_at=item.created_at or item.fetched_at,
            )
            for item in batch.items
        ]
        result_items.sort(
            key=lambda item: (
                item.provider_display_name or "",
                item.source_title,
                item.word_form,
                item.reference_link or "",
            )
        )
        return result_items, batch.status

    def _fuzzy_forms_for_category(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_query: str,
        category: WordSearchCategory,
    ) -> list[str]:
        if category is WordSearchCategory.LEXICON:
            candidates = self._lexicon_normalized_forms(session, user_id=user_id)
        elif category is WordSearchCategory.IMPORTED_BOOKS:
            candidates = list(
                session.scalars(
                    select(Occurrence.normalized_token)
                    .join(Document, Occurrence.document_id == Document.id)
                    .where(Document.user_id == user_id)
                    .distinct()
                    .order_by(Occurrence.normalized_token.asc())
                )
            )
        else:
            candidates = list(
                session.scalars(
                    select(ReferenceEntry.normalized_form)
                    .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
                    .where(ReferenceSource.user_id == str(user_id))
                    .distinct()
                    .order_by(ReferenceEntry.normalized_form.asc())
                )
            )
        scored = [
            (candidate, self._similarity_score(normalized_query, candidate))
            for candidate in candidates
            if candidate
        ]
        filtered = [candidate for candidate, score in scored if score >= FUZZY_THRESHOLD]
        filtered.sort(key=lambda candidate: (-self._similarity_score(normalized_query, candidate), candidate))
        return filtered[:FUZZY_FORM_LIMIT]

    @staticmethod
    def _lexicon_normalized_forms(session: Session, *, user_id: UUID) -> list[str]:
        user_key = str(user_id)
        canonical = list(
            session.scalars(
                select(Lexeme.canonical_normalized_form)
                .where(Lexeme.user_id == user_key)
                .order_by(Lexeme.canonical_normalized_form.asc())
            )
        )
        forms = list(
            session.scalars(
                select(LexemeForm.normalized_form)
                .where(LexemeForm.user_id == user_key)
                .order_by(LexemeForm.normalized_form.asc())
            )
        )
        return [value for value in dict.fromkeys(canonical + forms) if value]

    @staticmethod
    def _similarity_score(left: str, right: str) -> float:
        return float(SequenceMatcher(a=left, b=right).ratio() * 100)


def get_word_search_service() -> WordSearchService:
    return WordSearchService()
