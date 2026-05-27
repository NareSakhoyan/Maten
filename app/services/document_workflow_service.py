from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, distinct, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentStatus,
    DocumentWorkflow,
    DocumentWorkflowStage,
    IngestionJob,
    LexiconGroupIndex,
    LexiconGroupIndexDocument,
    Occurrence,
    OccurrenceScriptType,
)
from app.schemas.document import DocumentRead
from app.schemas.lexicon import LexiconGroupState, LexiconGroupView
from app.schemas.workflow import DocumentWorkflowRead, ReviewQueueItemRead
from app.services.document_service import DocumentService, get_document_service
from app.services.lexicon_service import LexiconService, get_lexicon_service


REVIEW_QUEUE_STAGES = {
    DocumentWorkflowStage.READY_FOR_REVIEW,
    DocumentWorkflowStage.IN_REVIEW,
    DocumentWorkflowStage.CURATED_PARTIAL,
}


class DocumentWorkflowService:
    def __init__(
        self,
        *,
        lexicon_service: LexiconService | None = None,
        document_service: DocumentService | None = None,
    ) -> None:
        self.lexicon_service = lexicon_service or get_lexicon_service()
        self.document_service = document_service or get_document_service()

    def ensure_workflow(
        self,
        session: Session,
        *,
        document: Document,
        last_job_id: UUID | None = None,
    ) -> DocumentWorkflow:
        workflow = session.get(DocumentWorkflow, document.id)
        if workflow is None:
            workflow = DocumentWorkflow(
                document_id=document.id,
                user_id=document.user_id,
                stage=DocumentWorkflowStage.UPLOADED,
            )
            session.add(workflow)
            session.flush()
        if last_job_id is not None:
            workflow.last_job_id = last_job_id
        return workflow

    def sync_for_document(
        self,
        session: Session,
        *,
        document_id: UUID,
        last_job_id: UUID | None = None,
    ) -> DocumentWorkflow | None:
        document = session.get(Document, document_id)
        if document is None:
            return None

        workflow = self.ensure_workflow(session, document=document, last_job_id=last_job_id)
        counts = self._count_document_groups(session, user_id=document.user_id, document_id=document.id)

        workflow.candidate_count = counts["candidate_count"]
        workflow.linked_count = counts["linked_count"]
        workflow.ignored_count = counts["ignored_count"]
        workflow.suspicious_count = counts["suspicious_count"]
        workflow.stage = self._resolve_stage(document.status, counts)
        workflow.last_activity_at = datetime.now(timezone.utc)
        if last_job_id is not None:
            workflow.last_job_id = last_job_id
        session.flush()
        return workflow

    def sync_for_normalized_forms(
        self,
        session: Session,
        *,
        user_id: UUID,
        normalized_forms: list[str],
    ) -> None:
        if not normalized_forms:
            return

        document_ids = session.scalars(
            select(distinct(Occurrence.document_id))
            .join(Document, Occurrence.document_id == Document.id)
            .where(
                Document.user_id == user_id,
                Occurrence.normalized_token.in_(normalized_forms),
            )
        ).all()
        for document_id in document_ids:
            self.sync_for_document(session, document_id=document_id)

    def get_document_workflow(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
    ) -> DocumentWorkflowRead | None:
        document = self.document_service.get_user_document(session, user_id=user_id, document_id=document_id)
        if document is None:
            return None

        workflow = self.sync_for_document(session, document_id=document_id)
        if workflow is None:
            return None
        session.commit()
        return self.build_workflow_read(workflow)

    def list_review_queue(
        self,
        session: Session,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[ReviewQueueItemRead], int]:
        missing_document_ids = session.scalars(
            select(Document.id).where(
                Document.user_id == user_id,
                Document.status == DocumentStatus.COMPLETED,
                ~Document.id.in_(select(DocumentWorkflow.document_id)),
            )
        ).all()
        for document_id in missing_document_ids:
            self.sync_for_document(session, document_id=document_id)
        if missing_document_ids:
            session.commit()

        workflows = session.scalars(
            select(DocumentWorkflow)
            .where(
                DocumentWorkflow.user_id == user_id,
                DocumentWorkflow.stage.in_(REVIEW_QUEUE_STAGES),
            )
            .order_by(
                DocumentWorkflow.candidate_count.desc(),
                DocumentWorkflow.last_activity_at.desc(),
            )
        ).all()

        items: list[ReviewQueueItemRead] = []
        for workflow in workflows:
            document = session.get(Document, workflow.document_id)
            if document is None or document.user_id != user_id:
                continue
            items.append(
                ReviewQueueItemRead(
                    document=self.document_service.build_document_read(session, document),
                    workflow=self.build_workflow_read(workflow),
                )
            )

        total = len(items)
        return items[offset : offset + limit], total

    def build_workflow_read(self, workflow: DocumentWorkflow) -> DocumentWorkflowRead:
        review_path = None
        if workflow.stage in REVIEW_QUEUE_STAGES or workflow.candidate_count > 0:
            review_path = f"/lexicon?document_id={workflow.document_id}&view=candidates"
        return DocumentWorkflowRead(
            document_id=workflow.document_id,
            user_id=workflow.user_id,
            stage=workflow.stage,
            candidate_count=workflow.candidate_count,
            linked_count=workflow.linked_count,
            ignored_count=workflow.ignored_count,
            suspicious_count=workflow.suspicious_count,
            last_job_id=workflow.last_job_id,
            last_activity_at=workflow.last_activity_at,
            review_lexicon_path=review_path,
        )

    def _count_document_groups(self, session: Session, *, user_id: UUID, document_id: UUID) -> dict[str, int]:
        return self._count_document_groups_from_index(session, user_id=user_id, document_id=document_id)

    @staticmethod
    def _count_document_groups_from_index(
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
    ) -> dict[str, int]:
        base = (
            select(LexiconGroupIndex.group_state, LexiconGroupIndex.dominant_script_type)
            .join(
                LexiconGroupIndexDocument,
                and_(
                    LexiconGroupIndex.user_id == LexiconGroupIndexDocument.user_id,
                    LexiconGroupIndex.normalized_form == LexiconGroupIndexDocument.normalized_form,
                ),
            )
            .where(
                LexiconGroupIndex.user_id == user_id,
                LexiconGroupIndexDocument.document_id == document_id,
            )
        )
        rows = session.execute(base).all()
        candidate_count = 0
        linked_count = 0
        ignored_count = 0
        suspicious_count = 0
        for group_state, dominant_script_type in rows:
            if group_state == LexiconGroupState.LINKED.value:
                linked_count += 1
            if group_state == LexiconGroupState.IGNORED_NOISE.value:
                ignored_count += 1
            if (
                group_state != LexiconGroupState.IGNORED_NOISE.value
                and dominant_script_type != OccurrenceScriptType.ARMENIAN
            ):
                suspicious_count += 1
            if (
                group_state == LexiconGroupState.UNREVIEWED.value
                and dominant_script_type == OccurrenceScriptType.ARMENIAN
            ):
                candidate_count += 1
        return {
            "candidate_count": candidate_count,
            "linked_count": linked_count,
            "ignored_count": ignored_count,
            "suspicious_count": suspicious_count,
        }

    @staticmethod
    def _resolve_stage(document_status: DocumentStatus, counts: dict[str, int]) -> DocumentWorkflowStage:
        if document_status is DocumentStatus.FAILED:
            return DocumentWorkflowStage.FAILED
        if document_status in {DocumentStatus.QUEUED, DocumentStatus.PROCESSING}:
            return DocumentWorkflowStage.INGESTING
        if document_status is DocumentStatus.UPLOADED:
            return DocumentWorkflowStage.UPLOADED

        candidate_count = counts["candidate_count"]
        linked_count = counts["linked_count"]

        if candidate_count == 0:
            return DocumentWorkflowStage.CURATED_COMPLETE
        if linked_count > 0:
            return DocumentWorkflowStage.IN_REVIEW
        return DocumentWorkflowStage.READY_FOR_REVIEW


def get_document_workflow_service() -> DocumentWorkflowService:
    return DocumentWorkflowService()

