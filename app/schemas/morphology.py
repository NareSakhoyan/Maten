from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, model_validator

from app.db.models import MorphologyAnalysisStatus, MorphologyRunStatus
from app.schemas.common import APIModel, JobProgressState
from app.schemas.document import DocumentRead
from app.schemas.job import LongRunningJobRead
from app.schemas.reference import ReferenceSourceDetail


class MorphologyRunCreateRequest(APIModel):
    document_id: UUID | None = None
    reference_source_id: UUID | None = None
    analyzer: str = Field(default="pie", min_length=1)

    @model_validator(mode="after")
    def validate_scope(self) -> "MorphologyRunCreateRequest":
        if bool(self.document_id) == bool(self.reference_source_id):
            raise ValueError("Provide exactly one of document_id or reference_source_id.")
        return self


class MorphologySettingsUpdateRequest(APIModel):
    language_stage: str | None = None
    morphology_profile: str | None = None
    run_morphology: bool = False
    analyzer: str = Field(default="pie", min_length=1)


class MorphologyRunRead(JobProgressState):
    id: UUID
    user_id: str
    document_id: UUID | None = None
    reference_source_id: UUID | None = None
    source_type: str
    analyzer_provider: str
    analyzer_model_key: str
    analyzer_version: str | None = None
    status: MorphologyRunStatus
    completed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    error_message: str | None = None
    error_code: str | None = None
    error_message_user: str | None = None
    next_steps: list[str] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MorphologyRunStartResponse(APIModel):
    message: str
    run: MorphologyRunRead
    job: LongRunningJobRead


class DocumentMorphologySettingsResponse(APIModel):
    message: str
    document: DocumentRead
    run: MorphologyRunRead | None = None
    job: LongRunningJobRead | None = None


class ReferenceSourceMorphologySettingsResponse(APIModel):
    message: str
    source: ReferenceSourceDetail
    run: MorphologyRunRead | None = None
    job: LongRunningJobRead | None = None


class MorphologySummaryResponse(APIModel):
    analyzed_occurrence_count: int = 0
    completed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    distinct_lemma_count: int = 0


class MorphologyCount(APIModel):
    value: str
    count: int


class MorphologyWordResponse(APIModel):
    normalized_form: str
    analyzed_occurrence_count: int = 0
    completed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    lemma_candidates: list[MorphologyCount] = Field(default_factory=list)
    pos_distribution: list[MorphologyCount] = Field(default_factory=list)
    morph_feature_summaries: dict[str, list[MorphologyCount]] = Field(default_factory=dict)


class MorphologyWordEvidenceSummary(APIModel):
    morphology_available: bool = False
    best_lemma: str | None = None
    lemma_candidates: list[str] = Field(default_factory=list)
    pos_candidates: list[str] = Field(default_factory=list)


class MorphologyAnalysisRead(APIModel):
    id: UUID
    occurrence_id: UUID | None = None
    document_id: UUID | None = None
    page_id: UUID | None = None
    reference_source_id: UUID | None = None
    reference_entry_id: UUID | None = None
    source_type: str
    token_surface: str
    token_normalized: str
    lemma: str | None = None
    lemma_normalized: str | None = None
    pos: str | None = None
    morph_features: dict[str, object] | None = None
    analyzer_provider: str
    analyzer_model_key: str
    analyzer_version: str | None = None
    analysis_status: MorphologyAnalysisStatus
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime
