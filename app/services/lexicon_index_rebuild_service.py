from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import session_scope
from app.db.models import Document
from app.services.lexicon_group_index_service import get_lexicon_group_index_service


logger = logging.getLogger(__name__)


class LexiconIndexRebuildService:
    def rebuild_document(self, session: Session, *, user_id: UUID, document_id: UUID) -> int:
        index_service = get_lexicon_group_index_service()

        document = session.get(Document, document_id)
        if document is None or document.user_id != user_id:
            raise ValueError("Document not found.")

        forms = index_service.rebuild_document(
            session,
            user_id=user_id,
            document_id=document_id,
            document_title=document.title,
        )
        session.commit()
        return len(forms)

    def rebuild_user(self, session: Session, *, user_id: UUID) -> int:
        index_service = get_lexicon_group_index_service()

        document_count = index_service.rebuild_user(session, user_id=user_id)
        session.commit()
        return document_count

    def rebuild_document_async(self, *, user_id: UUID, document_id: UUID) -> str:
        from app.workers.tasks import rebuild_lexicon_index_document_task

        async_result = rebuild_lexicon_index_document_task.delay(str(user_id), str(document_id))
        return async_result.id

    def rebuild_user_async(self, *, user_id: UUID) -> str:
        from app.workers.tasks import rebuild_lexicon_index_user_task

        async_result = rebuild_lexicon_index_user_task.delay(str(user_id))
        return async_result.id

    @staticmethod
    def process_document_rebuild(*, user_id: str, document_id: str) -> int:
        with session_scope() as session:
            service = LexiconIndexRebuildService()
            return service.rebuild_document(
                session,
                user_id=UUID(user_id),
                document_id=UUID(document_id),
            )

    @staticmethod
    def process_user_rebuild(*, user_id: str) -> int:
        with session_scope() as session:
            service = LexiconIndexRebuildService()
            return service.rebuild_user(session, user_id=UUID(user_id))


def get_lexicon_index_rebuild_service() -> LexiconIndexRebuildService:
    return LexiconIndexRebuildService()
