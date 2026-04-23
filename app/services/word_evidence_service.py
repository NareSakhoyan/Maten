from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentPage, Lexeme, LexemeForm, Occurrence, ReferenceEntry, ReferenceSource
from app.schemas.reference import ReferenceMatchBest
from app.schemas.word import (
    RelatedLexemeSummary,
    WordEvidenceItem,
    WordEvidenceResponse,
    WordEvidenceSourceType,
    WordEvidenceSummary,
)
from app.services.reference_matching_service import ReferenceMatchingService, get_reference_matching_service
from app.utils.text_normalization import normalize_token
from app.utils.token_classification import classify_token, is_suspicious_script_type, suspicion_reasons_for_script_type


class WordEvidenceService:
    def __init__(self, *, reference_matching_service: ReferenceMatchingService | None = None) -> None:
        self.reference_matching_service = reference_matching_service or get_reference_matching_service()

    def get_word_evidence(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        source_type: WordEvidenceSourceType | None = None,
        source_id: str | None = None,
        limit: int,
        offset: int,
    ) -> WordEvidenceResponse:
        normalized = normalize_token(normalized_form)
        if not normalized:
            raise ValueError("normalized_form must not be empty.")

        lexeme_map = self.matching_lexeme_map(session, user_id=user_id, normalized_forms=[normalized])
        matching_lexemes = lexeme_map.get(normalized, [])
        related_lexeme_summary = self._related_lexeme_summary(session, lexeme=matching_lexemes[0]) if matching_lexemes else None

        evidence_items: list[WordEvidenceItem] = []
        if source_type in {None, WordEvidenceSourceType.IMPORTED_BOOK}:
            evidence_items.extend(
                self.document_occurrence_evidence(
                    session,
                    user_id=user_id,
                    normalized_form=normalized,
                    document_id=UUID(source_id) if source_type is WordEvidenceSourceType.IMPORTED_BOOK and source_id else None,
                )
            )
        if source_type in {None, WordEvidenceSourceType.REFERENCE_SOURCE}:
            evidence_items.extend(
                self.reference_source_evidence(
                    session,
                    user_id=user_id,
                    normalized_form=normalized,
                    source_id=UUID(source_id) if source_type is WordEvidenceSourceType.REFERENCE_SOURCE and source_id else None,
                )
            )
        if source_type in {None, WordEvidenceSourceType.LEXICON}:
            evidence_items.extend(
                self.lexicon_evidence(
                    session,
                    user_id=user_id,
                    normalized_form=normalized,
                    lexeme_id=UUID(source_id) if source_type is WordEvidenceSourceType.LEXICON and source_id else None,
                )
            )

        evidence_items.sort(
            key=lambda item: (
                item.source_type.value,
                item.source_title,
                item.page_number or 0,
                item.word_form,
                str(item.occurrence_id or item.lexeme_id or item.source_id),
            )
        )
        total_hits = len(evidence_items)
        paged_items = evidence_items[offset:offset + limit]
        distinct_sources = {(item.source_type.value, item.source_id) for item in evidence_items}

        related_reference_matches = self._related_reference_matches(
            session,
            user_id=user_id,
            normalized_form=normalized,
            matching_lexemes=matching_lexemes,
        )
        return WordEvidenceResponse(
            normalized_form=normalized,
            summary=WordEvidenceSummary(
                total_hits=total_hits,
                source_count=len(distinct_sources),
                linked_lexeme_id=related_lexeme_summary.lexeme_id if related_lexeme_summary is not None else None,
                linked_lexeme_canonical_form=(
                    related_lexeme_summary.canonical_form if related_lexeme_summary is not None else None
                ),
            ),
            evidence_items=paged_items,
            related_reference_matches=related_reference_matches or None,
            related_lexeme_summary=related_lexeme_summary,
            total=total_hits,
            limit=limit,
            offset=offset,
        )

    def matching_lexeme_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: list[str],
    ) -> dict[str, list[Lexeme]]:
        forms = [form for form in dict.fromkeys(normalized_forms) if form]
        if not forms:
            return {}
        user_key = str(user_id)

        lexemes_by_id: dict[UUID, Lexeme] = {}
        direct_matches = list(
            session.scalars(
                select(Lexeme)
                .where(
                    Lexeme.user_id == user_key,
                    Lexeme.canonical_normalized_form.in_(forms),
                )
                .order_by(Lexeme.created_at.asc(), Lexeme.id.asc())
            )
        )
        for lexeme in direct_matches:
            lexemes_by_id[lexeme.id] = lexeme

        form_rows = session.execute(
            select(LexemeForm.normalized_form, Lexeme)
            .join(Lexeme, Lexeme.id == LexemeForm.lexeme_id)
            .where(
                LexemeForm.user_id == user_key,
                Lexeme.user_id == user_key,
                LexemeForm.normalized_form.in_(forms),
            )
            .order_by(Lexeme.created_at.asc(), Lexeme.id.asc(), LexemeForm.normalized_form.asc())
        ).all()

        grouped: dict[str, list[Lexeme]] = defaultdict(list)
        for lexeme in direct_matches:
            grouped[lexeme.canonical_normalized_form].append(lexeme)
        for normalized_form, lexeme in form_rows:
            if all(existing.id != lexeme.id for existing in grouped[normalized_form]):
                grouped[normalized_form].append(lexeme)
        return grouped

    def document_occurrence_evidence(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        document_id: UUID | None = None,
    ) -> list[WordEvidenceItem]:
        filters = [
            Document.user_id == user_id,
            Occurrence.normalized_token == normalized_form,
        ]
        if document_id is not None:
            filters.append(Occurrence.document_id == document_id)

        rows = session.execute(
            select(Occurrence, Document, DocumentPage.extraction_method, Lexeme.canonical_form)
            .join(Document, Occurrence.document_id == Document.id)
            .join(DocumentPage, Occurrence.page_id == DocumentPage.id)
            .outerjoin(Lexeme, Lexeme.id == Occurrence.lexeme_id)
            .where(*filters)
            .order_by(
                Document.title.asc(),
                Occurrence.page_number.asc(),
                Occurrence.char_start.asc().nullsfirst(),
                Occurrence.created_at.asc(),
            )
        ).all()
        if not rows:
            return []

        reference_summary = self.reference_matching_service.group_summary_map(
            session,
            user_id=user_id,
            normalized_forms=[normalized_form],
        )[normalized_form]

        items: list[WordEvidenceItem] = []
        for row in rows:
            items.append(
                self._build_occurrence_item(
                    occurrence=row.Occurrence,
                    document=row.Document,
                    extraction_method=row.extraction_method.value if row.extraction_method is not None else None,
                    best_reference_match=reference_summary.best_reference_match,
                    has_reference_match=reference_summary.has_reference_match,
                    lexeme_canonical_form=row.canonical_form,
                )
            )
        return items

    def reference_source_evidence(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        source_id: UUID | None = None,
    ) -> list[WordEvidenceItem]:
        filters = [
            ReferenceSource.user_id == str(user_id),
            ReferenceEntry.normalized_form == normalized_form,
        ]
        if source_id is not None:
            filters.append(ReferenceEntry.source_id == source_id)

        rows = session.execute(
            select(ReferenceEntry, ReferenceSource)
            .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
            .where(*filters)
            .order_by(ReferenceSource.display_name.asc(), ReferenceEntry.surface_form.asc(), ReferenceEntry.id.asc())
        ).all()
        if not rows:
            return []

        lexeme_map = self.matching_lexeme_map(session, user_id=user_id, normalized_forms=[normalized_form])
        matching_lexemes = lexeme_map.get(normalized_form, [])
        primary_lexeme = matching_lexemes[0] if matching_lexemes else None

        items: list[WordEvidenceItem] = []
        entry_summary_map = self.reference_matching_service.reference_entry_summary_map(
            session,
            user_id=user_id,
            reference_entry_ids=[row.ReferenceEntry.id for row in rows],
        )
        for row in rows:
            source = row.ReferenceSource
            entry = row.ReferenceEntry
            classification = classify_token(entry.surface_form)
            entry_summary = entry_summary_map.get(entry.id)
            items.append(
                WordEvidenceItem(
                    word_form=entry.surface_form,
                    normalized_form=entry.normalized_form,
                    source_type=WordEvidenceSourceType.REFERENCE_SOURCE,
                    source_id=str(source.id),
                    source_title=source.display_name,
                    source_subtitle=source.description,
                    page_number=None,
                    context_snippet=None,
                    reference_link=f"/reference-sources/{source.id}",
                    reference_entry_id=entry.id,
                    occurrence_id=None,
                    lexeme_id=primary_lexeme.id if primary_lexeme is not None else None,
                    lexeme_canonical_form=primary_lexeme.canonical_form if primary_lexeme is not None else None,
                    has_reference_match=entry_summary.has_reference_match if entry_summary is not None else False,
                    best_reference_match=entry_summary.best_reference_match if entry_summary is not None else None,
                    extraction_method=(
                        source.last_import_method.value
                        if source.last_import_method is not None
                        else "imported_reference"
                    ),
                    source_import_method=source.last_import_method,
                    source_warning=source.last_import_warning,
                    is_suspicious=is_suspicious_script_type(classification.script_type),
                    suspicion_reasons=suspicion_reasons_for_script_type(classification.script_type),
                    created_at=entry.created_at,
                )
            )
        return items

    def lexicon_evidence(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        lexeme_id: UUID | None = None,
    ) -> list[WordEvidenceItem]:
        user_key = str(user_id)
        statement = (
            select(Lexeme)
            .where(
                Lexeme.user_id == user_key,
                or_(
                    Lexeme.canonical_normalized_form == normalized_form,
                    Lexeme.id.in_(
                        select(LexemeForm.lexeme_id).where(
                            LexemeForm.user_id == user_key,
                            LexemeForm.normalized_form == normalized_form,
                        )
                    ),
                ),
            )
            .order_by(Lexeme.created_at.asc(), Lexeme.id.asc())
        )
        if lexeme_id is not None:
            statement = statement.where(Lexeme.id == lexeme_id)

        lexemes = list(session.scalars(statement))
        if not lexemes:
            return []

        reference_summary_map = self.reference_matching_service.lexeme_summary_map(
            session,
            user_id=user_id,
            lexeme_ids=[lexeme.id for lexeme in lexemes],
        )
        sample_rows = self._sample_occurrences_for_lexemes(session, lexeme_ids=[lexeme.id for lexeme in lexemes])
        form_map = self._normalized_forms_for_lexemes(session, lexeme_ids=[lexeme.id for lexeme in lexemes], user_id=user_id)

        items: list[WordEvidenceItem] = []
        for lexeme in lexemes:
            sample = sample_rows.get(lexeme.id)
            reference_summary = reference_summary_map[str(lexeme.id)]
            classification = classify_token(lexeme.canonical_form)
            items.append(
                WordEvidenceItem(
                    word_form=lexeme.canonical_form,
                    normalized_form=normalized_form,
                    source_type=WordEvidenceSourceType.LEXICON,
                    source_id=str(lexeme.id),
                    source_title=lexeme.canonical_form,
                    source_subtitle=", ".join(form_map.get(lexeme.id, [])) or lexeme.canonical_normalized_form,
                    page_number=sample["page_number"] if sample is not None else None,
                    context_snippet=sample["context_snippet"] if sample is not None else None,
                    reference_link=f"/lexemes/{lexeme.id}",
                    occurrence_id=sample["occurrence_id"] if sample is not None else None,
                    lexeme_id=lexeme.id,
                    lexeme_canonical_form=lexeme.canonical_form,
                    has_reference_match=reference_summary.has_reference_match,
                    best_reference_match=reference_summary.best_reference_match,
                    extraction_method=sample["extraction_method"] if sample is not None else None,
                    is_suspicious=is_suspicious_script_type(classification.script_type),
                    suspicion_reasons=suspicion_reasons_for_script_type(classification.script_type),
                    created_at=lexeme.created_at,
                )
            )
        return items

    def _related_reference_matches(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        matching_lexemes: list[Lexeme],
    ) -> list[ReferenceMatchBest]:
        related: list[ReferenceMatchBest] = []
        group_summary = self.reference_matching_service.group_summary_map(
            session,
            user_id=user_id,
            normalized_forms=[normalized_form],
        )[normalized_form]
        if group_summary.best_reference_match is not None:
            related.append(group_summary.best_reference_match)

        if matching_lexemes:
            lexeme_summary_map = self.reference_matching_service.lexeme_summary_map(
                session,
                user_id=user_id,
                lexeme_ids=[lexeme.id for lexeme in matching_lexemes],
            )
            for lexeme in matching_lexemes:
                best = lexeme_summary_map[str(lexeme.id)].best_reference_match
                if best is not None:
                    related.append(best)

        unique: dict[tuple[str, str, str, float | None], ReferenceMatchBest] = {}
        for item in related:
            unique[(item.source_display_name, item.matched_form, item.match_type.value, item.match_score)] = item
        return list(unique.values())

    @staticmethod
    def _related_lexeme_summary(session: Session, *, lexeme: Lexeme) -> RelatedLexemeSummary:
        occurrence_count = session.scalar(
            select(func.count(Occurrence.id)).where(Occurrence.lexeme_id == lexeme.id)
        ) or 0
        return RelatedLexemeSummary(
            lexeme_id=lexeme.id,
            canonical_form=lexeme.canonical_form,
            canonical_normalized_form=lexeme.canonical_normalized_form,
            occurrence_count=occurrence_count,
        )

    @staticmethod
    def _build_occurrence_item(
        *,
        occurrence: Occurrence,
        document: Document,
        extraction_method: str | None,
        best_reference_match: ReferenceMatchBest | None,
        has_reference_match: bool,
        lexeme_canonical_form: str | None,
    ) -> WordEvidenceItem:
        return WordEvidenceItem(
            word_form=occurrence.token,
            normalized_form=occurrence.normalized_token,
            source_type=WordEvidenceSourceType.IMPORTED_BOOK,
            source_id=str(document.id),
            source_title=document.title,
            source_subtitle=document.original_filename,
            page_number=occurrence.page_number,
            context_snippet=occurrence.context_snippet,
            reference_link=f"/documents/{document.id}?page={occurrence.page_number}",
            reference_entry_id=None,
            occurrence_id=occurrence.id,
            lexeme_id=occurrence.lexeme_id,
            lexeme_canonical_form=lexeme_canonical_form,
            has_reference_match=has_reference_match,
            best_reference_match=best_reference_match,
            extraction_method=extraction_method,
            source_import_method=None,
            source_warning=None,
            is_suspicious=is_suspicious_script_type(occurrence.script_type),
            suspicion_reasons=suspicion_reasons_for_script_type(occurrence.script_type),
            created_at=occurrence.created_at,
        )

    @staticmethod
    def _sample_occurrences_for_lexemes(session: Session, *, lexeme_ids: list[UUID]) -> dict[UUID, dict[str, object]]:
        if not lexeme_ids:
            return {}
        rows = session.execute(
            select(Occurrence, DocumentPage.extraction_method)
            .join(DocumentPage, Occurrence.page_id == DocumentPage.id)
            .where(Occurrence.lexeme_id.in_(lexeme_ids))
            .order_by(Occurrence.lexeme_id.asc(), Occurrence.page_number.asc(), Occurrence.created_at.asc())
        ).all()
        samples: dict[UUID, dict[str, object]] = {}
        for row in rows:
            if row.Occurrence.lexeme_id in samples:
                continue
            samples[row.Occurrence.lexeme_id] = {
                "occurrence_id": row.Occurrence.id,
                "page_number": row.Occurrence.page_number,
                "context_snippet": row.Occurrence.context_snippet,
                "extraction_method": row.extraction_method.value if row.extraction_method is not None else None,
            }
        return samples

    @staticmethod
    def _normalized_forms_for_lexemes(
        session: Session,
        *,
        lexeme_ids: list[UUID],
        user_id: UUID,
    ) -> dict[UUID, list[str]]:
        if not lexeme_ids:
            return {}
        rows = session.execute(
            select(LexemeForm.lexeme_id, LexemeForm.normalized_form)
            .where(
                LexemeForm.user_id == str(user_id),
                LexemeForm.lexeme_id.in_(lexeme_ids),
            )
            .order_by(LexemeForm.lexeme_id.asc(), LexemeForm.normalized_form.asc())
        ).all()
        result: dict[UUID, list[str]] = defaultdict(list)
        for lexeme_id, normalized_form in rows:
            result[lexeme_id].append(normalized_form)
        return result


def get_word_evidence_service() -> WordEvidenceService:
    return WordEvidenceService()
