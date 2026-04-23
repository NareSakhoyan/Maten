from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import session_scope
from app.core.config import Settings, get_settings
from app.db.models import (
    JobKind,
    JobResultResourceType,
    ReferenceEntry,
    ReferenceImportStatus,
    ReferenceSource,
    ReferenceSourceImport,
)
from app.schemas.reference import ReferenceImportResponse
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.ocr_service import OCRService, get_ocr_service
from app.services.reference_parsers.csv_parser import CsvReferenceParser
from app.services.reference_parsers.docx_parser import DocxReferenceParser
from app.services.reference_parsers.pdf_parser import PdfReferenceParser
from app.services.reference_parsers.txt_parser import TxtReferenceParser
from app.services.retry_errors import RetryStartError
from app.services.storage_service import StorageService, get_storage_service, sanitize_storage_filename


logger = logging.getLogger(__name__)


class ReferenceImportService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        ocr_service: OCRService | None = None,
        job_progress_service: JobProgressService | None = None,
        storage_service: StorageService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.ocr_service = ocr_service or get_ocr_service()
        self.job_progress_service = job_progress_service or get_job_progress_service()
        self.storage_service = storage_service or get_storage_service()
        self.txt_parser = TxtReferenceParser()
        self.csv_parser = CsvReferenceParser()
        self.docx_parser = DocxReferenceParser()
        self.pdf_parser = PdfReferenceParser(settings=self.settings, ocr_service=self.ocr_service)

    def create_import_run(
        self,
        session: Session,
        *,
        source: ReferenceSource,
        filename: str,
        mime_type: str | None = None,
        file_size_bytes: int | None = None,
    ) -> ReferenceSourceImport:
        import_run = ReferenceSourceImport(
            source_id=source.id,
            user_id=source.user_id,
            file_name=filename,
            file_type=self._file_suffix(filename),
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            status=ReferenceImportStatus.QUEUED,
            progress_percent=0,
            result_resource_type=JobResultResourceType.REFERENCE_SOURCE,
            result_resource_id=str(source.id),
        )
        session.add(import_run)
        session.flush()
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_IMPORT,
            job=import_run,
            stage_code="queued",
            progress_percent=0,
        )
        return import_run

    def create_retry_import_run(
        self,
        session: Session,
        *,
        user_id: UUID,
        source_id: UUID,
        failed_import_id: UUID,
    ) -> ReferenceSourceImport:
        source = session.get(ReferenceSource, source_id)
        if source is None or source.user_id != str(user_id):
            raise RetryStartError(status_code=404, message="Reference source not found.")

        failed_import = self.get_user_import(
            session,
            user_id=user_id,
            source_id=source_id,
            import_id=failed_import_id,
        )
        if failed_import is None:
            raise RetryStartError(status_code=404, message="Reference import not found.")
        if failed_import.status is not ReferenceImportStatus.FAILED:
            raise RetryStartError(status_code=409, message="Only failed reference imports can be retried.")
        if not failed_import.can_retry:
            raise RetryStartError(status_code=409, message="This reference import cannot be retried.")

        existing_active_retry = session.scalar(
            select(ReferenceSourceImport.id).where(
                ReferenceSourceImport.retry_of_job_id == failed_import.id,
                ReferenceSourceImport.status.in_([ReferenceImportStatus.QUEUED, ReferenceImportStatus.RUNNING]),
            )
        )
        if existing_active_retry is not None:
            raise RetryStartError(status_code=409, message="A retry is already running for this reference import.")

        self._ensure_source_file_exists(failed_import)

        retry_import = ReferenceSourceImport(
            source_id=failed_import.source_id,
            retry_of_job_id=failed_import.id,
            user_id=failed_import.user_id,
            file_name=failed_import.file_name,
            file_type=failed_import.file_type,
            storage_bucket=failed_import.storage_bucket,
            storage_path=failed_import.storage_path,
            mime_type=failed_import.mime_type,
            file_size_bytes=failed_import.file_size_bytes,
            status=ReferenceImportStatus.QUEUED,
            progress_percent=0,
            retry_count=failed_import.retry_count + 1,
            can_retry=True,
            result_resource_type=JobResultResourceType.REFERENCE_SOURCE,
            result_resource_id=str(source.id),
        )
        session.add(retry_import)
        session.flush()
        failed_import.last_retried_at = datetime.now(timezone.utc)
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.REFERENCE_IMPORT,
            job=retry_import,
            stage_code="queued",
            progress_percent=0,
        )
        session.commit()
        session.refresh(retry_import)
        return retry_import

    def store_import_file(
        self,
        session: Session,
        *,
        source: ReferenceSource,
        import_run: ReferenceSourceImport,
        content: bytes,
        content_type: str | None,
    ) -> None:
        storage_filename = sanitize_storage_filename(import_run.file_name or "reference_import.bin")
        storage_bucket = self.storage_service.settings.supabase_bucket_book_originals
        storage_path = f"{source.user_id}/reference-imports/{source.id}/{import_run.id}/{storage_filename}"
        self.storage_service.upload_bytes(
            bucket=storage_bucket,
            path=storage_path,
            data=content,
            content_type=content_type or "application/octet-stream",
            upsert=True,
        )
        import_run.storage_bucket = storage_bucket
        import_run.storage_path = storage_path
        import_run.mime_type = content_type or "application/octet-stream"
        import_run.file_size_bytes = len(content)
        session.flush()

    def list_imports(
        self,
        session: Session,
        *,
        user_id: UUID,
        source_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[ReferenceImportResponse], int]:
        filters = [
            ReferenceSourceImport.user_id == str(user_id),
            ReferenceSourceImport.source_id == source_id,
        ]
        total = session.scalar(select(func.count(ReferenceSourceImport.id)).where(*filters)) or 0
        runs = list(
            session.scalars(
                select(ReferenceSourceImport)
                .where(*filters)
                .order_by(
                    ReferenceSourceImport.created_at.desc(),
                    ReferenceSourceImport.updated_at.desc(),
                    ReferenceSourceImport.retry_count.desc(),
                    ReferenceSourceImport.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        )
        source = session.get(ReferenceSource, source_id)
        if source is None:
            return [], 0
        return [self.build_import_response(source, run) for run in runs], total

    def get_user_import(
        self,
        session: Session,
        *,
        user_id: UUID,
        source_id: UUID,
        import_id: UUID,
    ) -> ReferenceSourceImport | None:
        return session.scalar(
            select(ReferenceSourceImport).where(
                ReferenceSourceImport.id == import_id,
                ReferenceSourceImport.source_id == source_id,
                ReferenceSourceImport.user_id == str(user_id),
            )
        )

    def import_entries(
        self,
        session: Session,
        *,
        source: ReferenceSource,
        filename: str,
        content: bytes,
        import_run: ReferenceSourceImport | None = None,
    ) -> ReferenceImportResponse:
        active_import_run = import_run or self.create_import_run(session, source=source, filename=filename)
        try:
            active_import_run.status = ReferenceImportStatus.RUNNING
            active_import_run.started_at = datetime.now(timezone.utc)
            active_import_run.finished_at = None
            active_import_run.error_message = None
            active_import_run.error_code = None
            active_import_run.error_message_user = None
            active_import_run.next_steps = None
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.REFERENCE_IMPORT,
                job=active_import_run,
                stage_code="reading_source_file",
            )
            session.flush()

            parse_result = self._parse_file(filename=filename, content=content)
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.REFERENCE_IMPORT,
                job=active_import_run,
                stage_code="extracting_entries",
                progress_percent=40,
                items_processed=parse_result.rows_read,
                items_total=parse_result.rows_read,
            )
            if parse_result.import_method.value == "pdf_ocr":
                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.REFERENCE_IMPORT,
                    job=active_import_run,
                    stage_code="running_ocr",
                    message_user=parse_result.warning_message,
                )

            existing_pairs = set(
                session.execute(
                    select(ReferenceEntry.surface_form, ReferenceEntry.normalized_form).where(
                        ReferenceEntry.source_id == source.id,
                    )
                ).all()
            )
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.REFERENCE_IMPORT,
                job=active_import_run,
                stage_code="normalizing_entries",
                progress_percent=70,
                items_processed=parse_result.rows_read,
                items_total=parse_result.rows_read,
            )

            rows_to_insert = [
                ReferenceEntry(
                    source_id=source.id,
                    surface_form=row.surface_form,
                    normalized_form=row.normalized_form,
                    metadata_json=row.metadata_json,
                )
                for row in parse_result.rows
                if (row.surface_form, row.normalized_form) not in existing_pairs
            ]
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.REFERENCE_IMPORT,
                job=active_import_run,
                stage_code="saving_source",
                progress_percent=88,
                items_processed=self._handled_item_count(
                    rows_imported=len(rows_to_insert),
                    rows_skipped=parse_result.rows_read - len(rows_to_insert),
                ),
                items_total=parse_result.rows_read,
            )
            if rows_to_insert:
                session.add_all(rows_to_insert)

            source.last_import_method = parse_result.import_method
            source.last_import_warning = parse_result.warning_message
            source.last_imported_at = datetime.now(timezone.utc)
            source.entry_count = len(existing_pairs) + len(rows_to_insert)

            active_import_run.import_method = parse_result.import_method
            active_import_run.rows_read = parse_result.rows_read
            active_import_run.rows_imported = len(rows_to_insert)
            active_import_run.rows_skipped = parse_result.rows_read - len(rows_to_insert)
            active_import_run.warning_message = parse_result.warning_message

            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.REFERENCE_IMPORT,
                job=active_import_run,
                stage_code="finalizing",
                progress_percent=96,
                items_processed=self._handled_item_count(
                    rows_imported=active_import_run.rows_imported,
                    rows_skipped=active_import_run.rows_skipped,
                ),
                items_total=active_import_run.rows_read,
            )
            active_import_run.status = ReferenceImportStatus.COMPLETED
            active_import_run.items_processed = self._handled_item_count(
                rows_imported=active_import_run.rows_imported,
                rows_skipped=active_import_run.rows_skipped,
            )
            active_import_run.items_total = active_import_run.rows_read
            self.job_progress_service.complete(
                session,
                job_kind=JobKind.REFERENCE_IMPORT,
                job=active_import_run,
            )
            session.commit()
            session.refresh(source)
            session.refresh(active_import_run)
            return self.build_import_response(source, active_import_run)
        except Exception as exc:
            self._mark_import_failed(session, import_run=active_import_run, exc=exc)
            session.commit()
            raise

    def process_import_run(self, import_run_id: UUID | str) -> None:
        run_uuid = UUID(str(import_run_id))
        with session_scope() as session:
            import_run, source = self._load_import_run(session, run_uuid)
            import_run.status = ReferenceImportStatus.RUNNING
            import_run.started_at = datetime.now(timezone.utc)
            import_run.finished_at = None
            import_run.error_message = None
            import_run.error_code = None
            import_run.error_message_user = None
            import_run.next_steps = None
            self.job_progress_service.set_stage(
                session,
                job_kind=JobKind.REFERENCE_IMPORT,
                job=import_run,
                stage_code="reading_source_file",
            )
            logger.info(
                "Reference import started import_run_id=%s source_id=%s user_id=%s file_name=%s storage_path=%s",
                import_run.id,
                source.id,
                import_run.user_id,
                import_run.file_name,
                import_run.storage_path,
            )

        try:
            with session_scope() as session:
                import_run, source = self._load_import_run(session, run_uuid)
                if not import_run.storage_bucket or not import_run.storage_path:
                    raise ValueError("Reference import source file is not available.")
                content = self.storage_service.download_bytes(import_run.storage_bucket, import_run.storage_path)
                logger.info(
                    "Reference import downloaded source file import_run_id=%s source_id=%s bucket=%s path=%s bytes=%s",
                    import_run.id,
                    source.id,
                    import_run.storage_bucket,
                    import_run.storage_path,
                    len(content),
                )

            with session_scope() as session:
                import_run, source = self._load_import_run(session, run_uuid)
                logger.info(
                    "Reference import parsing file import_run_id=%s source_id=%s file_name=%s",
                    import_run.id,
                    source.id,
                    import_run.file_name,
                )
                parse_result = self._parse_file(filename=import_run.file_name or "", content=content)
                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.REFERENCE_IMPORT,
                    job=import_run,
                    stage_code="extracting_entries",
                    progress_percent=40,
                    items_processed=parse_result.rows_read,
                    items_total=parse_result.rows_read,
                )
                logger.info(
                    "Reference import parsed file import_run_id=%s source_id=%s file_name=%s import_method=%s rows_read=%s candidate_rows=%s warning=%s",
                    import_run.id,
                    source.id,
                    import_run.file_name,
                    parse_result.import_method.value,
                    parse_result.rows_read,
                    len(parse_result.rows),
                    bool(parse_result.warning_message),
                )
                if parse_result.import_method.value == "pdf_ocr":
                    self.job_progress_service.set_stage(
                        session,
                        job_kind=JobKind.REFERENCE_IMPORT,
                        job=import_run,
                        stage_code="running_ocr",
                        message_user=parse_result.warning_message,
                    )
                    logger.info(
                        "Reference import used OCR import_run_id=%s source_id=%s rows_read=%s warning_message=%s",
                        import_run.id,
                        source.id,
                        parse_result.rows_read,
                        parse_result.warning_message,
                    )

                existing_pairs = set(
                    session.execute(
                        select(ReferenceEntry.surface_form, ReferenceEntry.normalized_form).where(
                            ReferenceEntry.source_id == source.id,
                        )
                    ).all()
                )
                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.REFERENCE_IMPORT,
                    job=import_run,
                    stage_code="normalizing_entries",
                    progress_percent=70,
                    items_processed=parse_result.rows_read,
                    items_total=parse_result.rows_read,
                )
                logger.info(
                    "Reference import loaded existing entries import_run_id=%s source_id=%s existing_pairs=%s",
                    import_run.id,
                    source.id,
                    len(existing_pairs),
                )

                rows_to_insert = [
                    ReferenceEntry(
                        source_id=source.id,
                        surface_form=row.surface_form,
                        normalized_form=row.normalized_form,
                        metadata_json=row.metadata_json,
                    )
                    for row in parse_result.rows
                    if (row.surface_form, row.normalized_form) not in existing_pairs
                ]
                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.REFERENCE_IMPORT,
                    job=import_run,
                    stage_code="saving_source",
                    progress_percent=88,
                    items_processed=self._handled_item_count(
                        rows_imported=len(rows_to_insert),
                        rows_skipped=parse_result.rows_read - len(rows_to_insert),
                    ),
                    items_total=parse_result.rows_read,
                )
                logger.info(
                    "Reference import prepared rows import_run_id=%s source_id=%s candidate_rows=%s rows_to_insert=%s rows_skipped=%s",
                    import_run.id,
                    source.id,
                    len(parse_result.rows),
                    len(rows_to_insert),
                    parse_result.rows_read - len(rows_to_insert),
                )
                if rows_to_insert:
                    session.add_all(rows_to_insert)

                source.last_import_method = parse_result.import_method
                source.last_import_warning = parse_result.warning_message
                source.last_imported_at = datetime.now(timezone.utc)
                source.entry_count = len(existing_pairs) + len(rows_to_insert)

                import_run.import_method = parse_result.import_method
                import_run.rows_read = parse_result.rows_read
                import_run.rows_imported = len(rows_to_insert)
                import_run.rows_skipped = parse_result.rows_read - len(rows_to_insert)
                import_run.warning_message = parse_result.warning_message
                import_run.status = ReferenceImportStatus.COMPLETED

                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.REFERENCE_IMPORT,
                    job=import_run,
                    stage_code="finalizing",
                    progress_percent=96,
                    items_processed=self._handled_item_count(
                        rows_imported=import_run.rows_imported,
                        rows_skipped=import_run.rows_skipped,
                    ),
                    items_total=import_run.rows_read,
                )
                import_run.items_processed = self._handled_item_count(
                    rows_imported=import_run.rows_imported,
                    rows_skipped=import_run.rows_skipped,
                )
                import_run.items_total = import_run.rows_read
                self.job_progress_service.complete(
                    session,
                    job_kind=JobKind.REFERENCE_IMPORT,
                    job=import_run,
                )
                logger.info(
                    "Reference import completed import_run_id=%s source_id=%s status=%s import_method=%s rows_read=%s rows_imported=%s rows_skipped=%s items_processed=%s items_total=%s",
                    import_run.id,
                    source.id,
                    import_run.status.value,
                    import_run.import_method.value if import_run.import_method is not None else None,
                    import_run.rows_read,
                    import_run.rows_imported,
                    import_run.rows_skipped,
                    import_run.items_processed,
                    import_run.items_total,
                )
        except Exception as exc:
            with session_scope() as session:
                import_run, _ = self._load_import_run(session, run_uuid)
                self._mark_import_failed(session, import_run=import_run, exc=exc)
                logger.info(
                    "Reference import marked failed in database import_run_id=%s status=%s error_code=%s stage=%s",
                    import_run.id,
                    import_run.status.value,
                    import_run.error_code,
                    import_run.current_stage_code,
                )
            raise

    @staticmethod
    def build_import_response(source: ReferenceSource, import_run: ReferenceSourceImport) -> ReferenceImportResponse:
        return ReferenceImportResponse(
            id=import_run.id,
            source_id=source.id,
            source_display_name=source.display_name,
            status=import_run.status,
            file_name=import_run.file_name,
            file_type=import_run.file_type,
            rows_read=import_run.rows_read,
            rows_imported=import_run.rows_imported,
            rows_skipped=import_run.rows_skipped,
            import_method=import_run.import_method,
            warning_message=import_run.warning_message,
            error_message=import_run.error_message,
            error_code=import_run.error_code,
            error_message_user=import_run.error_message_user,
            next_steps=import_run.next_steps,
            current_stage_code=import_run.current_stage_code,
            current_stage_label=import_run.current_stage_label,
            stage_message_user=import_run.stage_message_user,
            progress_percent=import_run.progress_percent,
            items_processed=import_run.items_processed,
            items_total=import_run.items_total,
            result_resource_type=import_run.result_resource_type,
            result_resource_id=import_run.result_resource_id,
            started_at=import_run.started_at,
            finished_at=import_run.finished_at,
            created_at=import_run.created_at,
            updated_at=import_run.updated_at,
        )

    def _parse_file(self, *, filename: str, content: bytes):
        suffix = self._file_suffix(filename)
        if suffix == "txt":
            return self.txt_parser.parse(content)
        if suffix == "csv":
            return self.csv_parser.parse(content)
        if suffix == "docx":
            return self.docx_parser.parse(content)
        if suffix == "pdf":
            return self.pdf_parser.parse(content)
        raise ValueError("Unsupported reference import format.")

    def _file_suffix(self, filename: str) -> str:
        if "." not in filename:
            raise ValueError("Unsupported reference import format.")
        suffix = filename.rsplit(".", maxsplit=1)[1].lower()
        supported = {"txt", "csv", "docx", "pdf"}
        if suffix not in supported:
            raise ValueError("Unsupported reference import format.")
        return suffix

    def _ensure_source_file_exists(self, import_run: ReferenceSourceImport) -> None:
        if not import_run.storage_bucket or not import_run.storage_path:
            raise RetryStartError(
                status_code=409,
                message="The original reference import file could not be found. Upload it again and try again.",
            )
        try:
            self.storage_service.download_bytes(import_run.storage_bucket, import_run.storage_path)
        except Exception as exc:
            raise RetryStartError(
                status_code=409,
                message="The original reference import file could not be found. Upload it again and try again.",
            ) from exc

    @staticmethod
    def _handled_item_count(*, rows_imported: int | None, rows_skipped: int | None) -> int:
        return (rows_imported or 0) + (rows_skipped or 0)

    def _mark_import_failed(
        self,
        session: Session,
        *,
        import_run: ReferenceSourceImport,
        exc: Exception | None = None,
    ) -> None:
        error_code, error_message_user, next_steps, can_retry = self._failure_payload(exc)
        import_run.status = ReferenceImportStatus.FAILED
        import_run.error_code = error_code
        import_run.error_message = "Reference import failed."
        import_run.error_message_user = error_message_user
        import_run.next_steps = next_steps
        import_run.can_retry = can_retry
        self.job_progress_service.fail(
            session,
            job_kind=JobKind.REFERENCE_IMPORT,
            job=import_run,
            message_user=import_run.error_message_user,
        )

    @staticmethod
    def _failure_payload(exc: Exception | None) -> tuple[str, str, list[str], bool]:
        message = str(exc).lower() if exc is not None else ""
        if "unsupported reference import format" in message:
            return (
                "reference_import_unsupported_format",
                "This reference file format is not supported.",
                [
                    "Upload a .txt, .csv, .docx, or .pdf file.",
                    "If the source is in another format, convert it before importing.",
                ],
                False,
            )
        if isinstance(exc, FileNotFoundError) or "not available" in message:
            return (
                "reference_import_file_missing",
                "The original reference import file could not be found.",
                [
                    "Upload the reference file again and retry the import.",
                    "If this keeps happening, contact the administrator.",
                ],
                False,
            )
        return (
            "reference_import_failed",
            "The reference source could not be imported.",
            [
                "Try the import again.",
                "If it fails again, verify the file format or use a cleaner source file.",
            ],
            True,
        )

    @staticmethod
    def _load_import_run(session: Session, run_id: UUID) -> tuple[ReferenceSourceImport, ReferenceSource]:
        import_run = session.get(ReferenceSourceImport, run_id)
        if import_run is None:
            raise ValueError("Reference import not found.")
        source = session.get(ReferenceSource, import_run.source_id)
        if source is None:
            raise ValueError("Reference source not found.")
        return import_run, source


def get_reference_import_service() -> ReferenceImportService:
    return ReferenceImportService()
