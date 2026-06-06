from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.db.models import OccurrenceScriptType
from app.services.source_metadata import profile_weight, provider_metadata, normalize_language_profile
from app.services.validation.lexical_match_classifier import ValidationStrength


KNOWN_EVIDENCE_TYPES = {"curated_lexicon"}
DICTIONARY_EVIDENCE_TYPES = {"imported_dictionary", "reference", "dictionary", "external_dictionary", "web_dictionary"}
STRONG_EVIDENCE_TYPES = KNOWN_EVIDENCE_TYPES | DICTIONARY_EVIDENCE_TYPES
SUPPRESSED_RESOLUTION_STATUSES = {
    "resolved_known",
    "resolved_by_dictionary",
    "attested_in_corpus",
    "resolved_by_lemma",
    "resolved_as_variant",
    "poorly_defined",
    "weakly_attested",
    "needs_linguist_research",
    "probable_ocr_noise",
}


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    provider_key: str
    provider_type: str
    query_form: str
    evidence_role: str = ""
    matched_form: str | None = None
    result_headword: str | None = None
    lemma: str | None = None
    match_type: str = "none"
    validation_strength: str = ValidationStrength.DOES_NOT_VALIDATE.value
    evidence_strength: str = "none"
    definition_quality: str = "unknown"
    language_variant: str | None = None
    language_profile: str | None = None
    priority: int = 100
    can_validate_word: bool | str = False
    can_attest_usage: bool = False
    can_suggest_lemma: bool = False
    can_suggest_named_entity: bool = False
    requires_exact_match: bool = False
    requires_structured_headword: bool = False
    default_runtime: str = "disabled"
    independent_source_group: str | None = None
    source_kind: str | None = None
    confidence: float | None = None
    confidence_score: float | None = None
    is_exact_match: bool = False
    is_substring_match: bool = False
    is_fuzzy_match: bool = False
    is_canonical_match: bool = False
    citation: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        metadata = provider_metadata(self.provider_key, provider_type=self.provider_type)
        if not self.evidence_role:
            object.__setattr__(self, "evidence_role", metadata.evidence_role)
        if self.language_profile is None:
            object.__setattr__(self, "language_profile", metadata.language_profile)
        if self.priority == 100:
            object.__setattr__(self, "priority", metadata.priority)
        if self.can_validate_word is False:
            object.__setattr__(self, "can_validate_word", metadata.can_validate_word)
        if not self.can_attest_usage:
            object.__setattr__(self, "can_attest_usage", metadata.can_attest_usage)
        if not self.can_suggest_lemma:
            object.__setattr__(self, "can_suggest_lemma", metadata.can_suggest_lemma)
        if not self.can_suggest_named_entity:
            object.__setattr__(self, "can_suggest_named_entity", metadata.can_suggest_named_entity)
        if not self.requires_exact_match:
            object.__setattr__(self, "requires_exact_match", metadata.requires_exact_match)
        if not self.requires_structured_headword:
            object.__setattr__(self, "requires_structured_headword", metadata.requires_structured_headword)
        if self.default_runtime == "disabled":
            object.__setattr__(self, "default_runtime", metadata.default_runtime)
        if self.independent_source_group is None:
            object.__setattr__(self, "independent_source_group", metadata.independent_source_group)
        if self.source_kind is None:
            object.__setattr__(self, "source_kind", metadata.source_kind)


@dataclass(frozen=True, slots=True)
class ResolutionInput:
    normalized_form: str
    occurrence_count: int
    page_count: int
    dominant_script_type: OccurrenceScriptType | str
    evidence: list[EvidenceResult] = field(default_factory=list)
    linked_lexeme_id: str | None = None
    has_armenian: bool = True
    has_digits: bool = False
    morphology_plausible: bool = False
    morphology_lemma_known: bool = False
    language_profile: str = "unknown"


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    resolution_status: str
    candidate_type: str
    interest_score: float
    confidence_score: float | None
    ocr_risk_score: float
    morphology_plausibility_score: float | None
    definition_quality_score: float | None
    suppressed: bool
    reasons: list[str]
    best_evidence_summary: dict[str, Any]


class ResolutionEngine:
    def resolve(self, item: ResolutionInput) -> ResolutionResult:
        dominant_script_type = self._script_type_value(item.dominant_script_type)
        ocr_risk_score = self._ocr_risk_score(item, dominant_script_type=dominant_script_type)
        morphology_score = 0.8 if item.morphology_plausible else None
        definition_score = self._definition_quality_score(item.evidence)
        evidence = self._ordered_evidence(item.evidence, language_profile=item.language_profile)
        reasons: list[str] = []

        known_direct = self._best_evidence(
            evidence,
            match_types={"exact_headword_match"},
            validation_strengths={ValidationStrength.VALIDATES_WORD.value},
            strengths={"strong"},
            good_definition=True,
            provider_types=KNOWN_EVIDENCE_TYPES,
        )
        if item.linked_lexeme_id or known_direct is not None:
            reasons.append("A trusted source directly resolves this form with a usable definition.")
            return self._build_result(
                "resolved_known",
                "known_suppressed",
                0,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
                best_evidence=known_direct,
            )

        dictionary_direct = self._best_evidence(
            evidence,
            match_types={"exact_headword_match"},
            validation_strengths={ValidationStrength.VALIDATES_WORD.value},
            strengths={"strong"},
            good_definition=True,
            provider_types=DICTIONARY_EVIDENCE_TYPES,
        )
        if dictionary_direct is not None:
            reasons.append("Dictionary or web article evidence defines this form.")
            return self._build_result(
                "resolved_by_dictionary",
                "known_suppressed",
                0,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
                best_evidence=dictionary_direct,
            )

        lemma_match = self._best_evidence(
            evidence,
            match_types={"exact_lemma_match"},
            validation_strengths={ValidationStrength.SUPPORTS_WORD.value},
            strengths={"strong", "medium"},
            good_definition=True,
            trusted_only=True,
        )
        if lemma_match is not None:
            reasons.append("Morphology maps this form to a lemma with exact lexical evidence.")
            return self._build_result(
                "resolved_by_lemma",
                "known_suppressed",
                10,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
                best_evidence=lemma_match,
            )

        variant_match = self._best_evidence(
            evidence,
            match_types={"approved_variant_match"},
            validation_strengths={ValidationStrength.SUPPORTS_WORD.value},
            strengths={"strong", "medium"},
            good_definition=True,
            trusted_only=True,
        )
        if variant_match is not None and ocr_risk_score < 0.6:
            reasons.append("Trusted evidence treats this as a known variant.")
            return self._build_result(
                "resolved_as_variant",
                "known_suppressed",
                15,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
                best_evidence=variant_match,
            )

        if self._has_conflicting_sources(evidence):
            reasons.append("Available sources disagree about the form or lemma.")
            return self._build_result(
                "conflicting_sources",
                "conflicting_sources",
                70,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
            )

        corpus_attestation = self._best_evidence(
            evidence,
            match_types={"corpus_token_attestation", "corpus_lemma_attestation"},
            validation_strengths={ValidationStrength.SUPPORTS_WORD.value},
            strengths={"strong", "medium", "weak"},
            provider_types={"corpus"},
        )
        if corpus_attestation is not None and ocr_risk_score < 0.45:
            reasons.append("The form is attested in the local Nayiri corpus.")
            return self._build_result(
                "attested_in_corpus",
                "attested_suppressed",
                0,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
                best_evidence=corpus_attestation,
            )

        poor_definition = self._best_evidence(
            evidence,
            strengths={"strong", "medium"},
            definition_qualities={"poor", "missing"},
        )
        if poor_definition is not None:
            reasons.append("A source attests this form; definition quality is not used for linguist review.")
            return self._build_result(
                "poorly_defined",
                "poorly_defined",
                0,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
                best_evidence=poor_definition,
            )

        morphology_only = self._best_evidence(
            evidence,
            match_types={"morphology_analysis_only"},
            validation_strengths={ValidationStrength.SUGGESTS_CANDIDATE.value},
            provider_types={"morphology"},
        )
        if morphology_only is not None and item.has_armenian and ocr_risk_score < 0.45:
            reasons.append(
                "Morphology can analyze this form; review it as a plausible Armenian form."
            )
            return self._build_result(
                "unknown_plausible",
                "unknown_plausible",
                75,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
                best_evidence=morphology_only,
            )

        rejecting_evidence = self._best_evidence(
            evidence,
            match_types={"rejected_artifact"},
            validation_strengths={ValidationStrength.REJECTS.value},
        )
        if rejecting_evidence is not None:
            reasons.append(rejecting_evidence.payload.get("classification_reason", "Rejected by strict validation."))
            return self._build_result(
                "probable_ocr_noise",
                "noise_suppressed",
                0,
                item,
                ocr_risk_score=max(ocr_risk_score, 0.75),
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
                best_evidence=rejecting_evidence,
            )

        named_entity = self._best_evidence(
            evidence,
            match_types={"named_entity_signal"},
            validation_strengths={ValidationStrength.SUGGESTS_CANDIDATE.value},
            provider_types={"ner"},
        )
        if named_entity is not None and item.has_armenian:
            reasons.append("pioNER evidence suggests this form may be a named entity, not a dictionary-resolved word.")
            return self._build_result(
                "possible_named_entity",
                "named_entity_candidate",
                72,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
                best_evidence=named_entity,
            )

        weak_evidence = self._best_evidence(
            evidence,
            strengths={"weak"},
            validation_strengths={
                ValidationStrength.SUPPORTS_WORD.value,
                ValidationStrength.SUGGESTS_CANDIDATE.value,
            },
        )
        if weak_evidence is not None:
            reasons.append("The form has weak or limited attestation and is hidden from the default review queue.")
            return self._build_result(
                "weakly_attested",
                "weakly_attested",
                0,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
                best_evidence=weak_evidence,
            )

        if dominant_script_type == OccurrenceScriptType.LATIN.value and not item.has_armenian:
            reasons.append("The OCR token is Latin-only and outside the Armenian review scope.")
            return self._build_result(
                "probable_ocr_noise",
                "noise_suppressed",
                0,
                item,
                ocr_risk_score=max(ocr_risk_score, 0.75),
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
            )

        if ocr_risk_score >= 0.75 and item.occurrence_count <= 1:
            reasons.append("The form looks like OCR noise and appears only once.")
            return self._build_result(
                "probable_ocr_noise",
                "noise_suppressed",
                0,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
            )

        if ocr_risk_score >= 0.45:
            reasons.append("The form has OCR risk, but frequency or context makes it worth checking.")
            return self._build_result(
                "possible_ocr_noise",
                "possible_ocr_corruption",
                55,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
            )

        if item.has_armenian and (item.occurrence_count > 1 or item.morphology_plausible):
            reasons.append(
                "The form is Armenian-looking, unresolved, and repeated or morphologically plausible."
            )
            return self._build_result(
                "unknown_plausible",
                "unknown_plausible",
                80,
                item,
                ocr_risk_score=ocr_risk_score,
                morphology_score=morphology_score,
                definition_score=definition_score,
                reasons=reasons,
            )

        reasons.append("The form is unresolved and hidden from the default review queue.")
        return self._build_result(
            "needs_linguist_research",
            "needs_linguist_research",
            0,
            item,
            ocr_risk_score=ocr_risk_score,
            morphology_score=morphology_score,
            definition_score=definition_score,
            reasons=reasons,
        )

    def _build_result(
        self,
        resolution_status: str,
        candidate_type: str,
        base_score: float,
        item: ResolutionInput,
        *,
        ocr_risk_score: float,
        morphology_score: float | None,
        definition_score: float | None,
        reasons: list[str],
        best_evidence: EvidenceResult | None = None,
    ) -> ResolutionResult:
        interest_score = base_score
        if resolution_status not in SUPPRESSED_RESOLUTION_STATUSES:
            interest_score += min(15, item.occurrence_count)
            interest_score += min(10, item.page_count * 2)
            if item.morphology_plausible:
                interest_score += 10
            if ocr_risk_score >= 0.45:
                interest_score -= 30
        interest_score = max(0, min(100, interest_score))

        return ResolutionResult(
            resolution_status=resolution_status,
            candidate_type=candidate_type,
            interest_score=float(interest_score),
            confidence_score=self._confidence_score(best_evidence),
            ocr_risk_score=ocr_risk_score,
            morphology_plausibility_score=morphology_score,
            definition_quality_score=definition_score,
            suppressed=resolution_status in SUPPRESSED_RESOLUTION_STATUSES or candidate_type.endswith("_suppressed"),
            reasons=reasons,
            best_evidence_summary=self._summarize_evidence(best_evidence, reasons=reasons),
        )

    @staticmethod
    def _ordered_evidence(evidence: list[EvidenceResult], *, language_profile: str | None) -> list[EvidenceResult]:
        document_profile = normalize_language_profile(language_profile)

        def sort_key(item: EvidenceResult) -> tuple[float, int, float, str]:
            metadata = provider_metadata(item.provider_key, provider_type=item.provider_type)
            priority = item.priority if item.priority != 100 else metadata.priority
            source_profile = item.language_profile or metadata.language_profile
            profile_penalty = 0 if profile_weight(source_profile, document_profile) >= 1 else 25
            confidence = item.confidence if item.confidence is not None else item.confidence_score
            return (priority + profile_penalty, priority, -(confidence or 0.0), item.provider_key)

        return sorted(evidence, key=sort_key)

    @staticmethod
    def _best_evidence(
        evidence: list[EvidenceResult],
        *,
        match_types: set[str] | None = None,
        strengths: set[str] | None = None,
        validation_strengths: set[str] | None = None,
        definition_qualities: set[str] | None = None,
        good_definition: bool = False,
        trusted_only: bool = False,
        provider_types: set[str] | None = None,
        language_profile: str | None = None,
        min_profile_weight: float | None = None,
    ) -> EvidenceResult | None:
        for item in evidence:
            if match_types is not None and item.match_type not in match_types:
                continue
            if strengths is not None and item.evidence_strength not in strengths:
                continue
            if validation_strengths is not None and item.validation_strength not in validation_strengths:
                continue
            if definition_qualities is not None and item.definition_quality not in definition_qualities:
                continue
            if good_definition and item.definition_quality != "good":
                continue
            if provider_types is not None and item.provider_type not in provider_types:
                continue
            if min_profile_weight is not None and profile_weight(item.language_profile, language_profile) < min_profile_weight:
                continue
            if (
                item.validation_strength == ValidationStrength.VALIDATES_WORD.value
                and item.can_validate_word is not True
                and not (item.can_validate_word == "conditional" and item.is_exact_match and item.result_headword)
            ):
                continue
            if item.match_type in {"substring_match", "partial_match", "ambiguous_search_result", "page_fulltext_occurrence"}:
                continue
            if item.requires_exact_match and not item.is_exact_match:
                continue
            if item.requires_structured_headword and not item.result_headword:
                continue
            if trusted_only and item.provider_type not in STRONG_EVIDENCE_TYPES:
                continue
            return item
        return None

    @staticmethod
    def _has_conflicting_sources(evidence: list[EvidenceResult]) -> bool:
        meaningful = [
            item
            for item in evidence
            if item.evidence_strength in {"strong", "medium"}
            and item.validation_strength in {ValidationStrength.VALIDATES_WORD.value, ValidationStrength.SUPPORTS_WORD.value}
            and item.provider_type != "ner"
        ]
        if len(meaningful) < 2:
            return False
        resolved_forms = {
            (item.lemma or item.matched_form or "").strip()
            for item in meaningful
            if (item.lemma or item.matched_form or "").strip()
        }
        return len(resolved_forms) > 1

    @staticmethod
    def _script_type_value(script_type: OccurrenceScriptType | str) -> str:
        return script_type.value if isinstance(script_type, OccurrenceScriptType) else script_type

    @staticmethod
    def _ocr_risk_score(item: ResolutionInput, *, dominant_script_type: str) -> float:
        score = 0.0
        if dominant_script_type == OccurrenceScriptType.DIGIT_MIXED.value or item.has_digits:
            score += 0.8
        elif dominant_script_type in {OccurrenceScriptType.MIXED.value, OccurrenceScriptType.LATIN.value}:
            score += 0.55
        elif dominant_script_type == OccurrenceScriptType.OTHER.value:
            score += 0.7
        if not item.has_armenian:
            score += 0.2
        if item.occurrence_count > 1:
            score -= 0.15
        if item.page_count > 1:
            score -= 0.1
        return max(0.0, min(1.0, round(score, 2)))

    @staticmethod
    def _definition_quality_score(evidence: list[EvidenceResult]) -> float | None:
        values = {"good": 1.0, "poor": 0.35, "missing": 0.0, "unknown": 0.2}
        scores = [values[item.definition_quality] for item in evidence if item.definition_quality in values]
        return max(scores) if scores else None

    @staticmethod
    def _confidence_score(evidence: EvidenceResult | None) -> float | None:
        if evidence is None:
            return None
        if evidence.confidence is not None:
            return evidence.confidence
        if evidence.confidence_score is not None:
            return evidence.confidence_score
        return {"strong": 0.9, "medium": 0.65, "weak": 0.35, "none": 0.0}.get(evidence.evidence_strength)

    @staticmethod
    def _summarize_evidence(evidence: EvidenceResult | None, *, reasons: list[str]) -> dict[str, Any]:
        summary: dict[str, Any] = {"reasons": reasons}
        if evidence is None:
            return summary
        summary.update(
            {
                "provider_key": evidence.provider_key,
                "provider_type": evidence.provider_type,
                "matched_form": evidence.matched_form,
                "result_headword": evidence.result_headword,
                "lemma": evidence.lemma,
                "match_type": evidence.match_type,
                "validation_strength": evidence.validation_strength,
                "evidence_strength": evidence.evidence_strength,
                "definition_quality": evidence.definition_quality,
                "evidence_role": evidence.evidence_role,
                "language_profile": evidence.language_profile,
                "priority": evidence.priority,
                "can_validate_word": evidence.can_validate_word,
                "can_attest_usage": evidence.can_attest_usage,
                "can_suggest_lemma": evidence.can_suggest_lemma,
                "can_suggest_named_entity": evidence.can_suggest_named_entity,
                "requires_exact_match": evidence.requires_exact_match,
                "requires_structured_headword": evidence.requires_structured_headword,
                "default_runtime": evidence.default_runtime,
                "independent_source_group": evidence.independent_source_group,
                "source_kind": evidence.source_kind,
                "confidence_score": evidence.confidence if evidence.confidence is not None else evidence.confidence_score,
                "is_exact_match": evidence.is_exact_match,
                "is_substring_match": evidence.is_substring_match,
                "is_fuzzy_match": evidence.is_fuzzy_match,
                "is_canonical_match": evidence.is_canonical_match,
                "citation": evidence.citation,
            }
        )
        return summary
