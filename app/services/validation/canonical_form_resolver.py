from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.validation.lexical_match_classifier import (
    LexicalMatchType,
    ValidationStrength,
)


@dataclass(frozen=True, slots=True)
class CanonicalFormResolution:
    canonical_form: str | None
    canonical_source: str | None
    canonical_confidence: float | None
    candidate_lemmas: list[str] = field(default_factory=list)
    reason: str = "unresolved"
    conflicting_sources: list[str] = field(default_factory=list)


class CanonicalFormResolver:
    def resolve(
        self,
        *,
        normalized_form: str,
        evidence: list[Any],
        candidate_lemmas: list[str] | None = None,
    ) -> CanonicalFormResolution:
        lemmas = list(dict.fromkeys(candidate_lemmas or [item.lemma for item in evidence if getattr(item, "lemma", None)]))
        candidates: list[tuple[int, str, float, str]] = []

        for item in evidence:
            form = getattr(item, "result_headword", None) or getattr(item, "matched_form", None) or getattr(item, "lemma", None)
            if not form:
                continue
            match_type = getattr(item, "match_type", "")
            validation_strength = getattr(item, "validation_strength", "")
            provider_key = getattr(item, "provider_key", "unknown")
            confidence = getattr(item, "confidence", None) or getattr(item, "confidence_score", None) or 0.0
            provider_type = getattr(item, "provider_type", "")

            priority = None
            if provider_type == "curated_lexicon" and validation_strength == ValidationStrength.VALIDATES_WORD.value:
                priority = 1
            elif provider_type == "reference" and validation_strength == ValidationStrength.VALIDATES_WORD.value:
                priority = 2
            elif provider_type == "web_dictionary" and validation_strength == ValidationStrength.VALIDATES_WORD.value:
                priority = 3
            elif match_type == LexicalMatchType.EXACT_LEMMA_MATCH.value:
                priority = 4
            elif match_type in {
                LexicalMatchType.CORPUS_TOKEN_ATTESTATION.value,
                LexicalMatchType.CORPUS_LEMMA_ATTESTATION.value,
            }:
                priority = 5

            if priority is not None:
                candidates.append((priority, str(form), float(confidence), provider_key))

        if not candidates:
            return CanonicalFormResolution(
                canonical_form=None,
                canonical_source=None,
                canonical_confidence=None,
                candidate_lemmas=lemmas,
                reason="no exact lexical or explicit lemma evidence",
            )

        candidates.sort(key=lambda row: (row[0], -row[2], row[3]))
        best_priority = candidates[0][0]
        best_tier = [candidate for candidate in candidates if candidate[0] == best_priority]
        forms = {form for _, form, _, _ in best_tier}
        if len(forms) > 1:
            return CanonicalFormResolution(
                canonical_form=None,
                canonical_source=None,
                canonical_confidence=None,
                candidate_lemmas=lemmas,
                reason="conflicting_sources",
                conflicting_sources=sorted({source for _, _, _, source in best_tier}),
            )

        _, form, confidence, source = candidates[0]
        return CanonicalFormResolution(
            canonical_form=form,
            canonical_source=source,
            canonical_confidence=confidence,
            candidate_lemmas=lemmas,
            reason="selected_by_strict_priority",
        )


def get_canonical_form_resolver() -> CanonicalFormResolver:
    return CanonicalFormResolver()
