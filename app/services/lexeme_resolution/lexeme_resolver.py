from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Lexeme, LexemeForm, LexemeFormMapping, ReferenceEntry, ReferenceSource
from app.utils.text_normalization import normalize_token


APPROVED_MAPPING_REVIEW_STATUS = "approved"
MORPHOLOGY_FALLBACK_SOURCE = "pie_dalih_morphology"


@dataclass(frozen=True, slots=True)
class MorphologyEvidence:
    surface_form: str
    normalized_surface_form: str
    lemma: str | None
    normalized_lemma: str | None
    pos: str | None
    features: dict[str, object] | None
    language_profile: str | None
    source_key: str
    confidence: float | None
    raw_payload: dict[str, object] = field(default_factory=dict)


AnalyzerResult = MorphologyEvidence


@dataclass(frozen=True, slots=True)
class DictionaryLemmaCandidate:
    lemma: str
    normalized_lemma: str
    source: str
    confidence: float
    resolution_type: str
    raw_payload: dict[str, object] = field(default_factory=dict)

    @property
    def source_key(self) -> str:
        return self.source

    @property
    def evidence_type(self) -> str:
        return self.resolution_type

    @property
    def resolution_status(self) -> str:
        return self.resolution_type


@dataclass(frozen=True, slots=True)
class OcrCorrectionCandidate:
    surface_form: str
    normalized_form: str
    candidate: str
    normalized_candidate: str
    source_key: str
    confidence: float | None = None
    raw_payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LexemeResolution:
    surface_form: str
    normalized_form: str
    morphological_lemma: str | None = None
    dictionary_lemma: str | None = None
    dictionary_lemma_source: str | None = None
    confidence: float | None = None
    resolution_type: str = "unresolved"
    notes: list[str] = field(default_factory=list)
    morphological_analyses: list[MorphologyEvidence] = field(default_factory=list)
    dictionary_lemma_candidates: list[DictionaryLemmaCandidate] = field(default_factory=list)
    ocr_correction_candidates: list[OcrCorrectionCandidate] = field(default_factory=list)

    @property
    def selected_dictionary_lemma(self) -> str | None:
        return self.dictionary_lemma

    @property
    def selected_dictionary_lemma_normalized(self) -> str | None:
        if self.dictionary_lemma is None:
            return None
        return normalize_token(self.dictionary_lemma)

    @property
    def selected_source(self) -> str | None:
        return self.dictionary_lemma_source

    @property
    def conflict_status(self) -> str:
        return "none"

    @property
    def resolution_status(self) -> str:
        return self.resolution_type

    @property
    def has_structured_dictionary_lemma(self) -> bool:
        return self.resolution_type in {
            "resolved_by_approved_lexeme_mapping",
            "resolved_by_curated_lexeme_form",
            "resolved_by_imported_reference_mapping",
        }


class LexemeResolver:
    def resolve(
        self,
        session: Session,
        *,
        user_id: UUID,
        surface_form: str,
        normalized_form: str | None = None,
        morphology_result: MorphologyEvidence | None = None,
        morphological_analyses: list[MorphologyEvidence] | None = None,
        language_profile: str = "unknown",
        ocr_correction_candidates: list[OcrCorrectionCandidate] | None = None,
    ) -> LexemeResolution:
        normalized = normalize_token(normalized_form or surface_form)
        analyses = list(morphological_analyses or ([] if morphology_result is None else [morphology_result]))
        morphological_lemma = self._primary_morphological_lemma(analyses)
        notes: list[str] = []

        selected = None
        if normalized:
            selected = (
                self._approved_mapping_candidate(
                    session,
                    user_id=user_id,
                    normalized_form=normalized,
                    language_profile=language_profile,
                )
                or self._curated_lexeme_form_candidate(session, user_id=user_id, normalized_form=normalized)
                or self._imported_reference_candidate(session, user_id=user_id, normalized_form=normalized)
            )

        resolution_type = "unresolved"
        dictionary_lemma = None
        dictionary_lemma_source = None
        confidence = None
        candidates: list[DictionaryLemmaCandidate] = []
        if selected is not None:
            dictionary_lemma = selected.lemma
            dictionary_lemma_source = selected.source
            confidence = selected.confidence
            resolution_type = selected.resolution_type
            candidates.append(selected)
            if resolution_type == "resolved_by_approved_lexeme_mapping":
                notes.append("Resolved by approved lexeme mapping.")
        elif morphological_lemma:
            confidence = self._primary_morphological_confidence(analyses)
            resolution_type = "morphology_fallback_only"
            notes.append("PIE/DALiH lemma retained as morphology fallback only; it is not dictionary canonicalization.")

        return LexemeResolution(
            surface_form=surface_form,
            normalized_form=normalized,
            morphological_lemma=morphological_lemma,
            dictionary_lemma=dictionary_lemma,
            dictionary_lemma_source=dictionary_lemma_source,
            confidence=confidence,
            resolution_type=resolution_type,
            notes=notes,
            morphological_analyses=analyses,
            dictionary_lemma_candidates=candidates,
            ocr_correction_candidates=list(ocr_correction_candidates or []),
        )

    def resolve_many(
        self,
        session: Session,
        *,
        user_id: UUID,
        forms: list[str],
        morphological_analyses_by_form: dict[str, list[MorphologyEvidence]] | None = None,
        language_profile: str = "unknown",
    ) -> dict[str, LexemeResolution]:
        analyses_by_form = morphological_analyses_by_form or {}
        normalized_forms = list(dict.fromkeys(normalize_token(form) for form in forms if normalize_token(form)))
        approved_mapping_candidates = self._approved_mapping_candidates(
            session,
            user_id=user_id,
            normalized_forms=normalized_forms,
            language_profile=language_profile,
        )
        curated_lexeme_candidates = self._curated_lexeme_form_candidates(
            session,
            user_id=user_id,
            normalized_forms=normalized_forms,
        )
        imported_reference_candidates = self._imported_reference_candidates(
            session,
            user_id=user_id,
            normalized_forms=normalized_forms,
        )

        return {
            form: self._build_resolution(
                surface_form=form,
                normalized_form=normalize_token(form),
                analyses=analyses_by_form.get(form, []),
                selected=(
                    approved_mapping_candidates.get(normalize_token(form))
                    or curated_lexeme_candidates.get(normalize_token(form))
                    or imported_reference_candidates.get(normalize_token(form))
                ),
            )
            for form in forms
        }

    @staticmethod
    def _build_resolution(
        *,
        surface_form: str,
        normalized_form: str,
        analyses: list[MorphologyEvidence],
        selected: DictionaryLemmaCandidate | None,
        ocr_correction_candidates: list[OcrCorrectionCandidate] | None = None,
    ) -> LexemeResolution:
        morphological_lemma = LexemeResolver._primary_morphological_lemma(analyses)
        notes: list[str] = []

        resolution_type = "unresolved"
        dictionary_lemma = None
        dictionary_lemma_source = None
        confidence = None
        candidates: list[DictionaryLemmaCandidate] = []
        if selected is not None:
            dictionary_lemma = selected.lemma
            dictionary_lemma_source = selected.source
            confidence = selected.confidence
            resolution_type = selected.resolution_type
            candidates.append(selected)
            if resolution_type == "resolved_by_approved_lexeme_mapping":
                notes.append("Resolved by approved lexeme mapping.")
        elif morphological_lemma:
            confidence = LexemeResolver._primary_morphological_confidence(analyses)
            resolution_type = "morphology_fallback_only"
            notes.append("PIE/DALiH lemma retained as morphology fallback only; it is not dictionary canonicalization.")

        return LexemeResolution(
            surface_form=surface_form,
            normalized_form=normalized_form,
            morphological_lemma=morphological_lemma,
            dictionary_lemma=dictionary_lemma,
            dictionary_lemma_source=dictionary_lemma_source,
            confidence=confidence,
            resolution_type=resolution_type,
            notes=notes,
            morphological_analyses=analyses,
            dictionary_lemma_candidates=candidates,
            ocr_correction_candidates=list(ocr_correction_candidates or []),
        )

    @staticmethod
    def _approved_mapping_candidate(
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
        language_profile: str,
    ) -> DictionaryLemmaCandidate | None:
        profile_order = [language_profile, "mixed", "unknown"] if language_profile != "unknown" else ["unknown", "mixed"]
        rows = session.scalars(
            select(LexemeFormMapping).where(
                LexemeFormMapping.normalized_surface_form == normalized_form,
                LexemeFormMapping.review_status == APPROVED_MAPPING_REVIEW_STATUS,
                or_(LexemeFormMapping.user_id.is_(None), LexemeFormMapping.user_id == str(user_id)),
            )
        ).all()
        rows = [
            row
            for row in rows
            if row.mapping_type != "ocr_correction_candidate"
            and row.source_key not in {"fuzzy", "fuzzy_ocr", "ocr_correction_candidate"}
        ]
        ordered = sorted(
            rows,
            key=lambda row: (
                0 if row.user_id == str(user_id) else 1,
                profile_order.index(row.language_profile) if row.language_profile in profile_order else len(profile_order),
                -(float(row.confidence) if row.confidence is not None else 0.0),
                row.normalized_dictionary_lemma,
            ),
        )
        if not ordered:
            return None
        row = ordered[0]
        confidence = float(row.confidence) if row.confidence is not None else 0.9
        return DictionaryLemmaCandidate(
            lemma=row.dictionary_lemma,
            normalized_lemma=row.normalized_dictionary_lemma,
            source=row.source_key or row.source_type,
            confidence=confidence,
            resolution_type="resolved_by_approved_lexeme_mapping",
            raw_payload={
                "mapping_id": str(row.id),
                "mapping_type": row.mapping_type,
                "source_type": row.source_type,
                "language_profile": row.language_profile,
                "review_status": row.review_status,
            },
        )

    @staticmethod
    def _approved_mapping_candidates(
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: list[str],
        language_profile: str,
    ) -> dict[str, DictionaryLemmaCandidate]:
        if not normalized_forms:
            return {}

        profile_order = [language_profile, "mixed", "unknown"] if language_profile != "unknown" else ["unknown", "mixed"]
        rows = session.scalars(
            select(LexemeFormMapping).where(
                LexemeFormMapping.normalized_surface_form.in_(normalized_forms),
                LexemeFormMapping.review_status == APPROVED_MAPPING_REVIEW_STATUS,
                or_(LexemeFormMapping.user_id.is_(None), LexemeFormMapping.user_id == str(user_id)),
            )
        ).all()
        rows = [
            row
            for row in rows
            if row.mapping_type != "ocr_correction_candidate"
            and row.source_key not in {"fuzzy", "fuzzy_ocr", "ocr_correction_candidate"}
        ]
        ordered = sorted(
            rows,
            key=lambda row: (
                row.normalized_surface_form,
                0 if row.user_id == str(user_id) else 1,
                profile_order.index(row.language_profile) if row.language_profile in profile_order else len(profile_order),
                -(float(row.confidence) if row.confidence is not None else 0.0),
                row.normalized_dictionary_lemma,
            ),
        )

        candidates: dict[str, DictionaryLemmaCandidate] = {}
        for row in ordered:
            if row.normalized_surface_form in candidates:
                continue
            confidence = float(row.confidence) if row.confidence is not None else 0.9
            candidates[row.normalized_surface_form] = DictionaryLemmaCandidate(
                lemma=row.dictionary_lemma,
                normalized_lemma=row.normalized_dictionary_lemma,
                source=row.source_key or row.source_type,
                confidence=confidence,
                resolution_type="resolved_by_approved_lexeme_mapping",
                raw_payload={
                    "mapping_id": str(row.id),
                    "mapping_type": row.mapping_type,
                    "source_type": row.source_type,
                    "language_profile": row.language_profile,
                    "review_status": row.review_status,
                },
            )
        return candidates

    @staticmethod
    def _curated_lexeme_form_candidate(
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
    ) -> DictionaryLemmaCandidate | None:
        row = session.execute(
            select(Lexeme.canonical_form, Lexeme.canonical_normalized_form, LexemeForm.id)
            .join(LexemeForm, LexemeForm.lexeme_id == Lexeme.id)
            .where(
                Lexeme.user_id == str(user_id),
                LexemeForm.user_id == str(user_id),
                LexemeForm.normalized_form == normalized_form,
            )
            .order_by(Lexeme.created_at.asc(), Lexeme.id.asc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return DictionaryLemmaCandidate(
            lemma=row.canonical_form,
            normalized_lemma=row.canonical_normalized_form,
            source="internal_curated_lexeme_forms",
            confidence=1.0,
            resolution_type="resolved_by_curated_lexeme_form",
            raw_payload={"lexeme_form_id": str(row.id)},
        )

    @staticmethod
    def _curated_lexeme_form_candidates(
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: list[str],
    ) -> dict[str, DictionaryLemmaCandidate]:
        if not normalized_forms:
            return {}

        rows = session.execute(
            select(LexemeForm.normalized_form, Lexeme.canonical_form, Lexeme.canonical_normalized_form, LexemeForm.id)
            .join(Lexeme, LexemeForm.lexeme_id == Lexeme.id)
            .where(
                Lexeme.user_id == str(user_id),
                LexemeForm.user_id == str(user_id),
                LexemeForm.normalized_form.in_(normalized_forms),
            )
            .order_by(LexemeForm.normalized_form.asc(), Lexeme.created_at.asc(), Lexeme.id.asc())
        ).all()
        candidates: dict[str, DictionaryLemmaCandidate] = {}
        for row in rows:
            if row.normalized_form in candidates:
                continue
            candidates[row.normalized_form] = DictionaryLemmaCandidate(
                lemma=row.canonical_form,
                normalized_lemma=row.canonical_normalized_form,
                source="internal_curated_lexeme_forms",
                confidence=1.0,
                resolution_type="resolved_by_curated_lexeme_form",
                raw_payload={"lexeme_form_id": str(row.id)},
            )
        return candidates

    @staticmethod
    def _imported_reference_candidate(
        session: Session,
        *,
        user_id: UUID,
        normalized_form: str,
    ) -> DictionaryLemmaCandidate | None:
        rows = session.scalars(
            select(ReferenceEntry)
            .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
            .where(
                ReferenceSource.user_id == str(user_id),
                ReferenceSource.is_active.is_(True),
                ReferenceEntry.normalized_form == normalized_form,
            )
            .order_by(ReferenceSource.display_name.asc(), ReferenceEntry.surface_form.asc())
        ).all()
        for entry in rows:
            lemma = LexemeResolver._reference_dictionary_lemma(entry)
            normalized_lemma = normalize_token(lemma) if lemma else ""
            if not lemma or not normalized_lemma:
                continue
            return DictionaryLemmaCandidate(
                lemma=lemma,
                normalized_lemma=normalized_lemma,
                source="imported_reference",
                confidence=0.85,
                resolution_type="resolved_by_imported_reference_mapping",
                raw_payload={"reference_entry_id": str(entry.id), "source_id": str(entry.source_id)},
            )
        return None

    @staticmethod
    def _imported_reference_candidates(
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: list[str],
    ) -> dict[str, DictionaryLemmaCandidate]:
        if not normalized_forms:
            return {}

        rows = session.scalars(
            select(ReferenceEntry)
            .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
            .where(
                ReferenceSource.user_id == str(user_id),
                ReferenceSource.is_active.is_(True),
                ReferenceEntry.normalized_form.in_(normalized_forms),
            )
            .order_by(ReferenceEntry.normalized_form.asc(), ReferenceSource.display_name.asc(), ReferenceEntry.surface_form.asc())
        ).all()
        candidates: dict[str, DictionaryLemmaCandidate] = {}
        for entry in rows:
            if entry.normalized_form in candidates:
                continue
            lemma = LexemeResolver._reference_dictionary_lemma(entry)
            normalized_lemma = normalize_token(lemma) if lemma else ""
            if not lemma or not normalized_lemma:
                continue
            candidates[entry.normalized_form] = DictionaryLemmaCandidate(
                lemma=lemma,
                normalized_lemma=normalized_lemma,
                source="imported_reference",
                confidence=0.85,
                resolution_type="resolved_by_imported_reference_mapping",
                raw_payload={"reference_entry_id": str(entry.id), "source_id": str(entry.source_id)},
            )
        return candidates

    @staticmethod
    def _reference_dictionary_lemma(entry: ReferenceEntry) -> str | None:
        metadata = entry.metadata_json or {}
        for key in ("dictionary_lemma", "headword", "lemma", "canonical_form", "canonical_headword", "entry_headword"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _primary_morphological_lemma(analyses: list[MorphologyEvidence]) -> str | None:
        for analysis in analyses:
            if analysis.lemma:
                return analysis.lemma
        return None

    @staticmethod
    def _primary_morphological_confidence(analyses: list[MorphologyEvidence]) -> float | None:
        for analysis in analyses:
            if analysis.lemma:
                return analysis.confidence
        return None


def analyzer_result_from_morphology_row(
    row: Any,
    *,
    source_key: str,
    language_profile: str | None = None,
) -> MorphologyEvidence:
    return MorphologyEvidence(
        surface_form=row.token_surface,
        normalized_surface_form=row.token_normalized,
        lemma=row.lemma,
        normalized_lemma=row.lemma_normalized,
        pos=row.pos,
        features=row.morph_features,
        language_profile=language_profile,
        source_key=source_key,
        confidence=0.35,
        raw_payload={
            "morphology_analysis_id": str(row.id),
            "analyzer_provider": row.analyzer_provider,
            "analyzer_model_key": row.analyzer_model_key,
            "analyzer_version": row.analyzer_version,
        },
    )


def get_lexeme_resolver() -> LexemeResolver:
    return LexemeResolver()
