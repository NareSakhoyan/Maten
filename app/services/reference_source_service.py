from __future__ import annotations

import logging
import re
from uuid import UUID

from sqlalchemy import inspect, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.db.models import (
    ReferenceMatchingDirection,
    ReferenceMatchRun,
    ReferenceSource,
    ReferenceSourceImport,
    ReferenceSourceType,
)
from app.schemas.reference import (
    ReferenceImportResponse,
    ReferenceSourceCreateRequest,
    ReferenceSourceDetail,
    ReferenceSourceSummary,
)
from app.services.reference_import_service import ReferenceImportService
from app.utils.text_normalization import normalize_unicode


DEFAULT_REFERENCE_SOURCE_KEY = "manual_reference"
DEFAULT_REFERENCE_SOURCE_NAME = "Manual Reference Source"

NON_WORD_RE = re.compile(r"\W+", re.UNICODE)
REFERENCE_SOURCE_TABLES = ("reference_sources", "reference_entries", "reference_source_imports")
REFERENCE_SOURCE_REQUIRED_COLUMNS = {
    "reference_sources": {
        "id",
        "user_id",
        "key",
        "display_name",
        "source_type",
        "last_import_method",
        "last_import_warning",
        "last_imported_at",
        "entry_count",
    },
    "reference_entries": {"id", "source_id", "surface_form", "normalized_form"},
    "reference_source_imports": {
        "id",
        "source_id",
        "user_id",
        "retry_of_job_id",
        "status",
        "retry_count",
        "can_retry",
        "last_retried_at",
        "progress_percent",
        "current_stage_code",
    },
}


class ReferenceSourceSchemaNotReadyError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


class ReferenceSourceService:
    def ensure_default_source(self, session: Session, *, user_id: UUID) -> ReferenceSource:
        self.ensure_reference_schema(session)
        user_key = str(user_id)
        source = session.scalar(
            select(ReferenceSource).where(
                ReferenceSource.user_id == user_key,
                ReferenceSource.key == DEFAULT_REFERENCE_SOURCE_KEY,
            )
        )
        if source is not None:
            return source

        source = ReferenceSource(
            user_id=user_key,
            key=DEFAULT_REFERENCE_SOURCE_KEY,
            display_name=DEFAULT_REFERENCE_SOURCE_NAME,
            description="Default personal reference list for manual additions.",
            source_type=ReferenceSourceType.MANUAL,
            is_active=True,
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        return source

    def create_source(
        self,
        session: Session,
        *,
        user_id: UUID,
        request: ReferenceSourceCreateRequest,
    ) -> ReferenceSourceDetail:
        self.ensure_reference_schema(session)
        self.ensure_default_source(session, user_id=user_id)

        display_name = request.display_name.strip()
        if not display_name:
            raise ValueError("display_name must not be empty.")

        source = ReferenceSource(
            user_id=str(user_id),
            key=self._next_key(session, user_id=user_id, display_name=display_name),
            display_name=display_name,
            description=request.description.strip() if request.description else None,
            source_type=request.source_type,
            language=request.language.strip() if request.language else None,
            is_active=True,
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        return self.build_source_detail(session, source)

    def list_sources(self, session: Session, *, user_id: UUID) -> list[ReferenceSourceSummary]:
        self.ensure_reference_schema(session)
        self.ensure_default_source(session, user_id=user_id)
        rows = list(
            session.scalars(
                select(ReferenceSource)
                .where(ReferenceSource.user_id == str(user_id))
                .order_by(ReferenceSource.created_at.asc(), ReferenceSource.id.asc())
            )
        )
        summaries: list[ReferenceSourceSummary] = []
        for source in rows:
            latest_import = session.scalar(
                select(ReferenceSourceImport)
                .where(ReferenceSourceImport.source_id == source.id)
                .order_by(
                    ReferenceSourceImport.created_at.desc(),
                    ReferenceSourceImport.updated_at.desc(),
                    ReferenceSourceImport.retry_count.desc(),
                    ReferenceSourceImport.id.desc(),
                )
                .limit(1)
            )
            summaries.append(
                ReferenceSourceSummary(
                    id=source.id,
                    key=source.key,
                    display_name=source.display_name,
                    description=source.description,
                    source_type=source.source_type,
                    language=source.language,
                    is_active=source.is_active,
                    entry_count=source.entry_count,
                    last_import_method=source.last_import_method,
                    last_import_warning=source.last_import_warning,
                    last_imported_at=source.last_imported_at,
                    latest_import_job_id=latest_import.id if latest_import is not None else None,
                    latest_import_job_status=latest_import.status.value if latest_import is not None else None,
                    created_at=source.created_at,
                    updated_at=source.updated_at,
                )
            )
        return summaries

    def get_user_source(self, session: Session, *, user_id: UUID, source_id: UUID) -> ReferenceSource | None:
        self.ensure_reference_schema(session)
        self.ensure_default_source(session, user_id=user_id)
        return session.scalar(
            select(ReferenceSource).where(
                ReferenceSource.id == source_id,
                ReferenceSource.user_id == str(user_id),
            )
        )

    def get_source_detail(self, session: Session, *, user_id: UUID, source_id: UUID) -> ReferenceSourceDetail | None:
        source = self.get_user_source(session, user_id=user_id, source_id=source_id)
        if source is None:
            return None
        return self.build_source_detail(session, source)

    @staticmethod
    def build_source_detail(session: Session, source: ReferenceSource) -> ReferenceSourceDetail:
        latest_import = session.scalar(
            select(ReferenceSourceImport)
            .where(ReferenceSourceImport.source_id == source.id)
            .order_by(
                ReferenceSourceImport.created_at.desc(),
                ReferenceSourceImport.updated_at.desc(),
                ReferenceSourceImport.retry_count.desc(),
                ReferenceSourceImport.id.desc(),
            )
            .limit(1)
        )
        latest_match_run = session.scalar(
            select(ReferenceMatchRun)
            .where(
                ReferenceMatchRun.user_id == source.user_id,
                ReferenceMatchRun.source_id == source.id,
                ReferenceMatchRun.matching_direction == ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
            )
            .order_by(
                ReferenceMatchRun.created_at.desc(),
                ReferenceMatchRun.updated_at.desc(),
                ReferenceMatchRun.retry_count.desc(),
                ReferenceMatchRun.id.desc(),
            )
            .limit(1)
        )
        from app.services.source_word_review_service import SourceWordReviewService

        workspace_summary = SourceWordReviewService().count_reference_source_workspace_summary(
            session,
            user_id=UUID(source.user_id),
            source=source,
        )
        return ReferenceSourceDetail(
            id=source.id,
            key=source.key,
            display_name=source.display_name,
            description=source.description,
            source_type=source.source_type,
            language=source.language,
            is_active=source.is_active,
            entry_count=source.entry_count,
            last_import_method=source.last_import_method,
            last_import_warning=source.last_import_warning,
            last_imported_at=source.last_imported_at,
            latest_import_job_id=latest_import.id if latest_import is not None else None,
            latest_import_job_status=latest_import.status.value if latest_import is not None else None,
            latest_import=(
                ReferenceImportService.build_import_response(source, latest_import)
                if latest_import is not None
                else None
            ),
            latest_match_run_id=latest_match_run.id if latest_match_run is not None else None,
            latest_match_run_status=latest_match_run.status.value if latest_match_run is not None else None,
            imported_entry_count=workspace_summary["imported_entry_count"],
            matched_entry_count=workspace_summary["matched_entry_count"],
            unmatched_entry_count=workspace_summary["unmatched_entry_count"],
            created_at=source.created_at,
            updated_at=source.updated_at,
        )

    def _next_key(self, session: Session, *, user_id: UUID, display_name: str) -> str:
        base_key = self._slugify(display_name)
        user_key = str(user_id)
        existing_keys = set(
            session.scalars(
                select(ReferenceSource.key).where(
                    ReferenceSource.user_id == user_key,
                    ReferenceSource.key.like(f"{base_key}%"),
                )
            )
        )
        if base_key not in existing_keys:
            return base_key

        suffix = 2
        while True:
            candidate = f"{base_key}_{suffix}"
            if candidate not in existing_keys:
                return candidate
            suffix += 1

    @staticmethod
    def _slugify(value: str) -> str:
        normalized = normalize_unicode(value).strip().lower()
        slug = NON_WORD_RE.sub("_", normalized).strip("_")
        return slug or "reference_source"

    def ensure_reference_schema(self, session: Session) -> None:
        try:
            inspector = inspect(session.connection())
            missing_tables = [
                table_name for table_name in REFERENCE_SOURCE_TABLES if not inspector.has_table(table_name)
            ]
            missing_columns = {
                table_name: sorted(
                    REFERENCE_SOURCE_REQUIRED_COLUMNS[table_name]
                    - {column["name"] for column in inspector.get_columns(table_name)}
                )
                for table_name in REFERENCE_SOURCE_TABLES
                if table_name not in missing_tables
            }
            missing_columns = {
                table_name: columns
                for table_name, columns in missing_columns.items()
                if columns
            }
            if not missing_tables and not missing_columns:
                return
            logger.error(
                "Reference source schema not ready: missing_tables=%s missing_columns=%s",
                missing_tables,
                missing_columns,
            )
        except ProgrammingError as exc:  # pragma: no cover - depends on live database state
            if not self._is_missing_reference_table_error(exc):
                raise
            logger.error("Reference source schema inspection failed due to database schema error: %s", exc)
        raise ReferenceSourceSchemaNotReadyError(
            "Reference sources are temporarily unavailable."
        )

    @staticmethod
    def _is_missing_reference_table_error(exc: ProgrammingError) -> bool:
        message = str(exc).lower()
        return "undefinedtable" in message or "does not exist" in message or "no such table" in message


def get_reference_source_service() -> ReferenceSourceService:
    return ReferenceSourceService()
