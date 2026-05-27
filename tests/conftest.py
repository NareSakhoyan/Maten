from __future__ import annotations

import os
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite://")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-key")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/0")
os.environ.setdefault("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
from app.db.models import Base, Document


PRIMARY_USER_ID = UUID("123e4567-e89b-12d3-a456-426614174000")
SECONDARY_USER_ID = UUID("223e4567-e89b-12d3-a456-426614174001")


@pytest.fixture()
def db_engine() -> Engine:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def session_factory(db_engine: Engine):
    return sessionmaker(bind=db_engine, autoflush=False, autocommit=False, expire_on_commit=False)


def rebuild_lexicon_index_for_document(
    session: Session,
    *,
    user_id: UUID,
    document: Document,
) -> None:
    """Mirror production: lexicon list/workflow read from index, not raw occurrences."""
    from app.services.lexicon_group_index_service import LexiconGroupIndexService

    LexiconGroupIndexService().rebuild_document(
        session,
        user_id=user_id,
        document_id=document.id,
        document_title=document.title,
    )
    session.commit()


@pytest.fixture()
def db_session(session_factory) -> Session:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
