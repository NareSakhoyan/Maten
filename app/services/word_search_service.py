from __future__ import annotations

from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    Lexeme,
    LexemeForm,
    MorphologyAnalysis,
    MorphologyAnalysisStatus,
    Occurrence,
    ReferenceEntry,
    ReferenceSource,
)
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
        trusted_external_batch = self.external_lookup_service.lookup_cached(
            session,
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
            found_in_imported_books=self._found_in_imported_books(
                session,
                user_id=user_id,
                normalized_query=normalized_query,
            ),
            found_in_reference_sources=self._found_in_reference_sources(
                session,
                user_key=user_key,
                normalized_query=normalized_query,
            ),
            found_in_trusted_external=self._found_in_trusted_external(
                query=query.strip(),
                normalized_query=normalized_query,
                cached_batch=trusted_external_batch,
            ),
            trusted_external_status=self._trusted_external_check_status(
                include_external=include_external,
                cached_batch=trusted_external_batch,
                query=query.strip(),
                normalized_query=normalized_query,
            ),
            trusted_external_match_count=self._trusted_external_match_count(
                query=query.strip(),
                normalized_query=normalized_query,
                cached_batch=trusted_external_batch,
            ),
            trusted_external_sources=self._trusted_external_check_sources(
                query=query.strip(),
                normalized_query=normalized_query,
                cached_batch=trusted_external_batch,
            ),
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

        if mode is WordSearchMode.FUZZY and len(normalized_query) <= 2:
            normalized_forms = []
        elif mode is WordSearchMode.FUZZY:
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
                    self.word_evidence_service._merge_document_evidence(
                        self.word_evidence_service.document_occurrence_evidence(
                            session,
                            user_id=user_id,
                            normalized_form=normalized_form,
                        ),
                        self.word_evidence_service.document_lemma_evidence(
                            session,
                            user_id=user_id,
                            lemma_normalized=normalized_form,
                        ),
                    )
                )
            elif category is WordSearchCategory.REFERENCE_SOURCES:
                items.extend(
                    self.word_evidence_service._merge_reference_evidence(
                        self.word_evidence_service.reference_source_evidence(
                            session,
                            user_id=user_id,
                            normalized_form=normalized_form,
                        ),
                        self.word_evidence_service.reference_lemma_evidence(
                            session,
                            user_id=user_id,
                            lemma_normalized=normalized_form,
                        ),
                    )
                )

        items = self._filter_items_by_mode(
            items,
            query=query,
            normalized_query=normalized_query,
            mode=mode,
        )

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
        if mode is WordSearchMode.FUZZY and len(normalized_query) <= 2:
            return [], TrustedExternalLookupStatus.NO_RESULTS
        batch = self.external_lookup_service.lookup_cached(
            session,
            query=query,
            mode=mode,
            provider_keys=provider_keys,
        )
        cached_items = [
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
        corpus_items = self.word_evidence_service.nayiri_corpus_evidence(
            query=query,
            normalized_query=normalized_query,
        )
        result_items = self.word_evidence_service._merge_external_items(cached_items, corpus_items)
        result_items = self._filter_items_by_mode(
            result_items,
            query=query,
            normalized_query=normalized_query,
            mode=mode,
        )
        result_items.sort(
            key=lambda item: (
                item.provider_display_name or "",
                item.source_title,
                item.word_form,
                item.reference_link or "",
            )
        )
        status = self.word_evidence_service._trusted_external_status(batch.status, result_items)
        if (
            not result_items
            and corpus_items == []
            and self.word_evidence_service.resource_registry.resource_enabled("nayiri_western_corpus", default=True)
            and batch.status is TrustedExternalLookupStatus.UNAVAILABLE
        ):
            status = TrustedExternalLookupStatus.NO_RESULTS
        return result_items, status

    def _fuzzy_forms_for_category(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_query: str,
        category: WordSearchCategory,
    ) -> list[str]:
        if len(normalized_query) <= 2:
            return []
        if category is WordSearchCategory.LEXICON:
            candidates = self._lexicon_normalized_forms(session, user_id=user_id)
        elif category is WordSearchCategory.IMPORTED_BOOKS:
            token_candidates = list(
                session.scalars(
                    select(Occurrence.normalized_token)
                    .join(Document, Occurrence.document_id == Document.id)
                    .where(Document.user_id == user_id)
                    .distinct()
                    .order_by(Occurrence.normalized_token.asc())
                )
            )
            lemma_candidates = list(
                session.scalars(
                    select(MorphologyAnalysis.lemma_normalized)
                    .join(Document, MorphologyAnalysis.document_id == Document.id)
                    .where(
                        Document.user_id == user_id,
                        MorphologyAnalysis.analysis_status == MorphologyAnalysisStatus.COMPLETED,
                        MorphologyAnalysis.lemma_normalized.is_not(None),
                    )
                    .distinct()
                    .order_by(MorphologyAnalysis.lemma_normalized.asc())
                )
            )
            candidates = [value for value in dict.fromkeys(token_candidates + lemma_candidates) if value]
        else:
            entry_candidates = list(
                session.scalars(
                    select(ReferenceEntry.normalized_form)
                    .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
                    .where(ReferenceSource.user_id == str(user_id))
                    .distinct()
                    .order_by(ReferenceEntry.normalized_form.asc())
                )
            )
            lemma_candidates = list(
                session.scalars(
                    select(MorphologyAnalysis.lemma_normalized)
                    .join(ReferenceSource, MorphologyAnalysis.reference_source_id == ReferenceSource.id)
                    .where(
                        ReferenceSource.user_id == str(user_id),
                        MorphologyAnalysis.analysis_status == MorphologyAnalysisStatus.COMPLETED,
                        MorphologyAnalysis.lemma_normalized.is_not(None),
                    )
                    .distinct()
                    .order_by(MorphologyAnalysis.lemma_normalized.asc())
                )
            )
            candidates = [value for value in dict.fromkeys(entry_candidates + lemma_candidates) if value]
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

    @staticmethod
    def _filter_items_by_mode(
        items: list[WordEvidenceItem],
        *,
        query: str,
        normalized_query: str,
        mode: WordSearchMode,
    ) -> list[WordEvidenceItem]:
        if mode is WordSearchMode.FUZZY:
            return items
        if mode is WordSearchMode.EXACT:
            return [
                item
                for item in items
                if item.word_form == query
                or item.matched_form == query
                or (
                    item.matched_form
                    and normalize_token(item.matched_form) == normalized_query
                    and item.normalized_form != normalized_query
                )
            ]
        return [
            item
            for item in items
            if item.normalized_form == normalized_query
            or (item.matched_form and normalize_token(item.matched_form) == normalized_query)
        ]

    def _found_in_imported_books(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_query: str,
    ) -> bool:
        token_hit = session.scalar(
            select(func.count(Occurrence.id))
            .join(Document, Occurrence.document_id == Document.id)
            .where(
                Document.user_id == user_id,
                Occurrence.normalized_token == normalized_query,
            )
        )
        if token_hit:
            return True
        lemma_hit = session.scalar(
            select(func.count(MorphologyAnalysis.id))
            .join(Document, MorphologyAnalysis.document_id == Document.id)
            .where(
                Document.user_id == user_id,
                MorphologyAnalysis.analysis_status == MorphologyAnalysisStatus.COMPLETED,
                MorphologyAnalysis.lemma_normalized == normalized_query,
            )
        )
        return bool(lemma_hit)

    @staticmethod
    def _found_in_reference_sources(
        session: Session,
        *,
        user_key: str,
        normalized_query: str,
    ) -> bool:
        token_hit = session.scalar(
            select(func.count(ReferenceEntry.id))
            .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
            .where(
                ReferenceSource.user_id == user_key,
                ReferenceEntry.normalized_form == normalized_query,
            )
        )
        if token_hit:
            return True
        lemma_hit = session.scalar(
            select(func.count(MorphologyAnalysis.id))
            .join(ReferenceEntry, MorphologyAnalysis.reference_entry_id == ReferenceEntry.id)
            .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
            .where(
                ReferenceSource.user_id == user_key,
                MorphologyAnalysis.analysis_status == MorphologyAnalysisStatus.COMPLETED,
                MorphologyAnalysis.lemma_normalized == normalized_query,
            )
        )
        return bool(lemma_hit)

    def _found_in_trusted_external(
        self,
        *,
        query: str,
        normalized_query: str,
        cached_batch: ExternalLookupBatch,
    ) -> bool:
        if cached_batch.items:
            return True
        return bool(
            self.word_evidence_service.nayiri_corpus_evidence(
                query=query,
                normalized_query=normalized_query,
                limit=1,
            )
        )

    def _trusted_external_check_status(
        self,
        *,
        include_external: bool,
        cached_batch: ExternalLookupBatch,
        query: str,
        normalized_query: str,
    ) -> TrustedExternalLookupStatus | None:
        if self.word_evidence_service.nayiri_corpus_evidence(
            query=query,
            normalized_query=normalized_query,
            limit=1,
        ):
            return TrustedExternalLookupStatus.COMPLETED
        if cached_batch.items:
            return cached_batch.status
        if include_external:
            return cached_batch.status
        if self.word_evidence_service.resource_registry.resource_enabled("nayiri_western_corpus", default=True):
            return TrustedExternalLookupStatus.NO_RESULTS
        return None

    def _trusted_external_match_count(
        self,
        *,
        query: str,
        normalized_query: str,
        cached_batch: ExternalLookupBatch,
    ) -> int:
        return len(cached_batch.items) + len(
            self.word_evidence_service.nayiri_corpus_evidence(
                query=query,
                normalized_query=normalized_query,
            )
        )

    def _trusted_external_check_sources(
        self,
        *,
        query: str,
        normalized_query: str,
        cached_batch: ExternalLookupBatch,
    ) -> list[TrustedExternalWordCheckSource]:
        sources = [
            TrustedExternalWordCheckSource(
                provider_display_name=item.provider_display_name or item.provider_key or "External",
                matched_form=item.matched_form,
                reference_link=item.reference_link,
            )
            for item in cached_batch.items
        ]
        for item in self.word_evidence_service.nayiri_corpus_evidence(
            query=query,
            normalized_query=normalized_query,
            limit=5,
        ):
            sources.append(
                TrustedExternalWordCheckSource(
                    provider_display_name=item.provider_display_name or "Nayiri Corpus",
                    matched_form=item.matched_form or item.word_form,
                    reference_link=item.reference_link,
                )
            )
        return sources


def get_word_search_service() -> WordSearchService:
    return WordSearchService()
