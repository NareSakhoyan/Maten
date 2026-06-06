from __future__ import annotations

from app.db.models import OccurrenceScriptType
from app.services.discovery.resolution_engine import EvidenceResult, ResolutionEngine, ResolutionInput


def _input(**overrides):
    data = {
        "normalized_form": "անծանօթ",
        "occurrence_count": 2,
        "page_count": 1,
        "dominant_script_type": OccurrenceScriptType.ARMENIAN,
        "evidence": [],
        "has_armenian": True,
        "has_digits": False,
        "morphology_plausible": False,
        "morphology_lemma_known": False,
    }
    data.update(overrides)
    return ResolutionInput(**data)


def test_strong_direct_good_definition_is_suppressed_known() -> None:
    result = ResolutionEngine().resolve(
        _input(
            evidence=[
                EvidenceResult(
                    provider_key="internal_lexicon",
                    provider_type="curated_lexicon",
                    query_form="գիրք",
                    matched_form="գիրք",
                    match_type="exact_headword_match",
                    validation_strength="validates_word",
                    evidence_strength="strong",
                    definition_quality="good",
                )
            ]
        )
    )

    assert result.resolution_status == "resolved_known"
    assert result.candidate_type == "known_suppressed"
    assert result.suppressed is True
    assert result.interest_score == 0


def test_strong_dictionary_definition_is_resolved_by_dictionary() -> None:
    result = ResolutionEngine().resolve(
        _input(
            evidence=[
                EvidenceResult(
                    provider_key="nayiri_web",
                    provider_type="external_dictionary",
                    query_form="բառ",
                    matched_form="բառ",
                    match_type="exact_headword_match",
                    validation_strength="validates_word",
                    evidence_strength="strong",
                    definition_quality="good",
                )
            ]
        )
    )

    assert result.resolution_status == "resolved_by_dictionary"
    assert result.candidate_type == "known_suppressed"
    assert result.suppressed is True


def test_strong_corpus_attestation_is_suppressed_but_not_defined() -> None:
    result = ResolutionEngine().resolve(
        _input(
            evidence=[
                EvidenceResult(
                    provider_key="nayiri_corpus",
                    provider_type="corpus",
                    query_form="բառ",
                    matched_form="բառ",
                    match_type="corpus_token_attestation",
                    validation_strength="supports_word",
                    evidence_strength="strong",
                    definition_quality="unknown",
                )
            ]
        )
    )

    assert result.resolution_status == "attested_in_corpus"
    assert result.candidate_type == "attested_suppressed"
    assert result.suppressed is True
    assert result.best_evidence_summary["definition_quality"] == "unknown"


def test_lemma_match_is_resolved_by_lemma_and_suppressed() -> None:
    result = ResolutionEngine().resolve(
        _input(
            morphology_plausible=True,
            evidence=[
                EvidenceResult(
                    provider_key="morphology",
                    provider_type="reference",
                    query_form="գրոց",
                    lemma="գիրք",
                    match_type="exact_lemma_match",
                    validation_strength="supports_word",
                    evidence_strength="medium",
                    definition_quality="good",
                )
            ],
        )
    )

    assert result.resolution_status == "resolved_by_lemma"
    assert result.suppressed is True


def test_poor_definition_is_suppressed_when_source_attests_form() -> None:
    result = ResolutionEngine().resolve(
        _input(
            evidence=[
                EvidenceResult(
                    provider_key="reference",
                    provider_type="reference",
                    query_form="բառ",
                    matched_form="բառ",
                    match_type="exact_headword_match",
                    validation_strength="validates_word",
                    evidence_strength="medium",
                    definition_quality="poor",
                )
            ]
        )
    )

    assert result.resolution_status == "poorly_defined"
    assert result.candidate_type == "poorly_defined"
    assert result.suppressed is True
    assert result.interest_score == 0


def test_weak_corpus_attestation_is_suppressed_as_attested() -> None:
    result = ResolutionEngine().resolve(
        _input(
            evidence=[
                EvidenceResult(
                    provider_key="nayiri_corpus",
                    provider_type="corpus",
                    query_form="բառ",
                    matched_form="բառ",
                    match_type="corpus_token_attestation",
                    validation_strength="supports_word",
                    evidence_strength="weak",
                    definition_quality="unknown",
                )
            ]
        )
    )

    assert result.resolution_status == "attested_in_corpus"
    assert result.candidate_type == "attested_suppressed"
    assert result.suppressed is True
    assert result.interest_score == 0


def test_named_entity_evidence_never_resolves_as_lexical_word() -> None:
    result = ResolutionEngine().resolve(
        _input(
            normalized_form="երեւան",
            occurrence_count=1,
            page_count=1,
            evidence=[
                EvidenceResult(
                    provider_key="pioner_ner",
                    provider_type="ner",
                    evidence_role="named_entity_signal",
                    query_form="երեւան",
                    matched_form="Երեւան",
                    match_type="named_entity_signal",
                    validation_strength="suggests_candidate",
                    evidence_strength="medium",
                    definition_quality="unknown",
                    confidence=0.85,
                    is_exact_match=True,
                    payload={"entity_type": "LOC"},
                )
            ],
        )
    )

    assert result.resolution_status == "possible_named_entity"
    assert result.candidate_type == "named_entity_candidate"
    assert result.suppressed is False
    assert result.best_evidence_summary["validation_strength"] != "validates_word"


def test_repeated_unresolved_armenian_form_is_unknown_plausible() -> None:
    result = ResolutionEngine().resolve(
        _input(occurrence_count=4, page_count=2, morphology_plausible=True)
    )

    assert result.resolution_status == "unknown_plausible"
    assert result.candidate_type == "unknown_plausible"
    assert result.suppressed is False
    assert result.interest_score == 98


def test_one_off_digit_mixed_form_is_probable_ocr_noise() -> None:
    result = ResolutionEngine().resolve(
        _input(
            normalized_form="2ն",
            occurrence_count=1,
            page_count=1,
            dominant_script_type=OccurrenceScriptType.DIGIT_MIXED,
            has_digits=True,
        )
    )

    assert result.resolution_status == "probable_ocr_noise"
    assert result.candidate_type == "noise_suppressed"
    assert result.suppressed is True


def test_repeated_digit_mixed_form_is_possible_ocr_corruption() -> None:
    result = ResolutionEngine().resolve(
        _input(
            normalized_form="2ն",
            occurrence_count=3,
            page_count=2,
            dominant_script_type=OccurrenceScriptType.DIGIT_MIXED,
            has_digits=True,
        )
    )

    assert result.resolution_status == "possible_ocr_noise"
    assert result.candidate_type == "possible_ocr_corruption"
    assert result.suppressed is False


def test_repeated_latin_only_form_is_suppressed_as_noise() -> None:
    result = ResolutionEngine().resolve(
        _input(
            normalized_form="lorem",
            occurrence_count=8,
            page_count=3,
            dominant_script_type=OccurrenceScriptType.LATIN,
            has_armenian=False,
        )
    )

    assert result.resolution_status == "probable_ocr_noise"
    assert result.candidate_type == "noise_suppressed"
    assert result.suppressed is True
    assert result.interest_score == 0


def test_conflicting_sources_stay_in_discovery_queue() -> None:
    result = ResolutionEngine().resolve(
        _input(
            evidence=[
                EvidenceResult(
                    provider_key="reference_a",
                    provider_type="reference",
                    query_form="ձեւ",
                    matched_form="ձեւ",
                    lemma="ձեւ",
                    match_type="exact_headword_match",
                    validation_strength="validates_word",
                    evidence_strength="medium",
                    definition_quality="unknown",
                ),
                EvidenceResult(
                    provider_key="reference_b",
                    provider_type="reference",
                    query_form="ձեւ",
                    matched_form="ձեւ",
                    lemma="ձեւել",
                    match_type="exact_headword_match",
                    validation_strength="validates_word",
                    evidence_strength="medium",
                    definition_quality="unknown",
                ),
            ]
        )
    )

    assert result.resolution_status == "conflicting_sources"
    assert result.candidate_type == "conflicting_sources"
    assert result.suppressed is False


def test_nayiri_page_fulltext_context_does_not_validate() -> None:
    result = ResolutionEngine().resolve(
        _input(
            evidence=[
                EvidenceResult(
                    provider_key="nayiri_web",
                    provider_type="ambiguous_external",
                    evidence_role="classified_external_result",
                    query_form="տուն",
                    matched_form="տուն",
                    match_type="page_fulltext_occurrence",
                    validation_strength="context_only",
                    evidence_strength="none",
                    definition_quality="unknown",
                    confidence=0.05,
                    is_substring_match=True,
                )
            ]
        )
    )

    assert result.resolution_status in {"unknown_plausible", "needs_linguist_research"}
    assert result.suppressed is True
    assert result.best_evidence_summary.get("validation_strength") != "validates_word"


def test_profile_mismatched_corpus_still_suppresses_as_attested() -> None:
    result = ResolutionEngine().resolve(
        _input(
            language_profile="eastern",
            evidence=[
                EvidenceResult(
                    provider_key="nayiri_western_corpus",
                    provider_type="corpus",
                    evidence_role="corpus_attestation",
                    language_profile="western",
                    query_form="բառ",
                    matched_form="բառ",
                    match_type="corpus_token_attestation",
                    validation_strength="supports_word",
                    evidence_strength="strong",
                    definition_quality="unknown",
                    confidence=0.65,
                )
            ],
        )
    )

    assert result.resolution_status == "attested_in_corpus"
    assert result.candidate_type == "attested_suppressed"
    assert result.suppressed is True


def test_classical_profile_prefers_classical_provider_priority() -> None:
    result = ResolutionEngine().resolve(
        _input(
            language_profile="classical",
            evidence=[
                EvidenceResult(
                    provider_key="pie_eastern_morphology",
                    provider_type="morphology",
                    evidence_role="lemma_pos_features",
                    language_profile="eastern",
                    query_form="բան",
                    lemma="բան",
                    match_type="morphology_analysis_only",
                    validation_strength="suggests_candidate",
                    evidence_strength="weak",
                    confidence=0.7,
                ),
                EvidenceResult(
                    provider_key="pie_classical_morphology",
                    provider_type="morphology",
                    evidence_role="lemma_pos_features",
                    language_profile="classical",
                    query_form="բան",
                    lemma="բան",
                    match_type="morphology_analysis_only",
                    validation_strength="suggests_candidate",
                    evidence_strength="weak",
                    confidence=0.35,
                ),
            ],
            morphology_plausible=True,
        )
    )

    assert result.best_evidence_summary["provider_key"] == "pie_classical_morphology"


def test_eastern_profile_prefers_eastern_provider_priority() -> None:
    result = ResolutionEngine().resolve(
        _input(
            language_profile="eastern",
            evidence=[
                EvidenceResult(
                    provider_key="pie_classical_morphology",
                    provider_type="morphology",
                    evidence_role="lemma_pos_features",
                    language_profile="classical",
                    query_form="ասել",
                    lemma="ասել",
                    match_type="morphology_analysis_only",
                    validation_strength="suggests_candidate",
                    evidence_strength="weak",
                    confidence=0.7,
                ),
                EvidenceResult(
                    provider_key="pie_eastern_morphology",
                    provider_type="morphology",
                    evidence_role="lemma_pos_features",
                    language_profile="eastern",
                    query_form="ասել",
                    lemma="ասել",
                    match_type="morphology_analysis_only",
                    validation_strength="suggests_candidate",
                    evidence_strength="weak",
                    confidence=0.35,
                ),
            ],
            morphology_plausible=True,
        )
    )

    assert result.best_evidence_summary["provider_key"] == "pie_eastern_morphology"


def test_western_profile_accepts_western_corpus_attestation() -> None:
    result = ResolutionEngine().resolve(
        _input(
            language_profile="western",
            evidence=[
                EvidenceResult(
                    provider_key="nayiri_western_corpus",
                    provider_type="corpus",
                    evidence_role="corpus_attestation",
                    language_profile="western",
                    query_form="վկայուած",
                    matched_form="վկայուած",
                    match_type="corpus_token_attestation",
                    validation_strength="supports_word",
                    evidence_strength="strong",
                    definition_quality="unknown",
                    confidence=0.65,
                )
            ],
        )
    )

    assert result.resolution_status == "attested_in_corpus"
