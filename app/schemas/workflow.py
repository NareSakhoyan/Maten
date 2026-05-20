from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.db.models import DocumentWorkflowStage
from app.schemas.common import APIModel, OffsetPagination
from app.schemas.document import DocumentRead


class DocumentWorkflowRead(APIModel):
    document_id: UUID
    user_id: UUID
    stage: DocumentWorkflowStage
    candidate_count: int
    linked_count: int
    ignored_count: int
    suspicious_count: int
    last_job_id: UUID | None = None
    last_activity_at: datetime
    review_lexicon_path: str | None = None


class ReviewQueueItemRead(APIModel):
    document: DocumentRead
    workflow: DocumentWorkflowRead


class ReviewQueueListResponse(OffsetPagination):
    items: list[ReviewQueueItemRead]
