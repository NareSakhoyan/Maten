from __future__ import annotations

import argparse
from collections import Counter
import json
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from app.core.database import session_scope
from app.core.resource_registry import get_resource_registry
from app.db.models import (
    Document,
    ExternalLookupCache,
    ExternalLookupResult,
    ExternalLookupSearchMode,
    LexemeForm,
    MorphologyAnalysis,
    MorphologyAnalysisStatus,
    NerEntityEntry,
    NerSource,
    ReferenceEntry,
    ReferenceSource,
)
from app.services.discovery.discovery_candidate_service import DiscoveryCandidateService, MorphologySummary
from app.services.source_metadata import provider_metadata
from app.utils.text_normalization import normalize_token


def _json_default(value: Any) -> str:
    return str(value)


def _pick_user_id(session, explicit_user_id: str | None) -> UUID | None:  # noqa: ANN001
    if explicit_user_id:
        return UUID(explicit_user_id)
    value = session.scalar(select(Document.user_id).limit(1))
    if value is not None:
        return value
    value = session.scalar(select(LexemeForm.user_id).limit(1))
    if value:
        return UUID(str(value))
    value = session.scalar(select(ReferenceSource.user_id).limit(1))
    return UUID(str(value)) if value else None


def _count_or_zero(session, statement) -> int:  # noqa: ANN001
    return int(session.scalar(statement) or 0)


def _manifest_rows() -> list[dict[str, Any]]:
    return [
        {
            "provider_key": entry.provider_key or entry.key,
            "enabled": entry.enabled,
            "provider_type": entry.provider_type,
            "evidence_role": entry.evidence_role,
            "language_profile": entry.language_profile or entry.language_variant,
            "priority": entry.priority,
            "can_validate_word": entry.can_validate_word,
            "default_runtime": entry.default_runtime,
            "source_kind": entry.source_kind,
            "resource_key": entry.key,
        }
        for entry in get_resource_registry().manifest.resources
    ]


def _provider_row(provider_key: str, enabled: bool = True) -> dict[str, Any]:
    metadata = provider_metadata(provider_key)
    return {
        "provider_key": metadata.provider_key,
        "enabled": enabled,
        "provider_type": metadata.provider_type,
        "evidence_role": metadata.evidence_role,
        "language_profile": metadata.language_profile,
        "priority": metadata.priority,
        "can_validate_word": metadata.can_validate_word,
        "can_attest_usage": metadata.can_attest_usage,
        "can_suggest_lemma": metadata.can_suggest_lemma,
        "can_suggest_named_entity": metadata.can_suggest_named_entity,
        "source_kind": metadata.source_kind,
        "total_entries_or_indexed_rows": 0,
        "exact_headword_matches": 0,
        "lemma_matches": 0,
        "corpus_attestations": 0,
        "ner_matches": 0,
        "substring_or_ambiguous_matches": 0,
        "validation_strength": "does_not_validate",
        "match_type": "ambiguous_search_result",
        "confidence_score": None,
        "sample_result": None,
        "warning": None,
    }


def debug_word(word: str, *, user_id: str | None = None, document_id: str | None = None) -> dict[str, Any]:
    normalized = normalize_token(word)
    service = DiscoveryCandidateService()
    with session_scope() as session:
        resolved_user_id = _pick_user_id(session, user_id)
        document = session.get(Document, UUID(document_id)) if document_id else None
        document_profile = service._document_language_profile(document.language_stage if document else None)
        rows: dict[str, dict[str, Any]] = {
            manifest_row["provider_key"]: _provider_row(str(manifest_row["provider_key"]), bool(manifest_row["enabled"]))
            for manifest_row in _manifest_rows()
        }

        if resolved_user_id is not None:
            lexeme_total = _count_or_zero(
                session,
                select(func.count()).select_from(LexemeForm).where(LexemeForm.user_id == str(resolved_user_id)),
            )
            lexeme_matches = session.scalars(
                select(LexemeForm).where(
                    LexemeForm.user_id == str(resolved_user_id),
                    LexemeForm.normalized_form == normalized,
                )
            ).all()
            row = rows.setdefault("internal_lexicon", _provider_row("internal_lexicon"))
            row["total_entries_or_indexed_rows"] = lexeme_total
            row["exact_headword_matches"] = len(lexeme_matches)
            if lexeme_matches:
                row["validation_strength"] = "validates_word"
                row["match_type"] = "exact_headword_match"
                row["confidence_score"] = 1.0
                row["sample_result"] = {"lexeme_id": str(lexeme_matches[0].lexeme_id)}

            reference_total = _count_or_zero(
                session,
                select(func.count())
                .select_from(ReferenceEntry)
                .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
                .where(ReferenceSource.user_id == str(resolved_user_id), ReferenceSource.is_active.is_(True)),
            )
            reference_matches = session.scalars(
                select(ReferenceEntry)
                .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
                .where(
                    ReferenceSource.user_id == str(resolved_user_id),
                    ReferenceSource.is_active.is_(True),
                    ReferenceEntry.normalized_form == normalized,
                )
                .limit(3)
            ).all()
            row = rows.setdefault("imported_references", _provider_row("imported_references"))
            row["total_entries_or_indexed_rows"] = reference_total
            row["exact_headword_matches"] = len(reference_matches)
            if reference_matches:
                row["validation_strength"] = "validates_word"
                row["match_type"] = "exact_headword_match"
                row["confidence_score"] = 0.95
                row["sample_result"] = {
                    "surface_form": reference_matches[0].surface_form,
                    "source": reference_matches[0].source.display_name if reference_matches[0].source else None,
                }

            morphology_rows = session.scalars(
                select(MorphologyAnalysis).where(
                    MorphologyAnalysis.user_id == str(resolved_user_id),
                    MorphologyAnalysis.token_normalized == normalized,
                    MorphologyAnalysis.analysis_status == MorphologyAnalysisStatus.COMPLETED,
                )
            ).all()
            for analysis in morphology_rows:
                provider_key = service._morphology_provider_key(analysis.analyzer_model_key)
                row = rows.setdefault(provider_key, _provider_row(provider_key))
                row["total_entries_or_indexed_rows"] += 1
                row["lemma_matches"] += 1 if analysis.lemma_normalized else 0
                row["validation_strength"] = "suggests_candidate"
                row["match_type"] = "morphology_analysis_only"
                row["confidence_score"] = 0.35
                row["sample_result"] = {
                    "lemma": analysis.lemma_normalized,
                    "pos": analysis.pos,
                    "features": analysis.morph_features,
                }
                row["warning"] = "Morphology suggests lemmas/features only; it does not validate word existence."

        for provider_key in ("nayiri_western_corpus",):
            row = rows.setdefault(provider_key, _provider_row(provider_key, service.nayiri_corpus_enabled))
            row["enabled"] = service.nayiri_corpus_enabled
            if service.nayiri_corpus_enabled:
                matches = service.nayiri_corpus_service.lookup(normalized, limit=3)
                row["corpus_attestations"] = len(matches)
                if matches:
                    row["validation_strength"] = "supports_word"
                    row["match_type"] = "corpus_token_attestation"
                    row["confidence_score"] = 0.65 if matches[0].token_count >= 3 else 0.45
                    row["sample_result"] = {
                        "canonical_form": matches[0].canonical_form,
                        "token_count": matches[0].token_count,
                        "source_count": matches[0].source_count,
                    }
                    row["warning"] = "Corpus attestation supports usage; it is not a dictionary definition."

        ner_total = _count_or_zero(session, select(func.count()).select_from(NerEntityEntry))
        ner_matches = session.scalars(
            select(NerEntityEntry)
            .join(NerSource, NerEntityEntry.source_id == NerSource.id)
            .where(
                NerSource.provider_key == "pioner_ner",
                NerSource.is_active.is_(True),
                NerEntityEntry.normalized_surface == normalized,
            )
            .limit(3)
        ).all()
        row = rows.setdefault("pioner_ner", _provider_row("pioner_ner"))
        row["total_entries_or_indexed_rows"] = ner_total
        row["ner_matches"] = len(ner_matches)
        if ner_matches:
            row["validation_strength"] = "suggests_candidate"
            row["match_type"] = "named_entity_signal"
            row["confidence_score"] = float(ner_matches[0].confidence or 0.55)
            row["sample_result"] = {"surface": ner_matches[0].entity_surface, "entity_type": ner_matches[0].entity_type}
            row["warning"] = "Named-entity signal cannot validate a dictionary word by itself."

        external_matches = session.scalars(
            select(ExternalLookupResult)
            .join(ExternalLookupCache, ExternalLookupResult.cache_id == ExternalLookupCache.id)
            .where(
                ExternalLookupCache.search_mode == ExternalLookupSearchMode.NORMALIZED,
                ExternalLookupCache.normalized_query == normalized,
            )
            .limit(3)
        ).all()
        row = rows.setdefault("nayiri_web", _provider_row("nayiri_web"))
        row["total_entries_or_indexed_rows"] = _count_or_zero(session, select(func.count()).select_from(ExternalLookupResult))
        row["substring_or_ambiguous_matches"] = len(external_matches)
        if external_matches:
            evidence = service._collect_evidence(
                normalized,
                lexeme_map={},
                reference_map={},
                corpus_map={},
                external_map={normalized: external_matches},
                ner_map={},
                morphology_summary=MorphologySummary(Counter(), Counter()),
                known_lemmas=set(),
                document_language_stage=document_profile,
            )
            external_evidence = [item for item in evidence if item.provider_key == "nayiri_web"]
            if external_evidence:
                item = external_evidence[0]
                row["validation_strength"] = item.validation_strength
                row["match_type"] = item.match_type
                row["confidence_score"] = item.confidence
                row["exact_headword_matches"] = 1 if item.match_type == "exact_headword_match" else 0
                row["sample_result"] = item.payload
                if item.validation_strength != "validates_word":
                    row["warning"] = "Cached web/page evidence validates only exact structured headword matches."

        return {
            "word": word,
            "normalized_word": normalized,
            "user_id": str(resolved_user_id) if resolved_user_id else None,
            "document_language_profile": document_profile,
            "providers": sorted(rows.values(), key=lambda item: (item["priority"], item["provider_key"])),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Explain Baghramyan source lookup and validation roles.")
    parser.add_argument("--word", required=True)
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--document-id", default=None)
    args = parser.parse_args()
    print(json.dumps(debug_word(args.word, user_id=args.user_id, document_id=args.document_id), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
