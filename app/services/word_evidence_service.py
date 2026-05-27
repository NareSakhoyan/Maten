from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.resource_registry import ResourceRegistry, get_resource_registry
from app.db.models import (
    Document,
    DocumentPage,
    Lexeme,
    LexemeForm,
    MorphologyAnalysis,
    MorphologyAnalysisStatus,
    NerEntityEntry,
    NerSource,
    Occurrence,
    ReferenceEntry,
    ReferenceSource,
)
from app.db.models import ReferenceMatchType
from app.schemas.reference import ReferenceMatchBest
from app.schemas.word import (
    RelatedLexemeSummary,
    TrustedExternalLookupStatus,
    WordEvidenceItem,
    WordEvidenceExternalSummary,
    WordEvidenceResponse,
    WordEvidenceSourceType,
    WordNamedEntityEvidenceItem,
    WordSearchMode,
    WordEvidenceSummary,
)
from app.services.external_lookup_service import ExternalLookupBatch, ExternalLookupService, get_external_lookup_service
from app.services.lexeme_resolution.lexeme_resolver import (
    LexemeResolution,
    LexemeResolver,
    analyzer_result_from_morphology_row,
    get_lexeme_resolver,
)
from app.services.morphology.morphology_service import MorphologyService, get_morphology_service
from app.services.nayiri_corpus_service import NayiriCorpusService, get_nayiri_corpus_service
from app.services.reference_matching_service import ReferenceMatchingService, get_reference_matching_service
from app.utils.text_normalization import normalize_token
from app.utils.token_classification import classify_token, is_suspicious_script_type, suspicion_reasons_for_script_type


class WordEvidenceService:
    def __init__(
        self,
        *,
        reference_matching_service: ReferenceMatchingService | None = None,
        external_lookup_service: ExternalLookupService | None = None,
        morphology_service: MorphologyService | None = None,
        lexeme_resolver: LexemeResolver | None = None,
        nayiri_corpus_service: NayiriCorpusService | None = None,
        resource_registry: ResourceRegistry | None = None,
    ) -> None:
        self.reference_matching_service = reference_matching_service or get_reference_matching_service()
        self.external_lookup_service = external_lookup_service or get_external_lookup_service()
        self.morphology_service = morphology_service or get_morphology_service()
        self.lexeme_resolver = lexeme_resolver or get_lexeme_resolver()
        self.nayiri_corpus_service = nayiri_corpus_service or get_nayiri_corpus_service()
        self.resource_registry = resource_registry or get_resource_registry()

    def get_word_evidence(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        source_type: WordEvidenceSourceType | None = None,
        source_id: str | None = None,
        include_external: bool = False,
        provider_keys: list[str] | None = None,
        limit: int,
        offset: int,
    ) -> WordEvidenceResponse:
        normalized = normalize_token(normalized_form)
        if not normalized:
            raise ValueError("normalized_form must not be empty.")

        lexeme_map = self.matching_lexeme_map(session, user_id=user_id, normalized_forms=[normalized])
        matching_lexemes = lexeme_map.get(normalized, [])
        related_lexeme_summary = self._related_lexeme_summary(session, lexeme=matching_lexemes[0]) if matching_lexemes else None
        morphology_summary = self.morphology_service.get_word_evidence_summary(
            session,
            user_id=user_id,
            normalized_form=normalized,
        )
        lexeme_resolution = self._lexeme_resolution_for_word(
            session,
            user_id=user_id,
            surface_form=normalized_form,
            normalized_form=normalized,
        )
        dictionary_lemma_normalized = (
            lexeme_resolution.selected_dictionary_lemma_normalized
            if lexeme_resolution.has_structured_dictionary_lemma
            else None
        )

        evidence_items: list[WordEvidenceItem] = []
        if source_type in {None, WordEvidenceSourceType.IMPORTED_BOOK}:
            document_id = UUID(source_id) if source_type is WordEvidenceSourceType.IMPORTED_BOOK and source_id else None
            evidence_items.extend(
                self._merge_document_evidence(
                    self.document_occurrence_evidence(
                        session,
                        user_id=user_id,
                        normalized_form=normalized,
                        document_id=document_id,
                    ),
                    self.document_lemma_evidence(
                        session,
                        user_id=user_id,
                        lemma_normalized=dictionary_lemma_normalized or normalized,
                        document_id=document_id,
                    ),
                )
            )
        if source_type in {None, WordEvidenceSourceType.REFERENCE_SOURCE}:
            reference_source_id = UUID(source_id) if source_type is WordEvidenceSourceType.REFERENCE_SOURCE and source_id else None
            evidence_items.extend(
                self._merge_reference_evidence(
                    self.reference_source_evidence(
                        session,
                        user_id=user_id,
                        normalized_form=normalized,
                        source_id=reference_source_id,
                    ),
                    self.reference_lemma_evidence(
                        session,
                        user_id=user_id,
                        lemma_normalized=dictionary_lemma_normalized or normalized,
                        source_id=reference_source_id,
                    ),
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

        external_batch = ExternalLookupBatch(items=[], status=TrustedExternalLookupStatus.UNAVAILABLE)
        external_evidence_items: list[WordEvidenceItem] = []
        external_requested = include_external or source_type is WordEvidenceSourceType.TRUSTED_EXTERNAL
        best_external_canonical_form: str | None = None
        if external_requested:
            external_batch = self.external_evidence(
                session,
                user_id=user_id,
                normalized_form=normalized,
                provider_keys=[source_id] if source_type is WordEvidenceSourceType.TRUSTED_EXTERNAL and source_id else provider_keys,
            )
            external_evidence_items = external_batch.items
            best_external_canonical_form = self._best_external_canonical_form(external_batch)

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
        named_entity_evidence_items = self.named_entity_evidence(session, normalized_form=normalized)
        return WordEvidenceResponse(
            normalized_form=normalized,
            summary=WordEvidenceSummary(
                total_hits=total_hits,
                source_count=len(distinct_sources),
                linked_lexeme_id=related_lexeme_summary.lexeme_id if related_lexeme_summary is not None else None,
                linked_lexeme_canonical_form=(
                    related_lexeme_summary.canonical_form if related_lexeme_summary is not None else None
                ),
                surface_form=normalized_form,
                normalized_form=normalized,
                morphological_lemma=self._primary_morphological_lemma(lexeme_resolution),
                morphological_source=self._primary_morphological_source(lexeme_resolution),
                morphological_standard=self._morphological_standard(self._primary_morphological_source(lexeme_resolution)),
                dictionary_lemma=lexeme_resolution.selected_dictionary_lemma,
                dictionary_lemma_source=lexeme_resolution.selected_source,
                lexical_mapping_confidence=lexeme_resolution.confidence,
                lexical_mapping_conflict_status=lexeme_resolution.conflict_status,
                best_lemma=best_external_canonical_form or lexeme_resolution.selected_dictionary_lemma or morphology_summary.best_lemma,
                lemma_candidates=morphology_summary.lemma_candidates,
                pos_candidates=morphology_summary.pos_candidates,
                morphology_available=morphology_summary.morphology_available,
            ),
            evidence_items=paged_items,
            external_summary=WordEvidenceExternalSummary(
                total_hits=len(external_evidence_items),
                provider_count=len({item.provider_key for item in external_evidence_items if item.provider_key}),
                status=external_batch.status,
            ) if external_requested else None,
            external_evidence_items=external_evidence_items,
            named_entity_evidence_items=named_entity_evidence_items,
            related_reference_matches=related_reference_matches or None,
            related_lexeme_summary=related_lexeme_summary,
            total=total_hits,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def named_entity_evidence(
        session: Session,
        *,
        normalized_form: str,
        limit: int = 10,
    ) -> list[WordNamedEntityEvidenceItem]:
        rows = session.scalars(
            select(NerEntityEntry)
            .join(NerSource, NerEntityEntry.source_id == NerSource.id)
            .where(
                NerSource.provider_key == "pioner_ner",
                NerSource.is_active.is_(True),
                NerEntityEntry.normalized_surface == normalized_form,
            )
            .order_by(
                NerSource.source_kind.asc(),
                NerEntityEntry.occurrence_count.desc(),
                NerEntityEntry.entity_surface.asc(),
            )
            .limit(limit)
        ).all()
        items: list[WordNamedEntityEvidenceItem] = []
        for row in rows:
            source_kind = row.source.source_kind if row.source else "unknown"
            items.append(
                WordNamedEntityEvidenceItem(
                    id=row.id,
                    provider_display_name=row.source.display_name if row.source else "pioNER",
                    entity_surface=row.entity_surface,
                    normalized_surface=row.normalized_surface,
                    entity_type=row.entity_type,
                    source_kind=source_kind,
                    dataset_split=row.source.dataset_split if row.source else "unknown",
                    occurrence_count=row.occurrence_count,
                    confidence=float(row.confidence) if row.confidence is not None else None,
                    validation_strength="suggests_candidate",
                    sample_contexts=row.sample_contexts,
                )
            )
        return items

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

    def _lexeme_resolution_for_word(
        self,
        session: Session,
        *,
        user_id: UUID,
        surface_form: str,
        normalized_form: str,
    ) -> LexemeResolution:
        rows = session.scalars(
            select(MorphologyAnalysis).where(
                MorphologyAnalysis.user_id == str(user_id),
                MorphologyAnalysis.token_normalized == normalized_form,
                MorphologyAnalysis.analysis_status == MorphologyAnalysisStatus.COMPLETED,
            )
        ).all()
        analyses = [
            analyzer_result_from_morphology_row(
                row,
                source_key=self._morphology_provider_key(row.analyzer_model_key),
                language_profile=self._morphology_language_profile(row.analyzer_model_key),
            )
            for row in rows
        ]
        return self.lexeme_resolver.resolve(
            session,
            user_id=user_id,
            surface_form=surface_form,
            normalized_form=normalized_form,
            morphological_analyses=analyses,
        )

    @staticmethod
    def _primary_morphological_lemma(resolution: LexemeResolution) -> str | None:
        for analysis in resolution.morphological_analyses:
            if analysis.lemma:
                return analysis.lemma
        return None

    @staticmethod
    def _primary_morphological_source(resolution: LexemeResolution) -> str | None:
        for analysis in resolution.morphological_analyses:
            if analysis.lemma or analysis.pos or analysis.features:
                return analysis.source_key
        return None

    @staticmethod
    def _morphology_provider_key(analyzer_model_key: str | None) -> str:
        model_key = (analyzer_model_key or "").strip().lower()
        if "classical" in model_key or model_key in {"xcl", "grabar"}:
            return "pie_classical_morphology"
        if "western" in model_key:
            return "pie_western_morphology"
        return "pie_eastern_morphology"

    @staticmethod
    def _morphology_language_profile(analyzer_model_key: str | None) -> str:
        provider_key = WordEvidenceService._morphology_provider_key(analyzer_model_key)
        if provider_key == "pie_classical_morphology":
            return "classical"
        if provider_key == "pie_western_morphology":
            return "western"
        return "eastern"

    @staticmethod
    def _morphological_standard(source_key: str | None) -> str | None:
        if source_key and source_key.startswith("pie_"):
            return "UD/PIE"
        return source_key

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

    def document_lemma_evidence(
        self,
        session: Session,
        *,
        user_id: UUID,
        lemma_normalized: str,
        document_id: UUID | None = None,
    ) -> list[WordEvidenceItem]:
        filters = [
            MorphologyAnalysis.user_id == str(user_id),
            MorphologyAnalysis.analysis_status == MorphologyAnalysisStatus.COMPLETED,
            MorphologyAnalysis.lemma_normalized == lemma_normalized,
            MorphologyAnalysis.occurrence_id.is_not(None),
            Document.user_id == user_id,
        ]
        if document_id is not None:
            filters.append(Occurrence.document_id == document_id)

        rows = session.execute(
            select(
                Occurrence,
                Document,
                DocumentPage.extraction_method,
                Lexeme.canonical_form,
                MorphologyAnalysis.lemma,
            )
            .join(MorphologyAnalysis, MorphologyAnalysis.occurrence_id == Occurrence.id)
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
            normalized_forms=[lemma_normalized],
        )[lemma_normalized]

        items: list[WordEvidenceItem] = []
        for row in rows:
            lemma_label = row.lemma or lemma_normalized
            item = self._build_occurrence_item(
                occurrence=row.Occurrence,
                document=row.Document,
                extraction_method=row.extraction_method.value if row.extraction_method is not None else None,
                best_reference_match=reference_summary.best_reference_match,
                has_reference_match=reference_summary.has_reference_match,
                lexeme_canonical_form=row.canonical_form,
            )
            items.append(
                item.model_copy(
                    update={
                        "matched_form": lemma_label,
                        "match_type": ReferenceMatchType.NORMALIZED,
                        "source_subtitle": f"Lemma match: {lemma_label}",
                    }
                )
            )
        return items

    def reference_lemma_evidence(
        self,
        session: Session,
        *,
        user_id: UUID,
        lemma_normalized: str,
        source_id: UUID | None = None,
    ) -> list[WordEvidenceItem]:
        filters = [
            ReferenceSource.user_id == str(user_id),
            MorphologyAnalysis.analysis_status == MorphologyAnalysisStatus.COMPLETED,
            MorphologyAnalysis.lemma_normalized == lemma_normalized,
            MorphologyAnalysis.reference_entry_id.is_not(None),
        ]
        if source_id is not None:
            filters.append(ReferenceEntry.source_id == source_id)

        rows = session.execute(
            select(ReferenceEntry, ReferenceSource, MorphologyAnalysis.lemma)
            .join(MorphologyAnalysis, MorphologyAnalysis.reference_entry_id == ReferenceEntry.id)
            .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
            .where(*filters)
            .order_by(ReferenceSource.display_name.asc(), ReferenceEntry.surface_form.asc(), ReferenceEntry.id.asc())
        ).all()
        if not rows:
            return []

        lexeme_map = self.matching_lexeme_map(session, user_id=user_id, normalized_forms=[lemma_normalized])
        matching_lexemes = lexeme_map.get(lemma_normalized, [])
        primary_lexeme = matching_lexemes[0] if matching_lexemes else None

        entry_summary_map = self.reference_matching_service.reference_entry_summary_map(
            session,
            user_id=user_id,
            reference_entry_ids=[row.ReferenceEntry.id for row in rows],
        )
        items: list[WordEvidenceItem] = []
        for row in rows:
            source = row.ReferenceSource
            entry = row.ReferenceEntry
            classification = classify_token(entry.surface_form)
            entry_summary = entry_summary_map.get(entry.id)
            lemma_label = row.lemma or lemma_normalized
            items.append(
                WordEvidenceItem(
                    word_form=entry.surface_form,
                    normalized_form=entry.normalized_form,
                    matched_form=lemma_label,
                    source_type=WordEvidenceSourceType.REFERENCE_SOURCE,
                    source_id=str(source.id),
                    source_title=source.display_name,
                    source_subtitle=f"Lemma match: {lemma_label}",
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
                    match_type=ReferenceMatchType.NORMALIZED,
                    created_at=entry.created_at,
                )
            )
        return items

    def nayiri_corpus_evidence(
        self,
        *,
        query: str,
        normalized_query: str,
        limit: int = 20,
    ) -> list[WordEvidenceItem]:
        if not self.resource_registry.resource_enabled("nayiri_western_corpus", default=True):
            return []
        try:
            matches = self.nayiri_corpus_service.lookup(query, limit=limit)
        except Exception:
            return []

        items: list[WordEvidenceItem] = []
        for match in matches:
            canonical_normalized = normalize_token(match.canonical_form) or match.canonical_form
            items.append(
                WordEvidenceItem(
                    word_form=match.canonical_form,
                    normalized_form=match.normalized_query,
                    matched_form=match.canonical_form,
                    source_type=WordEvidenceSourceType.TRUSTED_EXTERNAL,
                    source_id="nayiri_corpus",
                    source_title="Nayiri Western Armenian Corpus",
                    source_subtitle=(
                        f"{match.token_count} attested tokens across {match.source_count} sources"
                    ),
                    context_snippet=None,
                    reference_link=None,
                    provider_key="nayiri_western_corpus",
                    provider_display_name="Nayiri Corpus",
                    match_type=(
                        ReferenceMatchType.NORMALIZED
                        if canonical_normalized == match.normalized_query
                        else ReferenceMatchType.FUZZY
                    ),
                    match_score=100.0 if canonical_normalized == match.normalized_query else None,
                    source_evidence_role="corpus_attestation",
                    source_evidence_tier="lemma_attestation",
                    source_evidence_verified=True,
                )
            )
        return items

    @staticmethod
    def _merge_document_evidence(
        token_items: list[WordEvidenceItem],
        lemma_items: list[WordEvidenceItem],
    ) -> list[WordEvidenceItem]:
        seen_occurrence_ids = {item.occurrence_id for item in token_items if item.occurrence_id is not None}
        merged = list(token_items)
        for item in lemma_items:
            if item.occurrence_id is not None and item.occurrence_id in seen_occurrence_ids:
                continue
            if item.occurrence_id is not None:
                seen_occurrence_ids.add(item.occurrence_id)
            merged.append(item)
        return merged

    @staticmethod
    def _merge_reference_evidence(
        token_items: list[WordEvidenceItem],
        lemma_items: list[WordEvidenceItem],
    ) -> list[WordEvidenceItem]:
        seen_entry_ids = {item.reference_entry_id for item in token_items if item.reference_entry_id is not None}
        merged = list(token_items)
        for item in lemma_items:
            if item.reference_entry_id is not None and item.reference_entry_id in seen_entry_ids:
                continue
            if item.reference_entry_id is not None:
                seen_entry_ids.add(item.reference_entry_id)
            merged.append(item)
        return merged

    @staticmethod
    def _merge_external_items(
        cached_items: list[WordEvidenceItem],
        corpus_items: list[WordEvidenceItem],
    ) -> list[WordEvidenceItem]:
        merged = list(cached_items)
        seen = {
            (
                item.provider_key or "",
                item.matched_form,
                item.reference_link or "",
                item.source_title or "",
            )
            for item in cached_items
        }
        for item in corpus_items:
            key = (
                item.provider_key or "",
                item.matched_form,
                item.reference_link or "",
                item.source_title or "",
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    @staticmethod
    def _trusted_external_status(
        cached_status: TrustedExternalLookupStatus,
        items: list[WordEvidenceItem],
    ) -> TrustedExternalLookupStatus:
        if items:
            return TrustedExternalLookupStatus.COMPLETED
        if cached_status is TrustedExternalLookupStatus.NO_RESULTS:
            return TrustedExternalLookupStatus.NO_RESULTS
        return TrustedExternalLookupStatus.UNAVAILABLE

    def external_evidence(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        provider_keys: list[str] | None = None,
    ) -> ExternalLookupBatch:
        batch = self.external_lookup_service.lookup_cached(
            session,
            query=normalized_form,
            mode=WordSearchMode.NORMALIZED,
            provider_keys=provider_keys,
        )
        evidence_items = [
            WordEvidenceItem(
                word_form=item.matched_form,
                matched_form=item.matched_form,
                normalized_form=item.normalized_form or normalized_form,
                source_type=WordEvidenceSourceType.TRUSTED_EXTERNAL,
                source_id=item.provider_key,
                source_title=item.source_title or item.provider_display_name,
                source_subtitle=item.source_subtitle,
                page_number=None,
                context_snippet=item.snippet,
                reference_link=item.reference_link,
                provider_key=item.provider_key,
                provider_display_name=item.provider_display_name,
                match_type=item.match_type,
                match_score=item.match_score,
                fetched_at=item.fetched_at,
                created_at=item.created_at or item.fetched_at,
                source_evidence_role=self._source_evidence_role(item.provider_key, item.metadata_json),
                source_evidence_tier=self._source_evidence_tier(item.provider_key, item.metadata_json),
                source_evidence_verified=self._source_evidence_verified(item.provider_key, item.metadata_json),
            )
            for item in batch.items
        ]
        corpus_items = self.nayiri_corpus_evidence(query=normalized_form, normalized_query=normalized_form)
        evidence_items = self._merge_external_items(evidence_items, corpus_items)
        evidence_items.sort(
            key=lambda item: (
                item.provider_display_name or "",
                item.source_title,
                item.word_form,
                item.reference_link or "",
            )
        )
        return ExternalLookupBatch(
            items=evidence_items,
            status=self._trusted_external_status(batch.status, evidence_items),
        )

    @staticmethod
    def _source_evidence_role(provider_key: str, metadata: dict[str, object] | None) -> str | None:
        payload = metadata or {}
        role = payload.get("source_evidence_role")
        if isinstance(role, str) and role.strip():
            return role.strip()
        if provider_key in {"nayiri_web", "nayiri_corpus", "nayiri_western_corpus"}:
            return "nayiri_page_result" if provider_key == "nayiri_web" else "corpus_attestation"
        return None

    @staticmethod
    def _source_evidence_tier(provider_key: str, metadata: dict[str, object] | None) -> str | None:
        payload = metadata or {}
        tier = payload.get("source_evidence_tier")
        if isinstance(tier, str) and tier.strip():
            return tier.strip()
        if provider_key == "nayiri_web":
            return "context_only"
        if provider_key in {"nayiri_corpus", "nayiri_western_corpus"}:
            return "lemma_attestation"
        return None

    @staticmethod
    def _source_evidence_verified(provider_key: str, metadata: dict[str, object] | None) -> bool | None:
        payload = metadata or {}
        verified = payload.get("source_evidence_verified")
        if isinstance(verified, bool):
            return verified
        if provider_key == "nayiri_web":
            return False
        if provider_key in {"nayiri_corpus", "nayiri_western_corpus"}:
            return True
        return None

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
    def _best_external_canonical_form(batch: ExternalLookupBatch) -> str | None:
        if not batch.items:
            return None
        ranked = sorted(
            batch.items,
            key=lambda item: (
                0 if item.match_type is not None and item.match_type.value == "exact" else 1,
                item.match_score is None,
                -(item.match_score or 0),
            ),
        )
        for item in ranked:
            candidate = (item.matched_form or "").strip()
            if candidate:
                return candidate
        return None

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
