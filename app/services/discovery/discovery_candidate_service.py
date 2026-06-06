from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.database import session_scope
from app.core.resource_registry import ResourceRegistry, get_resource_registry
from app.db.models import (
    DiscoveryCandidate,
    DiscoveryBuildRun,
    Document,
    DocumentPage,
    ExternalLookupCache,
    ExternalLookupResult,
    ExternalLookupSearchMode,
    ExternalLookupStatus,
    ExternalProvider,
    JobKind,
    JobResultResourceType,
    Lexeme,
    LexemeStatus,
    MorphologyRunStatus,
    LexemeForm,
    MorphologyAnalysis,
    MorphologyAnalysisStatus,
    NerEntityEntry,
    NerSource,
    Occurrence,
    OccurrenceScriptType,
    ReferenceEntry,
    ReferenceSource,
    ReferenceSourceImport,
)
from app.schemas.lexeme import LexemeCreateRequest
from app.schemas.discovery import (
    DiscoveryBuildRunRead,
    DiscoveryBuildSummary,
    DiscoveryEvidenceItem,
    DiscoveryOccurrenceEvidence,
    DocumentReferenceEvidenceState,
    DiscoverySummaryResponse,
)
from app.services.discovery.resolution_engine import EvidenceResult, ResolutionEngine, ResolutionInput
from app.services.backpressure_service import BackpressureService, get_backpressure_service
from app.services.job_orchestrator import JobOrchestrator, get_job_orchestrator
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.lexeme_resolution.lexeme_resolver import (
    LexemeResolution,
    LexemeResolver,
    analyzer_result_from_morphology_row,
    get_lexeme_resolver,
)
from app.services.lexeme_service import LexemeConflictError, get_lexeme_service
from app.services.nayiri_corpus_service import NayiriCorpusService, get_nayiri_corpus_service
from app.services.source_metadata import normalize_language_profile, profile_weight
from app.services.validation.canonical_form_resolver import CanonicalFormResolver, get_canonical_form_resolver
from app.services.validation.lexical_match_classifier import (
    EvidenceRole,
    LexicalMatchClassifier,
    ValidationStrength,
    get_lexical_match_classifier,
)
from app.utils.token_classification import classify_token
from app.utils.snippets import context_snippet_highlight_range


logger = logging.getLogger(__name__)

SAMPLE_LIMIT = 5
HIGH_FREQUENCY_PLAUSIBLE_MIN_OCCURRENCES = 4
HIGH_FREQUENCY_PLAUSIBLE_MIN_PAGES = 6
DEFAULT_HIDDEN_RESOLUTION_STATUSES = {
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
DEFAULT_HIDDEN_CANDIDATE_TYPES = {"known_suppressed", "attested_suppressed", "noise_suppressed"}
VALID_DECISIONS = {
    "mark_interesting",
    "mark_known",
    "mark_ocr_noise",
    "mark_uncertain",
    "mark_poorly_defined",
    "create_lexeme",
    "link_lexeme",
    "ignore",
}


@dataclass(slots=True)
class FormBucket:
    normalized_form: str
    occurrence_count: int = 0
    pages: set[int] = field(default_factory=set)
    sample_tokens: list[str] = field(default_factory=list)
    sample_contexts: list[str] = field(default_factory=list)
    script_counts: Counter[str] = field(default_factory=Counter)
    has_armenian: bool = False
    has_digits: bool = False

    def add(self, occurrence: Occurrence) -> None:
        self.occurrence_count += 1
        self.pages.add(occurrence.page_number)
        if occurrence.token not in self.sample_tokens and len(self.sample_tokens) < SAMPLE_LIMIT:
            self.sample_tokens.append(occurrence.token)
        if occurrence.context_snippet not in self.sample_contexts and len(self.sample_contexts) < SAMPLE_LIMIT:
            self.sample_contexts.append(occurrence.context_snippet)
        script_key = occurrence.script_type.value if hasattr(occurrence.script_type, "value") else str(occurrence.script_type)
        self.script_counts[script_key] += 1
        self.has_armenian = self.has_armenian or occurrence.has_armenian
        self.has_digits = self.has_digits or occurrence.has_digits

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def sample_pages(self) -> list[int]:
        return sorted(self.pages)[:SAMPLE_LIMIT]

    @property
    def dominant_script_type(self) -> str:
        if not self.script_counts:
            return OccurrenceScriptType.OTHER.value
        return self.script_counts.most_common(1)[0][0]


@dataclass(frozen=True, slots=True)
class MorphologySummary:
    lemma_counts: Counter[str]
    pos_counts: Counter[str]
    provider_counts: Counter[str] = field(default_factory=Counter)
    feature_counts: Counter[str] = field(default_factory=Counter)
    analyses: list[object] = field(default_factory=list)

    @property
    def plausible(self) -> bool:
        return bool(self.lemma_counts or self.pos_counts)

    @property
    def provider_key(self) -> str:
        if self.provider_counts:
            return self.provider_counts.most_common(1)[0][0]
        return "pie_eastern_morphology"


class DiscoveryCandidateService:
    def __init__(
        self,
        *,
        resolution_engine: ResolutionEngine | None = None,
        nayiri_corpus_service: NayiriCorpusService | None = None,
        lexical_match_classifier: LexicalMatchClassifier | None = None,
        canonical_form_resolver: CanonicalFormResolver | None = None,
        lexeme_resolver: LexemeResolver | None = None,
        job_progress_service: JobProgressService | None = None,
        job_orchestrator: JobOrchestrator | None = None,
        resource_registry: ResourceRegistry | None = None,
        backpressure_service: BackpressureService | None = None,
    ) -> None:
        self.resolution_engine = resolution_engine or ResolutionEngine()
        self.nayiri_corpus_service = nayiri_corpus_service or get_nayiri_corpus_service()
        self.lexical_match_classifier = lexical_match_classifier or get_lexical_match_classifier()
        self.canonical_form_resolver = canonical_form_resolver or get_canonical_form_resolver()
        self.lexeme_resolver = lexeme_resolver or get_lexeme_resolver()
        self.job_progress_service = job_progress_service or get_job_progress_service()
        self.job_orchestrator = job_orchestrator or get_job_orchestrator()
        self.resource_registry = resource_registry or get_resource_registry()
        self.backpressure_service = backpressure_service or get_backpressure_service()
        self.nayiri_corpus_enabled = self.resource_registry.resource_enabled("nayiri_western_corpus", default=True)

    def start_build_run(self, session: Session, *, user_id: UUID, document_id: UUID) -> DiscoveryBuildRun:
        document = self._get_document(session, user_id=user_id, document_id=document_id)
        if document is None:
            raise ValueError("Document not found.")
        active_run = session.scalar(
            select(DiscoveryBuildRun)
            .where(
                DiscoveryBuildRun.user_id == str(user_id),
                DiscoveryBuildRun.document_id == document_id,
                DiscoveryBuildRun.status.in_((MorphologyRunStatus.QUEUED, MorphologyRunStatus.RUNNING)),
            )
            .order_by(DiscoveryBuildRun.created_at.desc(), DiscoveryBuildRun.id.desc())
            .limit(1)
        )
        if active_run is not None:
            return active_run
        self.backpressure_service.ensure_user_capacity(session, user_id=user_id)
        run = DiscoveryBuildRun(
            user_id=str(user_id),
            document_id=document_id,
            status=MorphologyRunStatus.QUEUED,
            result_resource_type=JobResultResourceType.DOCUMENT,
            result_resource_id=str(document_id),
        )
        session.add(run)
        session.flush()
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.DISCOVERY_BUILD,
            job=run,
            stage_code="discovery_pending",
            progress_percent=0,
        )
        session.commit()
        session.refresh(run)
        self.job_orchestrator.enqueue(JobKind.DISCOVERY_BUILD, run.id)
        return run

    def start_reference_evidence_refresh_run(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        reference_source_id: UUID | None = None,
    ) -> DiscoveryBuildRun:
        document = self._get_document(session, user_id=user_id, document_id=document_id)
        if document is None:
            raise ValueError("Document not found.")
        state = self._select_reference_state_for_refresh(
            session,
            user_id=user_id,
            document_id=document_id,
            reference_source_id=reference_source_id,
        )
        if state is None:
            raise ValueError("No imported reference dataset needs an evidence refresh.")

        active_run = session.scalar(
            select(DiscoveryBuildRun)
            .where(
                DiscoveryBuildRun.user_id == str(user_id),
                DiscoveryBuildRun.document_id == document_id,
                DiscoveryBuildRun.build_mode == "reference_only",
                DiscoveryBuildRun.reference_source_id == state.reference_source_id,
                DiscoveryBuildRun.status.in_((MorphologyRunStatus.QUEUED, MorphologyRunStatus.RUNNING)),
            )
            .order_by(DiscoveryBuildRun.created_at.desc(), DiscoveryBuildRun.id.desc())
            .limit(1)
        )
        if active_run is not None:
            return active_run
        self.backpressure_service.ensure_user_capacity(session, user_id=user_id)

        run = DiscoveryBuildRun(
            user_id=str(user_id),
            document_id=document_id,
            build_mode="reference_only",
            reference_source_id=state.reference_source_id,
            reference_source_import_id=state.reference_source_import_id,
            status=MorphologyRunStatus.QUEUED,
            result_resource_type=JobResultResourceType.DOCUMENT,
            result_resource_id=str(document_id),
        )
        session.add(run)
        session.flush()
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.DISCOVERY_BUILD,
            job=run,
            stage_code="discovery_pending",
            message_user="Reference evidence refresh is waiting to start.",
            progress_percent=0,
        )
        session.commit()
        session.refresh(run)
        self.job_orchestrator.enqueue(JobKind.DISCOVERY_BUILD, run.id)
        return run

    def process_run(self, run_id: UUID | str) -> None:
        run_uuid = UUID(str(run_id))
        with session_scope() as session:
            run = self._load_run(session, run_uuid)
            run.status = MorphologyRunStatus.RUNNING
            run.error_message = None
            run.error_code = None
            run.error_message_user = None
            run.next_steps = None
            run.started_at = datetime.now(timezone.utc)
            run.finished_at = None
            run.progress_percent = 0
            run.items_processed = 0
            run.items_total = None
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.DISCOVERY_BUILD,
                job=run,
                stage_code="discovery_running",
                progress_percent=5,
            )

        try:
            with session_scope() as session:
                run = self._load_run(session, run_uuid)

                def stage_callback(stage_code: str, progress_percent: int) -> None:
                    self._commit_run_stage_checkpoint(
                        run_uuid,
                        stage_code=stage_code,
                        progress_percent=progress_percent,
                    )

                if run.build_mode == "reference_only":
                    summary, matched_count, unmatched_count = self.refresh_reference_evidence_for_document(
                        session,
                        user_id=UUID(run.user_id),
                        document_id=run.document_id,
                        reference_source_id=run.reference_source_id,
                        commit=False,
                        stage_callback=stage_callback,
                    )
                    run.matched_count = matched_count
                    run.unmatched_count = unmatched_count
                else:
                    summary = self.build_for_document(
                        session,
                        user_id=UUID(run.user_id),
                        document_id=run.document_id,
                        commit=False,
                        stage_callback=stage_callback,
                    )
                    run.matched_count = 0
                    run.unmatched_count = 0
                run.candidate_count = summary.total_grouped_forms
                run.shown_count = summary.shown_in_queue
                run.suppressed_count = summary.suppressed
                run.summary_counts = summary.model_dump()
                run.status = MorphologyRunStatus.COMPLETED
                self.job_progress_service.complete(
                    session,
                    job_kind=JobKind.DISCOVERY_BUILD,
                    job=run,
                    stage_code="discovery_done",
                    message_user=(
                        "Reference evidence has been refreshed."
                        if run.build_mode == "reference_only"
                        else "Discovery queue is ready."
                    ),
                )
        except Exception as exc:
            with session_scope() as session:
                run = self._load_run(session, run_uuid)
                run.status = MorphologyRunStatus.FAILED
                run.error_message = str(exc)
                run.error_code = "discovery_build_failed"
                run.error_message_user = "Discovery queue could not be built for this document."
                run.next_steps = [
                    "Retry the discovery build.",
                    "Verify the document has extracted occurrences.",
                ]
                self.job_progress_service.fail(
                    session,
                    job_kind=JobKind.DISCOVERY_BUILD,
                    job=run,
                    message_user=run.error_message_user,
                )
            raise

    def _commit_run_stage_checkpoint(
        self,
        run_id: UUID,
        *,
        stage_code: str,
        progress_percent: int,
    ) -> None:
        try:
            with session_scope() as session:
                run = self._load_run(session, run_id)
                if run.status not in (MorphologyRunStatus.QUEUED, MorphologyRunStatus.RUNNING):
                    return
                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.DISCOVERY_BUILD,
                    job=run,
                    stage_code=stage_code,
                    progress_percent=progress_percent,
                    force_event=True,
                )
        except Exception:
            logger.warning(
                "Failed to checkpoint discovery build progress run_id=%s stage_code=%s",
                run_id,
                stage_code,
                exc_info=True,
            )

    def build_for_document(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        commit: bool = True,
        stage_callback: Callable[[str, int], None] | None = None,
    ) -> DiscoveryBuildSummary:
        document = self._get_document(session, user_id=user_id, document_id=document_id)
        if document is None:
            raise ValueError("Document not found.")

        if stage_callback is not None:
            stage_callback("grouping_forms", 20)
        buckets = self._load_form_buckets(session, document_id=document.id)
        normalized_forms = list(buckets.keys())
        if not normalized_forms:
            self._delete_stale_candidates(session, user_id=user_id, document_id=document.id, keep_forms=[])
            if commit:
                session.commit()
            return DiscoveryBuildSummary()

        if stage_callback is not None:
            stage_callback("collecting_evidence", 45)
        morphology_map = self._load_morphology_map(
            session,
            user_id=user_id,
            document_id=document.id,
            normalized_forms=normalized_forms,
        )
        lexeme_resolutions = self.lexeme_resolver.resolve_many(
            session,
            user_id=user_id,
            forms=normalized_forms,
            morphological_analyses_by_form={form: summary.analyses for form, summary in morphology_map.items()},
            language_profile=self._document_language_profile(document.language_stage),
        )
        dictionary_lemma_forms = self._collect_structured_dictionary_lemmas(lexeme_resolutions)
        lookup_forms = list(dict.fromkeys([*normalized_forms, *dictionary_lemma_forms]))
        lexeme_map = self._load_lexeme_map(session, user_id=user_id, normalized_forms=lookup_forms)
        reference_map = self._load_reference_map(session, user_id=user_id, normalized_forms=lookup_forms)
        corpus_map = self._load_corpus_evidence_map(normalized_forms=lookup_forms)
        external_map = self._load_cached_external_map(session, normalized_forms=normalized_forms)
        ner_map = self._load_ner_map(session, normalized_forms=normalized_forms)
        known_lemmas = self._load_known_forms(
            session,
            user_id=user_id,
            normalized_forms=dictionary_lemma_forms,
        )

        if stage_callback is not None:
            stage_callback("resolving_candidates", 70)
        summary_counts: Counter[str] = Counter()
        existing_by_form = {
            candidate.normalized_form: candidate
            for candidate in session.scalars(
                select(DiscoveryCandidate).where(
                    DiscoveryCandidate.user_id == user_id,
                    DiscoveryCandidate.document_id == document.id,
                    DiscoveryCandidate.normalized_form.in_(normalized_forms),
                )
            )
        }

        for normalized_form, bucket in buckets.items():
            morphology_summary = morphology_map.get(normalized_form, MorphologySummary(Counter(), Counter()))
            lexeme_resolution = lexeme_resolutions.get(normalized_form) or self.lexeme_resolver.resolve(
                session,
                user_id=user_id,
                surface_form=normalized_form,
                normalized_form=normalized_form,
                morphological_analyses=morphology_summary.analyses,
                language_profile=self._document_language_profile(document.language_stage),
            )
            evidence = self._collect_evidence(
                normalized_form,
                lexeme_map=lexeme_map,
                reference_map=reference_map,
                corpus_map=corpus_map,
                external_map=external_map,
                ner_map=ner_map,
                morphology_summary=morphology_summary,
                lexeme_resolution=lexeme_resolution,
                known_lemmas=known_lemmas,
                has_digits=bucket.has_digits,
                document_language_stage=document.language_stage,
            )
            resolution = self.resolution_engine.resolve(
                ResolutionInput(
                    normalized_form=normalized_form,
                    occurrence_count=bucket.occurrence_count,
                    page_count=bucket.page_count,
                    dominant_script_type=bucket.dominant_script_type,
                    evidence=evidence,
                    linked_lexeme_id=str(lexeme_map[normalized_form]["id"]) if normalized_form in lexeme_map else None,
                    has_armenian=bucket.has_armenian,
                    has_digits=bucket.has_digits,
                    morphology_plausible=morphology_summary.plausible,
                    morphology_lemma_known=bool(set(morphology_summary.lemma_counts) & known_lemmas),
                    language_profile=self._document_language_profile(document.language_stage),
                )
            )
            candidate = existing_by_form.get(normalized_form) or DiscoveryCandidate(
                user_id=user_id,
                document_id=document.id,
                normalized_form=normalized_form,
            )
            canonical_resolution = self.canonical_form_resolver.resolve(normalized_form=normalized_form, evidence=evidence)
            candidate.canonical_form_candidate = (
                lexeme_resolution.selected_dictionary_lemma
                if lexeme_resolution.has_structured_dictionary_lemma
                else canonical_resolution.canonical_form
            )
            candidate.occurrence_count = bucket.occurrence_count
            candidate.page_count = bucket.page_count
            candidate.sample_tokens = bucket.sample_tokens
            candidate.sample_contexts = bucket.sample_contexts
            candidate.sample_pages = bucket.sample_pages
            candidate.resolution_status = resolution.resolution_status
            candidate.candidate_type = resolution.candidate_type
            candidate.interest_score = resolution.interest_score
            candidate.confidence_score = resolution.confidence_score
            candidate.ocr_risk_score = resolution.ocr_risk_score
            candidate.morphology_plausibility_score = resolution.morphology_plausibility_score
            candidate.definition_quality_score = resolution.definition_quality_score
            candidate.best_evidence_summary = {
                **resolution.best_evidence_summary,
                "morphology": self._morphology_payload(morphology_summary),
                "evidence_count": len(evidence),
                "canonical_resolution": {
                    "canonical_source": canonical_resolution.canonical_source,
                    "canonical_confidence": canonical_resolution.canonical_confidence,
                    "candidate_lemmas": canonical_resolution.candidate_lemmas,
                    "reason": canonical_resolution.reason,
                    "conflicting_sources": canonical_resolution.conflicting_sources,
                },
                "lexeme_resolution": self._lexeme_resolution_payload(lexeme_resolution),
            }
            candidate.linked_lexeme_id = lexeme_map.get(normalized_form, {}).get("id")
            session.add(candidate)

            summary_counts[resolution.resolution_status] += 1
            if resolution.suppressed:
                summary_counts["suppressed"] += 1
            else:
                summary_counts["shown_in_queue"] += 1

        if stage_callback is not None:
            stage_callback("saving_candidates", 90)
        self._delete_stale_candidates(session, user_id=user_id, document_id=document.id, keep_forms=normalized_forms)
        if commit:
            session.commit()
        return self._build_summary(total_grouped_forms=len(normalized_forms), counts=summary_counts)

    def refresh_reference_evidence_for_document(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        reference_source_id: UUID | None,
        commit: bool = True,
        stage_callback: Callable[[str, int], None] | None = None,
    ) -> tuple[DiscoveryBuildSummary, int, int]:
        document = self._get_document(session, user_id=user_id, document_id=document_id)
        if document is None:
            raise ValueError("Document not found.")
        if reference_source_id is None:
            raise ValueError("Reference source is required for reference-only refresh.")

        if stage_callback is not None:
            stage_callback("grouping_forms", 20)
        buckets = self._load_form_buckets(session, document_id=document.id)
        normalized_forms = list(buckets.keys())
        if not normalized_forms:
            return DiscoveryBuildSummary(), 0, 0

        if stage_callback is not None:
            stage_callback("collecting_evidence", 45)
        morphology_map = self._load_morphology_map(
            session,
            user_id=user_id,
            document_id=document.id,
            normalized_forms=normalized_forms,
        )
        lexeme_resolutions = self.lexeme_resolver.resolve_many(
            session,
            user_id=user_id,
            forms=normalized_forms,
            morphological_analyses_by_form={form: summary.analyses for form, summary in morphology_map.items()},
            language_profile=self._document_language_profile(document.language_stage),
        )
        dictionary_lemma_forms = self._collect_structured_dictionary_lemmas(lexeme_resolutions)
        lookup_forms = list(dict.fromkeys([*normalized_forms, *dictionary_lemma_forms]))
        reference_map = self._load_reference_map(
            session,
            user_id=user_id,
            normalized_forms=lookup_forms,
            reference_source_id=reference_source_id,
        )
        affected_forms = [
            form
            for form in normalized_forms
            if reference_map.get(form)
            or (
                (resolution := lexeme_resolutions.get(form)) is not None
                and resolution.has_structured_dictionary_lemma
                and resolution.selected_dictionary_lemma_normalized in reference_map
            )
        ]
        if not affected_forms:
            if commit:
                session.commit()
            return DiscoveryBuildSummary(total_grouped_forms=len(normalized_forms)), 0, len(normalized_forms)

        lexeme_map = self._load_lexeme_map(
            session,
            user_id=user_id,
            normalized_forms=list(dict.fromkeys([*affected_forms, *dictionary_lemma_forms])),
        )
        known_lemmas = self._load_known_forms(session, user_id=user_id, normalized_forms=dictionary_lemma_forms)
        existing_by_form = {
            candidate.normalized_form: candidate
            for candidate in session.scalars(
                select(DiscoveryCandidate).where(
                    DiscoveryCandidate.user_id == user_id,
                    DiscoveryCandidate.document_id == document.id,
                    DiscoveryCandidate.normalized_form.in_(affected_forms),
                )
            )
        }

        if stage_callback is not None:
            stage_callback("resolving_candidates", 70)
        summary_counts: Counter[str] = Counter()
        for normalized_form in affected_forms:
            bucket = buckets[normalized_form]
            morphology_summary = morphology_map.get(normalized_form, MorphologySummary(Counter(), Counter()))
            lexeme_resolution = lexeme_resolutions.get(normalized_form) or self.lexeme_resolver.resolve(
                session,
                user_id=user_id,
                surface_form=normalized_form,
                normalized_form=normalized_form,
                morphological_analyses=morphology_summary.analyses,
                language_profile=self._document_language_profile(document.language_stage),
            )
            evidence = self._collect_evidence(
                normalized_form,
                lexeme_map=lexeme_map,
                reference_map=reference_map,
                corpus_map={},
                external_map={},
                ner_map={},
                morphology_summary=morphology_summary,
                lexeme_resolution=lexeme_resolution,
                known_lemmas=known_lemmas,
                has_digits=bucket.has_digits,
            )
            resolution = self.resolution_engine.resolve(
                ResolutionInput(
                    normalized_form=normalized_form,
                    occurrence_count=bucket.occurrence_count,
                    page_count=bucket.page_count,
                    dominant_script_type=bucket.dominant_script_type,
                    evidence=evidence,
                    linked_lexeme_id=str(lexeme_map[normalized_form]["id"]) if normalized_form in lexeme_map else None,
                    has_armenian=bucket.has_armenian,
                    has_digits=bucket.has_digits,
                    morphology_plausible=morphology_summary.plausible,
                    morphology_lemma_known=bool(set(morphology_summary.lemma_counts) & known_lemmas),
                    language_profile=self._document_language_profile(document.language_stage),
                )
            )
            candidate = existing_by_form.get(normalized_form) or DiscoveryCandidate(
                user_id=user_id,
                document_id=document.id,
                normalized_form=normalized_form,
            )
            canonical_resolution = self.canonical_form_resolver.resolve(normalized_form=normalized_form, evidence=evidence)
            candidate.canonical_form_candidate = (
                lexeme_resolution.selected_dictionary_lemma
                if lexeme_resolution.has_structured_dictionary_lemma
                else canonical_resolution.canonical_form
            )
            candidate.occurrence_count = bucket.occurrence_count
            candidate.page_count = bucket.page_count
            candidate.sample_tokens = bucket.sample_tokens
            candidate.sample_contexts = bucket.sample_contexts
            candidate.sample_pages = bucket.sample_pages
            candidate.resolution_status = resolution.resolution_status
            candidate.candidate_type = resolution.candidate_type
            candidate.interest_score = resolution.interest_score
            candidate.confidence_score = resolution.confidence_score
            candidate.ocr_risk_score = resolution.ocr_risk_score
            candidate.morphology_plausibility_score = resolution.morphology_plausibility_score
            candidate.definition_quality_score = resolution.definition_quality_score
            candidate.best_evidence_summary = {
                **resolution.best_evidence_summary,
                "morphology": self._morphology_payload(morphology_summary),
                "evidence_count": len(evidence),
                "reference_refresh": {
                    "reference_source_id": str(reference_source_id),
                    "matched_reference_only": True,
                },
                "canonical_resolution": {
                    "canonical_source": canonical_resolution.canonical_source,
                    "canonical_confidence": canonical_resolution.canonical_confidence,
                    "candidate_lemmas": canonical_resolution.candidate_lemmas,
                    "reason": canonical_resolution.reason,
                    "conflicting_sources": canonical_resolution.conflicting_sources,
                },
                "lexeme_resolution": self._lexeme_resolution_payload(lexeme_resolution),
            }
            candidate.linked_lexeme_id = lexeme_map.get(normalized_form, {}).get("id")
            session.add(candidate)
            summary_counts[resolution.resolution_status] += 1
            if resolution.suppressed:
                summary_counts["suppressed"] += 1
            else:
                summary_counts["shown_in_queue"] += 1

        if stage_callback is not None:
            stage_callback("saving_candidates", 90)
        if commit:
            session.commit()
        return (
            self._build_summary(total_grouped_forms=len(affected_forms), counts=summary_counts),
            len(affected_forms),
            max(len(normalized_forms) - len(affected_forms), 0),
        )

    def list_candidates(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        search: str | None = None,
        candidate_type: str | None = None,
        resolution_status: str | None = None,
        review_status: str | None = None,
        min_interest_score: float | None = None,
        include_suppressed: bool = False,
        limit: int,
        offset: int,
        sort: str = "occurrence_count_asc",
    ) -> tuple[list[DiscoveryCandidate], int]:
        filters = [
            DiscoveryCandidate.user_id == user_id,
            DiscoveryCandidate.document_id == document_id,
        ]
        if search:
            filters.append(DiscoveryCandidate.normalized_form.ilike(f"%{search.strip()}%"))
        if candidate_type:
            filters.append(DiscoveryCandidate.candidate_type == candidate_type)
        if resolution_status:
            filters.append(DiscoveryCandidate.resolution_status == resolution_status)
        if review_status:
            filters.append(DiscoveryCandidate.review_status == review_status)
        if min_interest_score is not None:
            filters.append(DiscoveryCandidate.interest_score >= min_interest_score)
        if not include_suppressed:
            filters.extend(
                [
                    DiscoveryCandidate.resolution_status.not_in(DEFAULT_HIDDEN_RESOLUTION_STATUSES),
                    DiscoveryCandidate.candidate_type.not_in(DEFAULT_HIDDEN_CANDIDATE_TYPES),
                    self._has_armenian_occurrence_filter(),
                    self._has_no_foreign_script_occurrence_filter(),
                    self._exclude_high_frequency_plausible_filter(),
                ]
            )

        total = session.scalar(select(func.count(DiscoveryCandidate.id)).where(*filters)) or 0
        sort_options = {
            "normalized_form_asc": DiscoveryCandidate.normalized_form.asc(),
            "normalized_form_desc": DiscoveryCandidate.normalized_form.desc(),
            "occurrence_count_asc": DiscoveryCandidate.occurrence_count.asc(),
            "occurrence_count_desc": DiscoveryCandidate.occurrence_count.desc(),
            "page_count_asc": DiscoveryCandidate.page_count.asc(),
            "page_count_desc": DiscoveryCandidate.page_count.desc(),
            "candidate_type_asc": DiscoveryCandidate.candidate_type.asc(),
            "candidate_type_desc": DiscoveryCandidate.candidate_type.desc(),
            "resolution_status_asc": DiscoveryCandidate.resolution_status.asc(),
            "resolution_status_desc": DiscoveryCandidate.resolution_status.desc(),
            "ocr_risk_score_asc": DiscoveryCandidate.ocr_risk_score.asc(),
            "ocr_risk_score_desc": DiscoveryCandidate.ocr_risk_score.desc(),
            "interest_score_asc": DiscoveryCandidate.interest_score.asc(),
            "interest_score_desc": DiscoveryCandidate.interest_score.desc(),
            "review_status_asc": DiscoveryCandidate.review_status.asc(),
            "review_status_desc": DiscoveryCandidate.review_status.desc(),
        }
        order_by = sort_options.get(sort, DiscoveryCandidate.occurrence_count.asc())
        items = list(
            session.scalars(
                select(DiscoveryCandidate)
                .where(*filters)
                .order_by(order_by, DiscoveryCandidate.normalized_form.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        return items, total

    @staticmethod
    def _has_armenian_occurrence_filter():
        return (
            select(Occurrence.id)
            .where(
                Occurrence.document_id == DiscoveryCandidate.document_id,
                Occurrence.normalized_token == DiscoveryCandidate.normalized_form,
                Occurrence.has_armenian.is_(True),
            )
            .correlate(DiscoveryCandidate)
            .exists()
        )

    @staticmethod
    def _has_no_foreign_script_occurrence_filter():
        return ~(
            select(Occurrence.id)
            .where(
                Occurrence.document_id == DiscoveryCandidate.document_id,
                Occurrence.normalized_token == DiscoveryCandidate.normalized_form,
                Occurrence.script_type.in_(
                    (
                        OccurrenceScriptType.LATIN,
                        OccurrenceScriptType.MIXED,
                        OccurrenceScriptType.OTHER,
                    )
                ),
            )
            .correlate(DiscoveryCandidate)
            .exists()
        )

    @staticmethod
    def _exclude_high_frequency_plausible_filter():
        return or_(
            DiscoveryCandidate.resolution_status != "unknown_plausible",
            and_(
                DiscoveryCandidate.occurrence_count < HIGH_FREQUENCY_PLAUSIBLE_MIN_OCCURRENCES,
                DiscoveryCandidate.page_count < HIGH_FREQUENCY_PLAUSIBLE_MIN_PAGES,
            ),
        )

    def get_summary(self, session: Session, *, user_id: UUID, document_id: UUID) -> DiscoverySummaryResponse:
        base_filters = [
            DiscoveryCandidate.user_id == user_id,
            DiscoveryCandidate.document_id == document_id,
        ]
        visible_filter = and_(
            DiscoveryCandidate.resolution_status.not_in(DEFAULT_HIDDEN_RESOLUTION_STATUSES),
            DiscoveryCandidate.candidate_type.not_in(DEFAULT_HIDDEN_CANDIDATE_TYPES),
            self._has_armenian_occurrence_filter(),
            self._has_no_foreign_script_occurrence_filter(),
            self._exclude_high_frequency_plausible_filter(),
        )
        counts_row = session.execute(
            select(
                func.count(DiscoveryCandidate.id).label("total_candidates"),
                func.count(DiscoveryCandidate.id).filter(visible_filter).label("visible_candidates"),
                func.count(DiscoveryCandidate.id)
                .filter(DiscoveryCandidate.review_status == "reviewed")
                .label("reviewed_candidates"),
            ).where(*base_filters)
        ).one()
        total_candidates = int(counts_row.total_candidates or 0)
        visible_candidates = int(counts_row.visible_candidates or 0)
        reviewed_candidates = int(counts_row.reviewed_candidates or 0)
        by_candidate_type = self._count_by_column(
            session,
            filters=base_filters,
            column=DiscoveryCandidate.candidate_type,
        )
        by_resolution_status = self._count_by_column(
            session,
            filters=base_filters,
            column=DiscoveryCandidate.resolution_status,
        )
        by_review_status = self._count_by_column(
            session,
            filters=base_filters,
            column=DiscoveryCandidate.review_status,
        )
        latest_build = session.scalar(
            select(DiscoveryBuildRun)
            .where(
                DiscoveryBuildRun.user_id == str(user_id),
                DiscoveryBuildRun.document_id == document_id,
            )
            .order_by(DiscoveryBuildRun.created_at.desc(), DiscoveryBuildRun.id.desc())
            .limit(1)
        )
        return DiscoverySummaryResponse(
            total_candidates=total_candidates,
            visible_candidates=visible_candidates,
            suppressed_candidates=max(total_candidates - visible_candidates, 0),
            reviewed_candidates=reviewed_candidates,
            unreviewed_candidates=max(total_candidates - reviewed_candidates, 0),
            by_candidate_type=by_candidate_type,
            by_resolution_status=by_resolution_status,
            by_review_status=by_review_status,
            latest_build=DiscoveryBuildRunRead.model_validate(latest_build) if latest_build is not None else None,
            reference_evidence_states=self._document_reference_evidence_states(
                session,
                user_id=user_id,
                document_id=document_id,
            ),
        )

    def get_candidate(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        candidate_id: UUID,
    ) -> DiscoveryCandidate | None:
        return session.scalar(
            select(DiscoveryCandidate).where(
                DiscoveryCandidate.id == candidate_id,
                DiscoveryCandidate.user_id == user_id,
                DiscoveryCandidate.document_id == document_id,
            )
        )

    def get_candidate_detail(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        candidate_id: UUID,
    ) -> tuple[DiscoveryCandidate, list[DiscoveryEvidenceItem], list[DiscoveryOccurrenceEvidence], dict[str, object]] | None:
        candidate = self.get_candidate(
            session,
            user_id=user_id,
            document_id=document_id,
            candidate_id=candidate_id,
        )
        if candidate is None:
            return None
        document = self._get_document(session, user_id=user_id, document_id=document_id)
        if document is None:
            return None
        normalized_form = candidate.normalized_form
        morphology_map = self._load_morphology_map(
            session,
            user_id=user_id,
            document_id=document_id,
            normalized_forms=[normalized_form],
        )
        morphology_summary = morphology_map.get(normalized_form, MorphologySummary(Counter(), Counter()))
        lexeme_resolution = self.lexeme_resolver.resolve(
            session,
            user_id=user_id,
            surface_form=normalized_form,
            normalized_form=normalized_form,
            morphological_analyses=morphology_summary.analyses,
            language_profile=self._document_language_profile(document.language_stage),
        )
        dictionary_lemma_forms = self._collect_structured_dictionary_lemmas({normalized_form: lexeme_resolution})
        lookup_forms = list(dict.fromkeys([normalized_form, *dictionary_lemma_forms]))
        lexeme_map = self._load_lexeme_map(session, user_id=user_id, normalized_forms=lookup_forms)
        reference_map = self._load_reference_map(session, user_id=user_id, normalized_forms=lookup_forms)
        corpus_map = self._load_corpus_evidence_map(normalized_forms=lookup_forms)
        external_map = self._load_cached_external_map(session, normalized_forms=[normalized_form])
        ner_map = self._load_ner_map(session, normalized_forms=[normalized_form])
        known_lemmas = self._load_known_forms(
            session,
            user_id=user_id,
            normalized_forms=dictionary_lemma_forms,
        )
        token_classification = classify_token(normalized_form)
        provider_evidence = [
            DiscoveryEvidenceItem(
                provider_key=evidence.provider_key,
                provider_type=evidence.provider_type,
                evidence_role=evidence.evidence_role,
                role=evidence.evidence_role or self._provider_role(evidence.provider_type),
                query_form=evidence.query_form,
                matched_form=evidence.matched_form,
                result_headword=evidence.result_headword,
                lemma=evidence.lemma,
                match_type=evidence.match_type,
                validation_strength=evidence.validation_strength,
                evidence_strength=evidence.evidence_strength,
                definition_quality=evidence.definition_quality,
                language_profile=evidence.language_profile,
                priority=evidence.priority,
                can_validate_word=evidence.can_validate_word,
                can_attest_usage=evidence.can_attest_usage,
                can_suggest_lemma=evidence.can_suggest_lemma,
                can_suggest_named_entity=evidence.can_suggest_named_entity,
                requires_exact_match=evidence.requires_exact_match,
                requires_structured_headword=evidence.requires_structured_headword,
                default_runtime=evidence.default_runtime,
                independent_source_group=evidence.independent_source_group,
                source_kind=evidence.source_kind,
                confidence=evidence.confidence if evidence.confidence is not None else evidence.confidence_score,
                is_exact_match=evidence.is_exact_match,
                is_substring_match=evidence.is_substring_match,
                is_fuzzy_match=evidence.is_fuzzy_match,
                is_canonical_match=evidence.is_canonical_match,
                citation=evidence.citation,
                payload=evidence.payload,
            )
            for evidence in self._collect_evidence(
                normalized_form,
                lexeme_map=lexeme_map,
                reference_map=reference_map,
                corpus_map=corpus_map,
                external_map=external_map,
                ner_map=ner_map,
                morphology_summary=morphology_summary,
                lexeme_resolution=lexeme_resolution,
                known_lemmas=known_lemmas,
                has_digits=token_classification.has_digits,
                document_language_stage=document.language_stage,
            )
        ]
        occurrence_evidence = self._load_occurrence_evidence(
            session,
            document_id=document_id,
            normalized_form=normalized_form,
        )
        decision = {
            "review_status": candidate.review_status,
            "reviewer_decision": candidate.reviewer_decision,
            "reviewer_note": candidate.reviewer_note,
            "linked_lexeme_id": str(candidate.linked_lexeme_id) if candidate.linked_lexeme_id else None,
        }
        return candidate, provider_evidence, occurrence_evidence, decision

    def record_decision(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        candidate_id: UUID,
        decision: str,
        note: str | None = None,
        linked_lexeme_id: UUID | None = None,
        create_lexeme_canonical_form: str | None = None,
        create_lexeme_definition: str | None = None,
    ) -> DiscoveryCandidate:
        if decision not in VALID_DECISIONS:
            raise ValueError(f"Unsupported decision: {decision}")
        candidate = self.get_candidate(
            session,
            user_id=user_id,
            document_id=document_id,
            candidate_id=candidate_id,
        )
        if candidate is None:
            raise ValueError("Discovery candidate not found.")

        resolved_linked_lexeme_id = linked_lexeme_id
        if decision == "link_lexeme":
            if linked_lexeme_id is None:
                raise ValueError("linked_lexeme_id is required for link_lexeme.")
            resolved_linked_lexeme_id = self._validate_linked_lexeme(
                session,
                user_id=user_id,
                linked_lexeme_id=linked_lexeme_id,
            )
        elif linked_lexeme_id is not None:
            resolved_linked_lexeme_id = self._validate_linked_lexeme(
                session,
                user_id=user_id,
                linked_lexeme_id=linked_lexeme_id,
            )

        if decision == "create_lexeme":
            resolved_linked_lexeme_id = self._create_or_reuse_lexeme_for_candidate(
                session,
                user_id=user_id,
                candidate=candidate,
                canonical_form=create_lexeme_canonical_form,
                definition=create_lexeme_definition,
            )

        candidate.review_status = "reviewed"
        candidate.reviewer_decision = decision
        candidate.reviewer_note = note
        if resolved_linked_lexeme_id is not None:
            candidate.linked_lexeme_id = resolved_linked_lexeme_id
        session.commit()
        session.refresh(candidate)
        return candidate

    @staticmethod
    def _validate_linked_lexeme(
        session: Session,
        *,
        user_id: UUID,
        linked_lexeme_id: UUID,
    ) -> UUID:
        lexeme = session.scalar(
            select(Lexeme).where(
                Lexeme.id == linked_lexeme_id,
                Lexeme.user_id == str(user_id),
            )
        )
        if lexeme is None:
            raise ValueError("Linked lexeme was not found.")
        return lexeme.id

    def _create_or_reuse_lexeme_for_candidate(
        self,
        session: Session,
        *,
        user_id: UUID,
        candidate: DiscoveryCandidate,
        canonical_form: str | None = None,
        definition: str | None = None,
    ) -> UUID:
        resolved_canonical_form = (canonical_form or "").strip() or candidate.canonical_form_candidate or candidate.normalized_form
        cleaned_definition = (definition or "").strip()
        request = LexemeCreateRequest(
            canonical_form=resolved_canonical_form,
            normalized_forms=[candidate.normalized_form],
            notes=cleaned_definition or None,
            status=LexemeStatus.DRAFT,
        )
        try:
            detail = get_lexeme_service().create_lexeme(
                session,
                user_id=user_id,
                request=request,
            )
            return detail.id
        except LexemeConflictError as exc:
            if len(exc.conflicting_lexeme_ids) == 1:
                return self._validate_linked_lexeme(
                    session,
                    user_id=user_id,
                    linked_lexeme_id=exc.conflicting_lexeme_ids[0],
                )
            raise ValueError(exc.message) from exc

    @staticmethod
    def _get_document(session: Session, *, user_id: UUID, document_id: UUID) -> Document | None:
        return session.scalar(select(Document).where(Document.id == document_id, Document.user_id == user_id))

    @staticmethod
    def _count_by_column(session: Session, *, filters: list[object], column) -> dict[str, int]:  # noqa: ANN001
        rows = session.execute(
            select(column, func.count(DiscoveryCandidate.id))
            .where(*filters)
            .group_by(column)
            .order_by(column.asc())
        ).all()
        return {str(key): int(count) for key, count in rows if key is not None}

    @staticmethod
    def _load_run(session: Session, run_id: UUID) -> DiscoveryBuildRun:
        run = session.get(DiscoveryBuildRun, run_id)
        if run is None:
            raise ValueError(f"Discovery build run {run_id} was not found.")
        return run

    def _select_reference_state_for_refresh(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        reference_source_id: UUID | None,
    ) -> DocumentReferenceEvidenceState | None:
        states = self._document_reference_evidence_states(session, user_id=user_id, document_id=document_id)
        candidates = [
            state
            for state in states
            if reference_source_id is None or state.reference_source_id == reference_source_id
        ]
        if not candidates:
            return None
        priority = {"failed": 0, "never_checked": 1, "stale": 2, "up_to_date": 3}
        return sorted(candidates, key=lambda state: priority.get(state.status, 99))[0]

    @staticmethod
    def _document_reference_evidence_states(
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
    ) -> list[DocumentReferenceEvidenceState]:
        imports = session.scalars(
            select(ReferenceSourceImport)
            .join(ReferenceSource, ReferenceSourceImport.source_id == ReferenceSource.id)
            .where(
                ReferenceSource.user_id == str(user_id),
                ReferenceSource.is_active.is_(True),
                ReferenceSourceImport.status == "completed",
            )
            .order_by(ReferenceSource.display_name.asc(), ReferenceSourceImport.finished_at.desc().nullslast())
        ).all()
        latest_by_source: dict[UUID, ReferenceSourceImport] = {}
        for import_run in imports:
            latest_by_source.setdefault(import_run.source_id, import_run)

        states: list[DocumentReferenceEvidenceState] = []
        for import_run in latest_by_source.values():
            latest_check = session.scalar(
                select(DiscoveryBuildRun)
                .where(
                    DiscoveryBuildRun.user_id == str(user_id),
                    DiscoveryBuildRun.document_id == document_id,
                    DiscoveryBuildRun.reference_source_id == import_run.source_id,
                    DiscoveryBuildRun.reference_source_import_id == import_run.id,
                    DiscoveryBuildRun.build_mode == "reference_only",
                )
                .order_by(DiscoveryBuildRun.created_at.desc(), DiscoveryBuildRun.id.desc())
                .limit(1)
            )
            latest_full_check = session.scalar(
                select(DiscoveryBuildRun)
                .where(
                    DiscoveryBuildRun.user_id == str(user_id),
                    DiscoveryBuildRun.document_id == document_id,
                    DiscoveryBuildRun.build_mode == "full",
                    DiscoveryBuildRun.status == MorphologyRunStatus.COMPLETED,
                    DiscoveryBuildRun.finished_at.is_not(None),
                )
                .order_by(DiscoveryBuildRun.finished_at.desc(), DiscoveryBuildRun.id.desc())
                .limit(1)
            )
            full_check_is_current = bool(
                latest_full_check
                and import_run.finished_at
                and latest_full_check.finished_at
                and latest_full_check.finished_at >= import_run.finished_at
            )
            prior_check = None
            if latest_check is None and full_check_is_current:
                status = "up_to_date"
            elif latest_check is None:
                prior_check = session.scalar(
                    select(DiscoveryBuildRun)
                    .where(
                        DiscoveryBuildRun.user_id == str(user_id),
                        DiscoveryBuildRun.document_id == document_id,
                        DiscoveryBuildRun.reference_source_id == import_run.source_id,
                        DiscoveryBuildRun.build_mode == "reference_only",
                    )
                    .order_by(DiscoveryBuildRun.created_at.desc(), DiscoveryBuildRun.id.desc())
                    .limit(1)
                )
                status = "stale" if prior_check is not None else "never_checked"
            elif latest_check.status == MorphologyRunStatus.FAILED:
                status = "failed"
            elif latest_check.status == MorphologyRunStatus.COMPLETED:
                status = "up_to_date"
            else:
                status = "stale"
            display_check = latest_check or (latest_full_check if full_check_is_current else None) or prior_check
            states.append(
                DocumentReferenceEvidenceState(
                    document_id=document_id,
                    reference_source_id=import_run.source_id,
                    reference_source_import_id=import_run.id,
                    source_display_name=import_run.source.display_name if import_run.source else "Reference dataset",
                    status=status,
                    last_checked_at=display_check.finished_at if display_check else None,
                    matched_count=display_check.matched_count if display_check else 0,
                    unmatched_count=display_check.unmatched_count if display_check else 0,
                    error=(
                        (display_check.error_message_user or display_check.error_message)
                        if display_check
                        else None
                    ),
                )
            )
        return states

    @staticmethod
    def _load_form_buckets(session: Session, *, document_id: UUID) -> dict[str, FormBucket]:
        buckets: dict[str, FormBucket] = {}
        occurrences = session.scalars(
            select(Occurrence)
            .where(Occurrence.document_id == document_id)
            .order_by(Occurrence.normalized_token.asc(), Occurrence.page_number.asc(), Occurrence.char_start.asc().nullsfirst())
        )
        for occurrence in occurrences:
            normalized_form = occurrence.normalized_token.strip()
            if not normalized_form:
                continue
            bucket = buckets.setdefault(normalized_form, FormBucket(normalized_form=normalized_form))
            bucket.add(occurrence)
        return buckets

    @staticmethod
    def _load_lexeme_map(
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: list[str],
    ) -> dict[str, dict[str, object]]:
        rows = session.execute(
            select(LexemeForm.normalized_form, Lexeme.id, Lexeme.canonical_form)
            .join(Lexeme, LexemeForm.lexeme_id == Lexeme.id)
            .where(
                LexemeForm.user_id == str(user_id),
                LexemeForm.normalized_form.in_(normalized_forms),
            )
        ).all()
        return {
            row.normalized_form: {
                "id": row.id,
                "canonical_form": row.canonical_form,
            }
            for row in rows
        }

    @staticmethod
    def _load_reference_map(
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: list[str],
        reference_source_id: UUID | None = None,
    ) -> dict[str, list[ReferenceEntry]]:
        filters = [
            ReferenceSource.user_id == str(user_id),
            ReferenceSource.is_active.is_(True),
            ReferenceEntry.normalized_form.in_(normalized_forms),
        ]
        if reference_source_id is not None:
            filters.append(ReferenceSource.id == reference_source_id)
        rows = session.scalars(
            select(ReferenceEntry)
            .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
            .where(*filters)
            .order_by(ReferenceEntry.normalized_form.asc(), ReferenceEntry.surface_form.asc())
        ).all()
        grouped: dict[str, list[ReferenceEntry]] = defaultdict(list)
        for row in rows:
            grouped[row.normalized_form].append(row)
        return dict(grouped)

    @staticmethod
    def _load_cached_external_map(
        session: Session,
        *,
        normalized_forms: list[str],
    ) -> dict[str, list[ExternalLookupResult]]:
        if not normalized_forms:
            return {}
        latest_cache_subquery = (
            select(
                ExternalLookupCache.normalized_query.label("normalized_query"),
                func.max(ExternalLookupCache.created_at).label("latest_created_at"),
            )
            .where(
                ExternalLookupCache.status == ExternalLookupStatus.COMPLETED,
                ExternalLookupCache.search_mode == ExternalLookupSearchMode.NORMALIZED,
                ExternalLookupCache.normalized_query.in_(normalized_forms),
            )
            .group_by(ExternalLookupCache.normalized_query)
            .subquery()
        )
        rows = session.scalars(
            select(ExternalLookupResult)
            .join(ExternalLookupCache, ExternalLookupResult.cache_id == ExternalLookupCache.id)
            .join(ExternalProvider, ExternalLookupResult.provider_id == ExternalProvider.id)
            .join(
                latest_cache_subquery,
                and_(
                    ExternalLookupCache.normalized_query == latest_cache_subquery.c.normalized_query,
                    ExternalLookupCache.created_at == latest_cache_subquery.c.latest_created_at,
                ),
            )
            .where(
                ExternalProvider.is_active.is_(True),
                ExternalLookupCache.status == ExternalLookupStatus.COMPLETED,
                ExternalLookupCache.search_mode == ExternalLookupSearchMode.NORMALIZED,
                ExternalLookupCache.normalized_query.in_(normalized_forms),
            )
            .order_by(ExternalLookupCache.normalized_query.asc(), ExternalLookupResult.created_at.asc())
        ).all()
        grouped: dict[str, list[ExternalLookupResult]] = defaultdict(list)
        for row in rows:
            normalized = row.normalized_form or row.cache.normalized_query or ""
            if normalized:
                grouped[normalized].append(row)
        return dict(grouped)

    def _load_morphology_map(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        normalized_forms: list[str],
    ) -> dict[str, MorphologySummary]:
        rows = session.scalars(
            select(MorphologyAnalysis).where(
                MorphologyAnalysis.user_id == str(user_id),
                MorphologyAnalysis.document_id == document_id,
                MorphologyAnalysis.token_normalized.in_(normalized_forms),
                MorphologyAnalysis.analysis_status == MorphologyAnalysisStatus.COMPLETED,
            )
        ).all()
        lemma_counts: dict[str, Counter[str]] = defaultdict(Counter)
        pos_counts: dict[str, Counter[str]] = defaultdict(Counter)
        provider_counts: dict[str, Counter[str]] = defaultdict(Counter)
        feature_counts: dict[str, Counter[str]] = defaultdict(Counter)
        analyses: dict[str, list[object]] = defaultdict(list)
        for row in rows:
            provider_key = self._morphology_provider_key(row.analyzer_model_key)
            if row.lemma_normalized:
                lemma_counts[row.token_normalized][row.lemma_normalized] += 1
            if row.pos:
                pos_counts[row.token_normalized][row.pos] += 1
            provider_counts[row.token_normalized][provider_key] += 1
            analyses[row.token_normalized].append(
                analyzer_result_from_morphology_row(
                    row,
                    source_key=provider_key,
                    language_profile=self._provider_language_profile(provider_key),
                )
            )
            for key, value in (row.morph_features or {}).items():
                feature_counts[row.token_normalized][f"{key}={value}"] += 1
        forms = set(lemma_counts) | set(pos_counts) | set(provider_counts) | set(feature_counts) | set(analyses)
        return {
            form: MorphologySummary(
                lemma_counts[form],
                pos_counts[form],
                provider_counts[form],
                feature_counts[form],
                analyses[form],
            )
            for form in forms
        }

    @staticmethod
    def _load_ner_map(
        session: Session,
        *,
        normalized_forms: list[str],
    ) -> dict[str, list[NerEntityEntry]]:
        if not normalized_forms:
            return {}
        rows = session.scalars(
            select(NerEntityEntry)
            .join(NerSource, NerEntityEntry.source_id == NerSource.id)
            .where(
                NerSource.provider_key == "pioner_ner",
                NerSource.is_active.is_(True),
                NerEntityEntry.normalized_surface.in_(normalized_forms),
            )
            .order_by(
                NerEntityEntry.normalized_surface.asc(),
                NerSource.source_kind.asc(),
                NerEntityEntry.occurrence_count.desc(),
                NerEntityEntry.entity_surface.asc(),
            )
        ).all()
        grouped: dict[str, list[NerEntityEntry]] = defaultdict(list)
        for row in rows:
            grouped[row.normalized_surface].append(row)
        return dict(grouped)

    @staticmethod
    def _ner_confidence(entry: NerEntityEntry, *, document_language_stage: str | None = None) -> float:
        base = float(entry.confidence) if entry.confidence is not None else 0.55
        stage = (document_language_stage or "").strip().lower()
        if stage in {"classical", "grabar", "old_armenian", "xcl"}:
            base -= 0.2
        return max(0.1, min(0.95, base))

    @staticmethod
    def _collect_morphology_lemmas(morphology_map: dict[str, MorphologySummary]) -> list[str]:
        lemmas: list[str] = []
        for summary in morphology_map.values():
            lemmas.extend(summary.lemma_counts.keys())
        return list(dict.fromkeys(lemmas))

    @staticmethod
    def _collect_structured_dictionary_lemmas(lexeme_resolutions: dict[str, LexemeResolution]) -> list[str]:
        lemmas: list[str] = []
        for resolution in lexeme_resolutions.values():
            if resolution.has_structured_dictionary_lemma and resolution.selected_dictionary_lemma_normalized:
                lemmas.append(resolution.selected_dictionary_lemma_normalized)
        return list(dict.fromkeys(lemmas))

    def _load_known_forms(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: list[str],
    ) -> set[str]:
        if not normalized_forms:
            return set()
        lexeme_forms = set(
            session.scalars(
                select(LexemeForm.normalized_form).where(
                    LexemeForm.user_id == str(user_id),
                    LexemeForm.normalized_form.in_(normalized_forms),
                )
            )
        )
        reference_forms = set(
            session.scalars(
                select(ReferenceEntry.normalized_form)
                .join(ReferenceSource, ReferenceEntry.source_id == ReferenceSource.id)
                .where(
                    ReferenceSource.user_id == str(user_id),
                    ReferenceSource.is_active.is_(True),
                    ReferenceEntry.normalized_form.in_(normalized_forms),
                )
            )
        )
        return lexeme_forms | reference_forms

    def _collect_evidence(
        self,
        normalized_form: str,
        *,
        lexeme_map: dict[str, dict[str, object]],
        reference_map: dict[str, list[ReferenceEntry]],
        corpus_map: dict[str, list[EvidenceResult]],
        external_map: dict[str, list[ExternalLookupResult]],
        ner_map: dict[str, list[NerEntityEntry]],
        morphology_summary: MorphologySummary,
        lexeme_resolution: LexemeResolution,
        known_lemmas: set[str],
        has_digits: bool = False,
        document_language_stage: str | None = None,
    ) -> list[EvidenceResult]:
        evidence: list[EvidenceResult] = []
        document_profile = self._document_language_profile(document_language_stage)
        if normalized_form in lexeme_map:
            classification = self.lexical_match_classifier.classify(
                query_form=normalized_form,
                provider_role=EvidenceRole.CURATED_LEXICON.value,
                matched_form=str(lexeme_map[normalized_form]["canonical_form"]),
                result_headword=str(lexeme_map[normalized_form]["canonical_form"]),
                definition_quality="good",
                has_digits=has_digits,
                allow_short_token=True,
            )
            evidence.append(
                EvidenceResult(
                    provider_key="internal_lexicon",
                    provider_type="curated_lexicon",
                    evidence_role="curated_truth",
                    language_profile="mixed",
                    query_form=normalized_form,
                    matched_form=lexeme_map[normalized_form]["canonical_form"],
                    result_headword=str(lexeme_map[normalized_form]["canonical_form"]),
                    match_type=classification.match_type.value,
                    validation_strength=classification.validation_strength.value,
                    evidence_strength=classification.evidence_strength,
                    definition_quality=classification.definition_quality,
                    confidence=classification.confidence_score,
                    is_exact_match=classification.is_exact_match,
                    is_substring_match=classification.is_substring_match,
                    is_fuzzy_match=classification.is_fuzzy_match,
                    is_canonical_match=classification.is_canonical_match,
                    payload=self._classification_payload(classification),
                )
            )
        for entry in reference_map.get(normalized_form, [])[:3]:
            definition_quality = self._reference_definition_quality(entry)
            classification = self.lexical_match_classifier.classify(
                query_form=normalized_form,
                provider_role=EvidenceRole.IMPORTED_REFERENCE.value,
                matched_form=entry.surface_form,
                result_headword=entry.surface_form,
                definition_quality=definition_quality,
                has_digits=has_digits,
                allow_short_token=definition_quality == "good",
            )
            evidence.append(
                EvidenceResult(
                    provider_key="imported_references",
                    provider_type="imported_dictionary",
                    evidence_role="structured_dictionary_headword",
                    language_profile=self._document_language_profile(entry.source.language_stage if entry.source else None) or "mixed",
                    query_form=normalized_form,
                    matched_form=entry.surface_form,
                    result_headword=entry.surface_form,
                    match_type=classification.match_type.value,
                    validation_strength=classification.validation_strength.value,
                    evidence_strength=classification.evidence_strength,
                    definition_quality=classification.definition_quality,
                    confidence=classification.confidence_score,
                    is_exact_match=classification.is_exact_match,
                    is_substring_match=classification.is_substring_match,
                    is_fuzzy_match=classification.is_fuzzy_match,
                    is_canonical_match=classification.is_canonical_match,
                    citation=entry.source.display_name if entry.source else None,
                    payload={
                        "reference_entry_id": str(entry.id),
                        "source_id": str(entry.source_id),
                        "source_language_profile": self._document_language_profile(entry.source.language_stage if entry.source else None),
                        **self._classification_payload(classification),
                    },
                )
            )
        dictionary_lemma = (
            lexeme_resolution.selected_dictionary_lemma_normalized
            if lexeme_resolution.has_structured_dictionary_lemma
            else None
        )
        if lexeme_resolution.conflict_status == "conflict":
            for candidate in lexeme_resolution.dictionary_lemma_candidates[:3]:
                evidence.append(
                    EvidenceResult(
                        provider_key=candidate.source_key,
                        provider_type="reference",
                        evidence_role="structured_lexeme_mapping",
                        query_form=normalized_form,
                        matched_form=candidate.lemma,
                        lemma=candidate.lemma,
                        match_type="exact_lemma_match",
                        validation_strength=ValidationStrength.SUPPORTS_WORD.value,
                        evidence_strength="medium",
                        definition_quality="unknown",
                        confidence=candidate.confidence,
                        is_canonical_match=True,
                        payload={
                            **candidate.raw_payload,
                            "classification_reason": "lexeme resolver found conflicting dictionary lemma mappings",
                        },
                    )
                )
        if dictionary_lemma and dictionary_lemma != normalized_form and dictionary_lemma in lexeme_map:
            classification = self.lexical_match_classifier.classify(
                query_form=dictionary_lemma,
                provider_role=EvidenceRole.CURATED_LEXICON.value,
                matched_form=str(lexeme_map[dictionary_lemma]["canonical_form"]),
                result_headword=str(lexeme_map[dictionary_lemma]["canonical_form"]),
                definition_quality="good",
                has_digits=has_digits,
                allow_short_token=True,
            )
            evidence.append(
                EvidenceResult(
                    provider_key="internal_lexicon",
                    provider_type="curated_lexicon",
                    evidence_role="curated_truth",
                    language_profile="mixed",
                    query_form=normalized_form,
                    matched_form=lexeme_map[dictionary_lemma]["canonical_form"],
                    result_headword=str(lexeme_map[dictionary_lemma]["canonical_form"]),
                    lemma=lexeme_resolution.selected_dictionary_lemma,
                    match_type="exact_lemma_match",
                    validation_strength=ValidationStrength.SUPPORTS_WORD.value,
                    evidence_strength="medium",
                    definition_quality=classification.definition_quality,
                    confidence=min(classification.confidence_score, lexeme_resolution.confidence or 0.8),
                    is_exact_match=True,
                    is_canonical_match=True,
                    payload={
                        "dictionary_lemma_source": lexeme_resolution.selected_source,
                        "classification_reason": "lexeme resolver mapped this form to a curated dictionary lemma",
                    },
                )
            )
        if dictionary_lemma and dictionary_lemma != normalized_form:
            for entry in reference_map.get(dictionary_lemma, [])[:2]:
                definition_quality = self._reference_definition_quality(entry)
                evidence.append(
                    EvidenceResult(
                        provider_key="imported_references",
                        provider_type="imported_dictionary",
                        evidence_role="structured_dictionary_headword",
                        language_profile=self._document_language_profile(entry.source.language_stage if entry.source else None) or "mixed",
                        query_form=normalized_form,
                        matched_form=entry.surface_form,
                        result_headword=entry.surface_form,
                        lemma=lexeme_resolution.selected_dictionary_lemma,
                        match_type="exact_lemma_match",
                        validation_strength=ValidationStrength.SUPPORTS_WORD.value,
                        evidence_strength="medium",
                        definition_quality=definition_quality,
                        confidence=min(lexeme_resolution.confidence or 0.8, 0.8 if definition_quality == "good" else 0.55),
                        is_exact_match=True,
                        is_canonical_match=True,
                        citation=entry.source.display_name if entry.source else None,
                        payload={
                            "reference_entry_id": str(entry.id),
                            "source_id": str(entry.source_id),
                            "dictionary_lemma_source": lexeme_resolution.selected_source,
                            "classification_reason": "lexeme resolver relates this form to an exact imported reference lemma",
                        },
                    )
                )
        evidence.extend(self._profile_adjusted_evidence(corpus_map.get(normalized_form, []), document_profile=document_profile))
        if dictionary_lemma and dictionary_lemma != normalized_form:
            for corpus_item in corpus_map.get(dictionary_lemma, [])[:2]:
                evidence.append(
                    EvidenceResult(
                        provider_key=corpus_item.provider_key,
                        provider_type="corpus",
                        evidence_role=EvidenceRole.CORPUS_ATTESTATION.value,
                        query_form=normalized_form,
                        matched_form=corpus_item.matched_form,
                        lemma=lexeme_resolution.selected_dictionary_lemma,
                        result_headword=corpus_item.result_headword,
                        match_type="corpus_lemma_attestation",
                        validation_strength=ValidationStrength.SUPPORTS_WORD.value,
                        evidence_strength=corpus_item.evidence_strength,
                        definition_quality="unknown",
                        confidence=self._profile_adjusted_confidence(corpus_item.confidence, corpus_item.language_profile, document_profile),
                        is_canonical_match=True,
                        citation=corpus_item.citation,
                        payload={
                            **corpus_item.payload,
                            "dictionary_lemma_source": lexeme_resolution.selected_source,
                            "classification_reason": "lexeme resolver relates this form to a corpus-attested lemma",
                        },
                    )
                )
        for lemma in morphology_summary.lemma_counts:
            classification = self.lexical_match_classifier.classify(
                query_form=normalized_form,
                provider_role=EvidenceRole.MORPHOLOGY_ANALYSIS.value,
                lemma=lemma,
                has_digits=has_digits,
            )
            evidence.append(
                EvidenceResult(
                    provider_key=morphology_summary.provider_key,
                    provider_type="morphology",
                    evidence_role="lemma_pos_features",
                    language_profile=self._provider_language_profile(morphology_summary.provider_key),
                    query_form=normalized_form,
                    lemma=lemma,
                    match_type=classification.match_type.value,
                    validation_strength=classification.validation_strength.value,
                    evidence_strength=classification.evidence_strength,
                    definition_quality=classification.definition_quality,
                    confidence=self._profile_adjusted_confidence(
                        classification.confidence_score,
                        self._provider_language_profile(morphology_summary.provider_key),
                        document_profile,
                    ),
                    is_exact_match=classification.is_exact_match,
                    is_substring_match=classification.is_substring_match,
                    is_fuzzy_match=classification.is_fuzzy_match,
                    is_canonical_match=classification.is_canonical_match,
                    payload={
                        "count": morphology_summary.lemma_counts[lemma],
                        "known_lemma": lemma in known_lemmas,
                        **self._classification_payload(classification),
                    },
                )
            )
        for entry in ner_map.get(normalized_form, [])[:3]:
            confidence = self._ner_confidence(entry, document_language_stage=document_language_stage)
            evidence.append(
                EvidenceResult(
                    provider_key="pioner_ner",
                    provider_type="ner",
                    evidence_role=EvidenceRole.NAMED_ENTITY_SIGNAL.value,
                    language_profile="eastern",
                    query_form=normalized_form,
                    matched_form=entry.entity_surface,
                    match_type="named_entity_signal",
                    validation_strength=ValidationStrength.SUGGESTS_CANDIDATE.value,
                    evidence_strength="medium" if (entry.source.source_kind if entry.source else "") == "gold" else "weak",
                    definition_quality="unknown",
                    confidence=self._profile_adjusted_confidence(confidence, "eastern", document_profile),
                    is_exact_match=True,
                    citation=entry.source.display_name if entry.source else "pioNER",
                    payload={
                        "ner_entry_id": str(entry.id),
                        "source_id": str(entry.source_id),
                        "entity_type": entry.entity_type,
                        "entity_surface": entry.entity_surface,
                        "normalized_surface": entry.normalized_surface,
                        "source_kind": entry.source.source_kind if entry.source else "unknown",
                        "dataset_split": entry.source.dataset_split if entry.source else "unknown",
                        "occurrence_count": entry.occurrence_count,
                        "sample_contexts": entry.sample_contexts,
                        "classification_reason": (
                            "pioNER named-entity surface evidence; not lexical validation"
                        ),
                    },
                )
            )
        for result in external_map.get(normalized_form, [])[:3]:
            provider_key = result.provider.key if result.provider else "external_cache"
            metadata = result.metadata_json or {}
            source_evidence_role = self._resolve_external_source_evidence_role(
                provider_key=provider_key,
                metadata=metadata,
            )
            source_match_type = result.match_type.value if hasattr(result.match_type, "value") else str(result.match_type)
            confidence = float(result.match_score / 100) if result.match_score is not None else None
            definition_quality = "good" if result.snippet else "unknown"
            provider_role = (
                EvidenceRole.WEB_DICTIONARY.value
                if self._is_trusted_dictionary_entry(source_evidence_role=source_evidence_role, metadata=metadata)
                else EvidenceRole.AMBIGUOUS_EXTERNAL.value
            )
            classification = self.lexical_match_classifier.classify(
                query_form=normalized_form,
                provider_role=provider_role,
                matched_form=result.matched_form,
                result_headword=(result.normalized_form or result.matched_form) if source_match_type in {"exact", "normalized"} else None,
                snippet=result.snippet,
                definition_quality=definition_quality,
                source_match_type=source_match_type,
                source_confidence=confidence,
                has_digits=has_digits,
                allow_short_token=source_match_type == "exact" and definition_quality == "good",
            )
            evidence.append(
                EvidenceResult(
                    provider_key=provider_key,
                    provider_type="web_dictionary" if classification.evidence_role is EvidenceRole.WEB_DICTIONARY else "ambiguous_external",
                    evidence_role=(
                        "structured_dictionary_headword"
                        if classification.validation_strength.value == ValidationStrength.VALIDATES_WORD.value
                        else "classified_external_result"
                    ),
                    language_profile="mixed",
                    query_form=normalized_form,
                    matched_form=result.matched_form,
                    result_headword=(result.normalized_form or result.matched_form) if classification.is_exact_match else None,
                    match_type=classification.match_type.value,
                    validation_strength=classification.validation_strength.value,
                    evidence_strength=classification.evidence_strength,
                    definition_quality=classification.definition_quality,
                    confidence=classification.confidence_score,
                    is_exact_match=classification.is_exact_match,
                    is_substring_match=classification.is_substring_match,
                    is_fuzzy_match=classification.is_fuzzy_match,
                    is_canonical_match=classification.is_canonical_match,
                    citation=result.reference_link,
                    payload={
                        "source_title": result.source_title,
                        "snippet": result.snippet,
                        "source_match_type": source_match_type,
                        "source_evidence_role": source_evidence_role,
                        "source_evidence_tier": self._resolve_external_source_evidence_tier(
                            provider_key=provider_key,
                            metadata=metadata,
                        ),
                        "source_evidence_verified": self._resolve_external_source_evidence_verified(
                            provider_key=provider_key,
                            metadata=metadata,
                        ),
                        **self._classification_payload(classification),
                    },
                )
            )
        if not any(item.validation_strength == ValidationStrength.VALIDATES_WORD.value for item in evidence):
            gate = self.lexical_match_classifier.classify(
                query_form=normalized_form,
                provider_role=EvidenceRole.AMBIGUOUS_EXTERNAL.value,
                has_digits=has_digits,
            )
            if gate.validation_strength == ValidationStrength.REJECTS:
                evidence.append(
                    EvidenceResult(
                        provider_key="strict_validation",
                        provider_type="validation",
                        evidence_role=gate.evidence_role.value,
                        query_form=normalized_form,
                        match_type=gate.match_type.value,
                        validation_strength=gate.validation_strength.value,
                        evidence_strength=gate.evidence_strength,
                        definition_quality=gate.definition_quality,
                        confidence=gate.confidence_score,
                        payload=self._classification_payload(gate),
                    )
                )
        return evidence

    def _load_corpus_evidence_map(self, *, normalized_forms: list[str]) -> dict[str, list[EvidenceResult]]:
        if not self.nayiri_corpus_enabled:
            return {}
        matches_by_form = self.nayiri_corpus_service.lookup_many(normalized_forms, limit=3)
        return {
            normalized_form: self._corpus_evidence_from_matches(normalized_form, matches)
            for normalized_form, matches in matches_by_form.items()
        }

    def _corpus_evidence(self, normalized_form: str) -> list[EvidenceResult]:
        try:
            matches = self.nayiri_corpus_service.lookup(normalized_form, limit=3)
        except Exception:
            return []
        return self._corpus_evidence_from_matches(normalized_form, matches)

    def _corpus_evidence_from_matches(self, normalized_form: str, matches) -> list[EvidenceResult]:  # noqa: ANN001
        evidence: list[EvidenceResult] = []
        for match in matches:
            classification = self.lexical_match_classifier.classify(
                query_form=normalized_form,
                provider_role=EvidenceRole.CORPUS_ATTESTATION.value,
                matched_form=match.canonical_form,
                result_headword=match.canonical_form,
                lemma=match.canonical_form,
                corpus_token_count=match.token_count,
                corpus_source_count=match.source_count,
            )
            evidence.append(
                EvidenceResult(
                    provider_key="nayiri_western_corpus",
                    provider_type="corpus",
                    evidence_role="corpus_attestation",
                    language_profile="western",
                    query_form=normalized_form,
                    matched_form=match.canonical_form,
                    lemma=match.canonical_form,
                    result_headword=match.canonical_form,
                    match_type=classification.match_type.value,
                    validation_strength=classification.validation_strength.value,
                    evidence_strength=classification.evidence_strength,
                    definition_quality=classification.definition_quality,
                    confidence=classification.confidence_score,
                    is_exact_match=classification.is_exact_match,
                    is_substring_match=classification.is_substring_match,
                    is_fuzzy_match=classification.is_fuzzy_match,
                    is_canonical_match=classification.is_canonical_match,
                    payload={
                        "token_count": match.token_count,
                        "source_count": match.source_count,
                        **self._classification_payload(classification),
                    },
                )
            )
        return evidence

    @staticmethod
    def _reference_definition_quality(entry: ReferenceEntry) -> str:
        metadata = entry.metadata_json or {}
        for key in ("definition", "gloss", "translation", "description"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return "good"
        return "missing"

    @staticmethod
    def _classification_payload(classification) -> dict[str, object]:
        payload: dict[str, object] = {}
        if classification.reason:
            payload["classification_reason"] = classification.reason
        payload["is_exact_match"] = classification.is_exact_match
        payload["is_substring_match"] = classification.is_substring_match
        payload["is_fuzzy_match"] = classification.is_fuzzy_match
        payload["is_canonical_match"] = classification.is_canonical_match
        return payload

    @staticmethod
    def _canonical_candidate(
        normalized_form: str,
        *,
        lexeme_map: dict[str, dict[str, object]],
        reference_map: dict[str, list[ReferenceEntry]],
        corpus_map: dict[str, list[EvidenceResult]],
        external_map: dict[str, list[ExternalLookupResult]],
        morphology_summary: MorphologySummary,
    ) -> str | None:
        if normalized_form in lexeme_map:
            return str(lexeme_map[normalized_form]["canonical_form"])
        if reference_map.get(normalized_form):
            return reference_map[normalized_form][0].surface_form
        if corpus_map.get(normalized_form):
            return corpus_map[normalized_form][0].matched_form
        if morphology_summary.lemma_counts:
            return morphology_summary.lemma_counts.most_common(1)[0][0]
        if external_map.get(normalized_form):
            for result in external_map[normalized_form]:
                metadata = result.metadata_json or {}
                role = metadata.get("source_evidence_role")
                trusted_flag = metadata.get("trusted_dictionary_entry")
                if role == "human_approved_headword" or trusted_flag is True:
                    return result.matched_form
        return None

    @staticmethod
    def _morphology_payload(summary: MorphologySummary) -> dict[str, object]:
        return {
            "provider_key": summary.provider_key,
            "provider_type": "morphology",
            "evidence_role": "lemma_pos_features",
            "language_profile": DiscoveryCandidateService._provider_language_profile(summary.provider_key),
            "lemma_candidates": [
                {"value": lemma, "count": count}
                for lemma, count in summary.lemma_counts.most_common(5)
            ],
            "pos_candidates": [
                {"value": pos, "count": count}
                for pos, count in summary.pos_counts.most_common(5)
            ],
            "feature_candidates": [
                {"value": feature, "count": count}
                for feature, count in summary.feature_counts.most_common(10)
            ],
        }

    @staticmethod
    def _lexeme_resolution_payload(resolution: LexemeResolution) -> dict[str, object]:
        return {
            "surface_form": resolution.surface_form,
            "normalized_form": resolution.normalized_form,
            "pie_lemma": resolution.morphological_lemma,
            "morphological_lemma": resolution.morphological_lemma,
            "dictionary_lemma": resolution.dictionary_lemma,
            "normalized_dictionary_lemma": resolution.selected_dictionary_lemma_normalized,
            "dictionary_lemma_source": resolution.dictionary_lemma_source,
            "selected_dictionary_lemma": resolution.selected_dictionary_lemma,
            "selected_source": resolution.selected_source,
            "confidence": resolution.confidence,
            "conflict_status": resolution.conflict_status,
            "resolution_type": resolution.resolution_type,
            "resolution_status": resolution.resolution_status,
            "notes": resolution.notes,
            "dictionary_lemma_candidates": [
                {
                    "lemma": candidate.lemma,
                    "normalized_lemma": candidate.normalized_lemma,
                    "source_key": candidate.source_key,
                    "confidence": candidate.confidence,
                    "evidence_type": candidate.evidence_type,
                    "resolution_status": candidate.resolution_status,
                }
                for candidate in resolution.dictionary_lemma_candidates
            ],
            "morphological_analyses": [
                {
                    "surface_form": analysis.surface_form,
                    "normalized_surface_form": analysis.normalized_surface_form,
                    "lemma": analysis.lemma,
                    "normalized_lemma": analysis.normalized_lemma,
                    "pos": analysis.pos,
                    "features": analysis.features,
                    "language_profile": analysis.language_profile,
                    "source_key": analysis.source_key,
                    "confidence": analysis.confidence,
                }
                for analysis in resolution.morphological_analyses
            ],
            "ocr_correction_candidates": [
                {
                    "candidate": candidate.candidate,
                    "normalized_candidate": candidate.normalized_candidate,
                    "source_key": candidate.source_key,
                    "confidence": candidate.confidence,
                }
                for candidate in resolution.ocr_correction_candidates
            ],
        }

    @staticmethod
    def _morphology_provider_key(analyzer_model_key: str | None) -> str:
        model_key = (analyzer_model_key or "").strip().lower()
        if "classical" in model_key or model_key in {"xcl", "grabar"}:
            return "pie_classical_morphology"
        return "pie_eastern_morphology"

    @staticmethod
    def _document_language_profile(value: str | None) -> str:
        return normalize_language_profile(value)

    @staticmethod
    def _provider_language_profile(provider_key: str) -> str:
        if provider_key == "pie_classical_morphology":
            return "classical"
        if provider_key in {"pie_eastern_morphology", "pioner_ner"}:
            return "eastern"
        if provider_key in {"nayiri_western_corpus", "nayiri_corpus"}:
            return "western"
        return "mixed"

    @classmethod
    def _profile_adjusted_confidence(
        cls,
        confidence: float | None,
        source_profile: str | None,
        document_profile: str | None,
    ) -> float | None:
        if confidence is None:
            return None
        return round(confidence * profile_weight(source_profile, document_profile), 3)

    @classmethod
    def _profile_adjusted_evidence(
        cls,
        evidence: list[EvidenceResult],
        *,
        document_profile: str | None,
    ) -> list[EvidenceResult]:
        adjusted: list[EvidenceResult] = []
        for item in evidence:
            weight = profile_weight(item.language_profile, document_profile)
            if weight >= 0.75:
                adjusted.append(item)
                continue
            payload = {
                **item.payload,
                "profile_warning": "source language profile does not match the document profile",
                "document_language_profile": document_profile,
            }
            adjusted.append(
                EvidenceResult(
                    provider_key=item.provider_key,
                    provider_type=item.provider_type,
                    evidence_role=item.evidence_role,
                    query_form=item.query_form,
                    matched_form=item.matched_form,
                    result_headword=item.result_headword,
                    lemma=item.lemma,
                    match_type=item.match_type,
                    validation_strength=item.validation_strength,
                    evidence_strength="medium" if item.evidence_strength == "strong" else item.evidence_strength,
                    definition_quality=item.definition_quality,
                    language_variant=item.language_variant,
                    language_profile=item.language_profile,
                    priority=item.priority,
                    can_validate_word=item.can_validate_word,
                    can_attest_usage=item.can_attest_usage,
                    can_suggest_lemma=item.can_suggest_lemma,
                    can_suggest_named_entity=item.can_suggest_named_entity,
                    requires_exact_match=item.requires_exact_match,
                    requires_structured_headword=item.requires_structured_headword,
                    default_runtime=item.default_runtime,
                    independent_source_group=item.independent_source_group,
                    source_kind=item.source_kind,
                    confidence=cls._profile_adjusted_confidence(item.confidence, item.language_profile, document_profile),
                    is_exact_match=item.is_exact_match,
                    is_substring_match=item.is_substring_match,
                    is_fuzzy_match=item.is_fuzzy_match,
                    is_canonical_match=item.is_canonical_match,
                    citation=item.citation,
                    payload=payload,
                )
            )
        return adjusted

    @staticmethod
    def _provider_role(provider_type: str) -> str:
        return {
            "curated_lexicon": "curated_lexicon",
            "reference": "reference",
            "corpus": "corpus_attestation",
            "web_dictionary": "web_dictionary",
            "external_reference": "external_reference",
            "morphology": "morphology_analysis",
        }.get(provider_type, provider_type)

    @staticmethod
    def _resolve_external_source_evidence_role(*, provider_key: str, metadata: dict[str, object]) -> str:
        value = metadata.get("source_evidence_role")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if provider_key == "nayiri_web":
            return "nayiri_page_result"
        return "external_lookup_result"

    @staticmethod
    def _resolve_external_source_evidence_tier(*, provider_key: str, metadata: dict[str, object]) -> str:
        value = metadata.get("source_evidence_tier")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if provider_key == "nayiri_web":
            return "context_only"
        return "unknown"

    @staticmethod
    def _resolve_external_source_evidence_verified(*, provider_key: str, metadata: dict[str, object]) -> bool:
        value = metadata.get("source_evidence_verified")
        if isinstance(value, bool):
            return value
        if provider_key == "nayiri_web":
            return False
        return False

    @staticmethod
    def _is_trusted_dictionary_entry(*, source_evidence_role: str, metadata: dict[str, object]) -> bool:
        if source_evidence_role == "human_approved_headword":
            return True
        trusted_flag = metadata.get("trusted_dictionary_entry")
        if isinstance(trusted_flag, bool):
            return trusted_flag
        return False

    @staticmethod
    def _load_occurrence_evidence(
        session: Session,
        *,
        document_id: UUID,
        normalized_form: str,
        limit: int = 20,
    ) -> list[DiscoveryOccurrenceEvidence]:
        rows = session.execute(
            select(Occurrence, DocumentPage.extracted_text)
            .join(DocumentPage, Occurrence.page_id == DocumentPage.id)
            .where(
                Occurrence.document_id == document_id,
                Occurrence.normalized_token == normalized_form,
            )
            .order_by(Occurrence.page_number.asc(), Occurrence.char_start.asc().nullsfirst(), Occurrence.id.asc())
            .limit(limit)
        ).all()
        evidence: list[DiscoveryOccurrenceEvidence] = []
        for row, page_text in rows:
            highlight_start, highlight_end = context_snippet_highlight_range(
                page_text,
                row.char_start,
                row.char_end,
                row.context_snippet,
                token=row.token,
            )
            evidence.append(
                DiscoveryOccurrenceEvidence(
                    token=row.token,
                    normalized_token=row.normalized_token,
                    page_number=row.page_number,
                    context_snippet=row.context_snippet,
                    char_start=row.char_start,
                    char_end=row.char_end,
                    context_highlight_start=highlight_start,
                    context_highlight_end=highlight_end,
                )
            )
        return evidence

    @staticmethod
    def _delete_stale_candidates(
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
        keep_forms: list[str],
    ) -> None:
        filters = [
            DiscoveryCandidate.user_id == user_id,
            DiscoveryCandidate.document_id == document_id,
        ]
        if keep_forms:
            filters.append(DiscoveryCandidate.normalized_form.not_in(keep_forms))
        session.execute(delete(DiscoveryCandidate).where(*filters))

    @staticmethod
    def _build_summary(*, total_grouped_forms: int, counts: Counter[str]) -> DiscoveryBuildSummary:
        return DiscoveryBuildSummary(
            total_grouped_forms=total_grouped_forms,
            resolved_known=counts["resolved_known"],
            resolved_by_dictionary=counts["resolved_by_dictionary"],
            attested_in_corpus=counts["attested_in_corpus"],
            resolved_by_lemma=counts["resolved_by_lemma"],
            resolved_as_variant=counts["resolved_as_variant"],
            weakly_attested=counts["weakly_attested"],
            poorly_defined=counts["poorly_defined"],
            unknown_plausible=counts["unknown_plausible"],
            possible_ocr_noise=counts["possible_ocr_noise"],
            probable_ocr_noise=counts["probable_ocr_noise"],
            possible_named_entity=counts["possible_named_entity"],
            conflicting_sources=counts["conflicting_sources"],
            needs_linguist_research=counts["needs_linguist_research"],
            suppressed=counts["suppressed"],
            shown_in_queue=counts["shown_in_queue"],
        )


def get_discovery_candidate_service() -> DiscoveryCandidateService:
    return DiscoveryCandidateService()
