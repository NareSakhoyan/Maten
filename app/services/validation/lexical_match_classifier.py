from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
import unicodedata

from app.utils.text_normalization import normalize_token


class LexicalMatchType(StrEnum):
    EXACT_HEADWORD_MATCH = "exact_headword_match"
    EXACT_LEMMA_MATCH = "exact_lemma_match"
    APPROVED_VARIANT_MATCH = "approved_variant_match"
    CORPUS_TOKEN_ATTESTATION = "corpus_token_attestation"
    CORPUS_LEMMA_ATTESTATION = "corpus_lemma_attestation"
    MORPHOLOGY_ANALYSIS_ONLY = "morphology_analysis_only"
    NAMED_ENTITY_SIGNAL = "named_entity_signal"
    FUZZY_OCR_CANDIDATE = "fuzzy_ocr_candidate"
    PAGE_FULLTEXT_OCCURRENCE = "page_fulltext_occurrence"
    SUBSTRING_MATCH = "substring_match"
    PARTIAL_MATCH = "partial_match"
    AMBIGUOUS_SEARCH_RESULT = "ambiguous_search_result"
    REJECTED_ARTIFACT = "rejected_artifact"


class ValidationStrength(StrEnum):
    VALIDATES_WORD = "validates_word"
    SUPPORTS_WORD = "supports_word"
    SUGGESTS_CANDIDATE = "suggests_candidate"
    CONTEXT_ONLY = "context_only"
    DOES_NOT_VALIDATE = "does_not_validate"
    REJECTS = "rejects"


class EvidenceRole(StrEnum):
    CURATED_LEXICON = "curated_lexicon"
    IMPORTED_REFERENCE = "imported_reference"
    CORPUS_ATTESTATION = "corpus_attestation"
    WEB_DICTIONARY = "web_dictionary"
    MORPHOLOGY_ANALYSIS = "morphology_analysis"
    NAMED_ENTITY_SIGNAL = "named_entity_signal"
    FUZZY_OCR = "fuzzy_ocr_suggestion"
    AMBIGUOUS_EXTERNAL = "ambiguous_external"


REFERENCE_ROLES = {
    EvidenceRole.CURATED_LEXICON.value,
    EvidenceRole.IMPORTED_REFERENCE.value,
    EvidenceRole.WEB_DICTIONARY.value,
}


@dataclass(frozen=True, slots=True)
class LexicalMatchClassification:
    match_type: LexicalMatchType
    validation_strength: ValidationStrength
    evidence_role: EvidenceRole
    evidence_strength: str
    definition_quality: str
    confidence_score: float
    is_exact_match: bool = False
    is_substring_match: bool = False
    is_fuzzy_match: bool = False
    is_canonical_match: bool = False
    reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class LexicalMatchClassifier:
    def classify(
        self,
        *,
        query_form: str,
        provider_role: str,
        matched_form: str | None = None,
        result_headword: str | None = None,
        lemma: str | None = None,
        snippet: str | None = None,
        definition_quality: str = "unknown",
        source_match_type: str | None = None,
        source_confidence: float | None = None,
        corpus_token_count: int | None = None,
        corpus_source_count: int | None = None,
        has_digits: bool = False,
        allow_short_token: bool = False,
    ) -> LexicalMatchClassification:
        role = self._normalize_role(provider_role)
        query = normalize_token(query_form)
        headword = normalize_token(result_headword or matched_form or "")
        matched = normalize_token(matched_form or "")
        normalized_lemma = normalize_token(lemma or "")
        source_match = (source_match_type or "").strip().lower()

        if not query:
            return self._rejected(role, reason="empty token")

        if self._is_probable_ocr_noise(query, has_digits=has_digits) and not allow_short_token:
            return self._rejected(role, reason="probable OCR noise")

        if self._is_single_armenian_letter(query) and not allow_short_token:
            return self._rejected(role, reason="single Armenian character requires exact curated/reference evidence")

        if source_match == "fuzzy" or role is EvidenceRole.FUZZY_OCR:
            return LexicalMatchClassification(
                match_type=LexicalMatchType.FUZZY_OCR_CANDIDATE,
                validation_strength=ValidationStrength.SUGGESTS_CANDIDATE,
                evidence_role=EvidenceRole.FUZZY_OCR,
                evidence_strength="weak",
                definition_quality=definition_quality,
                confidence_score=source_confidence if source_confidence is not None else 0.25,
                is_fuzzy_match=True,
                reason="fuzzy match is only an OCR correction suggestion",
            )

        if role in REFERENCE_ROLES:
            if headword and query == headword:
                return self._exact_reference(role, definition_quality=definition_quality)
            if normalized_lemma and query == normalized_lemma:
                return LexicalMatchClassification(
                    match_type=LexicalMatchType.EXACT_LEMMA_MATCH,
                    validation_strength=ValidationStrength.SUPPORTS_WORD,
                    evidence_role=role,
                    evidence_strength="medium",
                    definition_quality=definition_quality,
                    confidence_score=0.8,
                    is_exact_match=True,
                    is_canonical_match=True,
                )
            if headword and (query in headword or headword in query):
                return self._substring(role, definition_quality=definition_quality, source_confidence=source_confidence)
            if matched and matched != headword and query in matched:
                return self._substring(role, definition_quality=definition_quality, source_confidence=source_confidence)
            if snippet and query and query in normalize_token(snippet):
                return LexicalMatchClassification(
                    match_type=LexicalMatchType.PAGE_FULLTEXT_OCCURRENCE,
                    validation_strength=ValidationStrength.CONTEXT_ONLY,
                    evidence_role=EvidenceRole.AMBIGUOUS_EXTERNAL,
                    evidence_strength="none",
                    definition_quality=definition_quality,
                    confidence_score=0.05,
                    is_substring_match=True,
                    reason="query appears only in snippet/body search text",
                )
            return LexicalMatchClassification(
                match_type=LexicalMatchType.AMBIGUOUS_SEARCH_RESULT,
                validation_strength=ValidationStrength.DOES_NOT_VALIDATE,
                evidence_role=EvidenceRole.AMBIGUOUS_EXTERNAL,
                evidence_strength="none",
                definition_quality=definition_quality,
                confidence_score=0.05,
                reason="no exact headword or canonical form was available",
            )

        if role is EvidenceRole.CORPUS_ATTESTATION:
            if matched and query == matched:
                strong = (corpus_token_count or 0) >= 3 or (corpus_source_count or 0) > 1
                return LexicalMatchClassification(
                    match_type=LexicalMatchType.CORPUS_TOKEN_ATTESTATION,
                    validation_strength=ValidationStrength.SUPPORTS_WORD,
                    evidence_role=role,
                    evidence_strength="strong" if strong else "weak",
                    definition_quality="unknown",
                    confidence_score=0.65 if strong else 0.45,
                    is_exact_match=True,
                )
            if normalized_lemma and query == normalized_lemma:
                return LexicalMatchClassification(
                    match_type=LexicalMatchType.CORPUS_LEMMA_ATTESTATION,
                    validation_strength=ValidationStrength.SUPPORTS_WORD,
                    evidence_role=role,
                    evidence_strength="medium",
                    definition_quality="unknown",
                    confidence_score=0.6,
                    is_canonical_match=True,
                )
            if matched and query in matched:
                return self._substring(role, definition_quality="unknown", source_confidence=0.05)
            return LexicalMatchClassification(
                match_type=LexicalMatchType.AMBIGUOUS_SEARCH_RESULT,
                validation_strength=ValidationStrength.DOES_NOT_VALIDATE,
                evidence_role=role,
                evidence_strength="none",
                definition_quality="unknown",
                confidence_score=0.05,
            )

        if role is EvidenceRole.MORPHOLOGY_ANALYSIS:
            return LexicalMatchClassification(
                match_type=LexicalMatchType.MORPHOLOGY_ANALYSIS_ONLY,
                validation_strength=ValidationStrength.SUGGESTS_CANDIDATE,
                evidence_role=role,
                evidence_strength="weak",
                definition_quality="unknown",
                confidence_score=0.35,
                is_canonical_match=bool(normalized_lemma),
                reason="morphology is plausibility evidence, not lexical validation",
            )

        if role is EvidenceRole.NAMED_ENTITY_SIGNAL:
            return LexicalMatchClassification(
                match_type=LexicalMatchType.NAMED_ENTITY_SIGNAL,
                validation_strength=ValidationStrength.SUGGESTS_CANDIDATE,
                evidence_role=role,
                evidence_strength="medium",
                definition_quality="unknown",
                confidence_score=0.55,
                is_exact_match=True,
                reason="named-entity evidence supports a proper-noun reading but does not validate lexical existence",
            )

        return LexicalMatchClassification(
            match_type=LexicalMatchType.AMBIGUOUS_SEARCH_RESULT,
            validation_strength=ValidationStrength.DOES_NOT_VALIDATE,
            evidence_role=EvidenceRole.AMBIGUOUS_EXTERNAL,
            evidence_strength="none",
            definition_quality=definition_quality,
            confidence_score=0.05,
        )

    @staticmethod
    def _normalize_role(provider_role: str) -> EvidenceRole:
        value = provider_role.strip().lower()
        if value == "reference":
            value = EvidenceRole.IMPORTED_REFERENCE.value
        if value == "corpus":
            value = EvidenceRole.CORPUS_ATTESTATION.value
        if value == "external_reference":
            value = EvidenceRole.AMBIGUOUS_EXTERNAL.value
        if value == "morphology":
            value = EvidenceRole.MORPHOLOGY_ANALYSIS.value
        if value == "fuzzy_ocr":
            value = EvidenceRole.FUZZY_OCR.value
        try:
            return EvidenceRole(value)
        except ValueError:
            return EvidenceRole.AMBIGUOUS_EXTERNAL

    @staticmethod
    def _exact_reference(role: EvidenceRole, *, definition_quality: str) -> LexicalMatchClassification:
        confidence = {
            EvidenceRole.CURATED_LEXICON: 1.0,
            EvidenceRole.IMPORTED_REFERENCE: 0.95,
            EvidenceRole.WEB_DICTIONARY: 0.9,
        }.get(role, 0.9)
        return LexicalMatchClassification(
            match_type=LexicalMatchType.EXACT_HEADWORD_MATCH,
            validation_strength=ValidationStrength.VALIDATES_WORD,
            evidence_role=role,
            evidence_strength="strong",
            definition_quality=definition_quality,
            confidence_score=confidence,
            is_exact_match=True,
            is_canonical_match=True,
        )

    @staticmethod
    def _substring(
        role: EvidenceRole,
        *,
        definition_quality: str,
        source_confidence: float | None,
    ) -> LexicalMatchClassification:
        return LexicalMatchClassification(
            match_type=LexicalMatchType.SUBSTRING_MATCH,
            validation_strength=ValidationStrength.DOES_NOT_VALIDATE,
            evidence_role=role,
            evidence_strength="none",
            definition_quality=definition_quality,
            confidence_score=source_confidence if source_confidence is not None else 0.05,
            is_substring_match=True,
            reason="substring/partial search hit does not validate the token",
        )

    @staticmethod
    def _rejected(role: EvidenceRole, *, reason: str) -> LexicalMatchClassification:
        return LexicalMatchClassification(
            match_type=LexicalMatchType.REJECTED_ARTIFACT,
            validation_strength=ValidationStrength.REJECTS,
            evidence_role=role,
            evidence_strength="none",
            definition_quality="unknown",
            confidence_score=0.0,
            reason=reason,
        )

    @staticmethod
    def _is_single_armenian_letter(value: str) -> bool:
        return len(value) == 1 and "ARMENIAN" in unicodedata.name(value, "")

    @staticmethod
    def _is_probable_ocr_noise(value: str, *, has_digits: bool) -> bool:
        if has_digits:
            return True
        if len(value) <= 1:
            return False
        letters = [char for char in value if unicodedata.category(char).startswith("L")]
        punctuation = [char for char in value if unicodedata.category(char).startswith("P")]
        symbols = [char for char in value if unicodedata.category(char).startswith("S")]
        return bool(punctuation or symbols) and len(letters) <= 1


def get_lexical_match_classifier() -> LexicalMatchClassifier:
    return LexicalMatchClassifier()
