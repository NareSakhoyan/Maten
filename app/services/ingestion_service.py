from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import joinedload

from app.core.database import session_scope
from app.db.models import Document, DocumentPage, DocumentStatus, IngestionJob, IngestionJobStatus, JobKind, Occurrence
from app.services.ingestion_error_service import IngestionErrorService, get_ingestion_error_service
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.lexicon_group_index_service import get_lexicon_group_index_service
from app.services.occurrence_service import OccurrenceService, get_occurrence_service
from app.services.page_extraction_service import PageExtractionService, get_page_extraction_service
from app.services.storage_service import StorageService, get_storage_service
from app.utils.mime import detect_mime_type
from app.utils.text_reconstruction import reconstruct_page_text


logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(
        self,
        storage_service: StorageService | None = None,
        page_extraction_service: PageExtractionService | None = None,
        occurrence_service: OccurrenceService | None = None,
        ingestion_error_service: IngestionErrorService | None = None,
        job_progress_service: JobProgressService | None = None,
    ) -> None:
        self.storage_service = storage_service or get_storage_service()
        self.page_extraction_service = page_extraction_service or get_page_extraction_service()
        self.occurrence_service = occurrence_service or get_occurrence_service()
        self.ingestion_error_service = ingestion_error_service or get_ingestion_error_service()
        self.job_progress_service = job_progress_service or get_job_progress_service()

    def process_job(self, job_id: UUID | str) -> None:
        job_uuid = UUID(str(job_id))
        page_iterator = None
        page_count = 0

        index_service = get_lexicon_group_index_service()

        try:
            with session_scope() as session:
                job = self._load_job(session, job_uuid)
                document = job.document

                job.status = IngestionJobStatus.RUNNING
                job.step = "downloading_original"
                job.progress_percent = 0
                job.error_message = None
                job.error_code = None
                job.error_message_user = None
                job.error_message_technical = None
                job.next_steps = None
                job.can_retry = True
                job.started_at = datetime.now(timezone.utc)
                job.finished_at = None
                job.items_processed = 0
                job.items_total = None

                document.status = DocumentStatus.PROCESSING
                from app.services.document_workflow_service import get_document_workflow_service

                get_document_workflow_service().sync_for_document(
                    session,
                    document_id=document.id,
                    last_job_id=job.id,
                )

                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.INGESTION,
                    job=job,
                    stage_code="loading_source_file",
                    progress_percent=5,
                )

            with session_scope() as session:
                job = self._load_job(session, job_uuid)
                document = job.document
                original_bytes = self.storage_service.download_bytes(document.storage_bucket, document.storage_path)

            with session_scope() as session:
                job = self._load_job(session, job_uuid)
                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.INGESTION,
                    job=job,
                    stage_code="opening_document",
                    progress_percent=10,
                )

            with session_scope() as session:
                job = self._load_job(session, job_uuid)
                document = job.document
                index_service.clear_document_index(
                    session,
                    user_id=document.user_id,
                    document_id=document.id,
                )
                session.execute(delete(Occurrence).where(Occurrence.document_id == document.id))
                session.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))

                mime_type = detect_mime_type(document.original_filename, original_bytes, document.mime_type)
                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.INGESTION,
                    job=job,
                    stage_code="opening_document",
                    progress_percent=10,
                    append_event=False,
                )
                page_count, page_iterator = self.page_extraction_service.iter_document_pages(
                    original_bytes,
                    mime_type,
                )
                document.page_count = page_count
                job.items_total = page_count
                self.job_progress_service.update_progress(
                    session,
                    job_kind=JobKind.INGESTION,
                    job=job,
                    items_processed=0,
                    items_total=page_count,
                )

            if page_iterator is None:
                raise RuntimeError(f"Could not initialize extraction for job {job_uuid}.")

            processed_pages = 0
            for extracted_page in page_iterator:
                with session_scope() as session:
                    job = self._load_job(session, job_uuid)
                    document = job.document
                    extraction_stage = (
                        "running_ocr"
                        if extracted_page.extraction_method.value == "ocr"
                        else "extracting_text"
                    )
                    self.job_progress_service.set_stage(
                        session,
                        job_kind=JobKind.INGESTION,
                        job=job,
                        stage_code=extraction_stage,
                        progress_percent=self.job_progress_service.ranged_progress(
                            processed_pages,
                            page_count,
                            start_percent=15,
                            end_percent=65,
                        ),
                        items_processed=processed_pages,
                        items_total=page_count,
                        append_event=False,
                    )
                    reconstructed_text = reconstruct_page_text(extracted_page.extracted_text)
                    self.job_progress_service.set_stage(
                        session,
                        job_kind=JobKind.INGESTION,
                        job=job,
                        stage_code="reconstructing_text",
                        progress_percent=self.job_progress_service.ranged_progress(
                            processed_pages + 1,
                            page_count,
                            start_percent=66,
                            end_percent=78,
                        ),
                        items_processed=processed_pages,
                        items_total=page_count,
                        append_event=False,
                    )

                    page_image_bucket = None
                    page_image_path = None
                    if extracted_page.page_image_png is not None:
                        page_image_bucket = self.storage_service.settings.supabase_bucket_page_images
                        page_image_path = (
                            f"{document.user_id}/{document.id}/pages/{extracted_page.page_number:04d}.png"
                        )
                        self.storage_service.upload_bytes(
                            bucket=page_image_bucket,
                            path=page_image_path,
                            data=extracted_page.page_image_png,
                            content_type="image/png",
                            upsert=True,
                        )
                        self.storage_service.upload_json(
                            bucket=self.storage_service.settings.supabase_bucket_ocr_json,
                            path=f"{document.user_id}/{document.id}/pages/{extracted_page.page_number:04d}.json",
                            payload={
                                "document_id": str(document.id),
                                "page_number": extracted_page.page_number,
                                "extraction_method": extracted_page.extraction_method.value,
                                "ocr_lang": self.page_extraction_service.ocr_service.settings.tesseract_lang,
                                "char_count": extracted_page.char_count,
                                "text": extracted_page.extracted_text,
                            },
                            upsert=True,
                        )

                    page = DocumentPage(
                        document_id=document.id,
                        page_number=extracted_page.page_number,
                        extraction_method=extracted_page.extraction_method,
                        page_image_bucket=page_image_bucket,
                        page_image_path=page_image_path,
                        raw_extracted_text=extracted_page.extracted_text,
                        reconstructed_text=reconstructed_text,
                        extracted_text=reconstructed_text,
                        char_count=len(reconstructed_text),
                    )
                    session.add(page)
                    session.flush()

                    self.job_progress_service.set_stage(
                        session,
                        job_kind=JobKind.INGESTION,
                        job=job,
                        stage_code="tokenizing",
                        progress_percent=self.job_progress_service.ranged_progress(
                            processed_pages + 1,
                            page_count,
                            start_percent=79,
                            end_percent=89,
                        ),
                        items_processed=processed_pages,
                        items_total=page_count,
                        append_event=False,
                    )
                    occurrences = self.occurrence_service.store_page_occurrences(
                        session,
                        document_id=document.id,
                        page_id=page.id,
                        page_number=page.page_number,
                        text=page.reconstructed_text or page.extracted_text,
                    )
                    index_service.apply_page_occurrences(
                        session,
                        user_id=document.user_id,
                        document_id=document.id,
                        document_title=document.title,
                        page_id=page.id,
                        occurrences=occurrences,
                    )

                    processed_pages += 1
                    self.job_progress_service.update_progress(
                        session,
                        job_kind=JobKind.INGESTION,
                        job=job,
                        progress_percent=self.job_progress_service.ranged_progress(
                            processed_pages,
                            page_count,
                            start_percent=80,
                            end_percent=90,
                        ),
                        items_processed=processed_pages,
                        items_total=page_count,
                    )

                    logger.info(
                        "Processed page %s/%s for document %s",
                        processed_pages,
                        page_count,
                        document.id,
                    )

            with session_scope() as session:
                job = self._load_job(session, job_uuid)
                document = job.document
                document.status = DocumentStatus.COMPLETED
                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.INGESTION,
                    job=job,
                    stage_code="saving_results",
                    progress_percent=92,
                    items_processed=processed_pages,
                    items_total=page_count,
                )
                self.job_progress_service.set_stage(
                    session,
                    job_kind=JobKind.INGESTION,
                    job=job,
                    stage_code="finalizing",
                    progress_percent=97,
                    items_processed=processed_pages,
                    items_total=page_count,
                )
                job.status = IngestionJobStatus.COMPLETED
                self.job_progress_service.complete(
                    session,
                    job_kind=JobKind.INGESTION,
                    job=job,
                )
                from app.services.document_workflow_service import get_document_workflow_service

                get_document_workflow_service().sync_for_document(
                    session,
                    document_id=document.id,
                    last_job_id=job.id,
                )

        except Exception as exc:
            logger.exception("Document ingestion failed for job %s", job_uuid)
            self._mark_failed(job_uuid, exc)
            raise

    def _mark_failed(self, job_id: UUID, exc: Exception) -> None:
        failure_info = self.ingestion_error_service.map_exception(exc)
        with session_scope() as session:
            job = session.scalar(
                select(IngestionJob)
                .options(joinedload(IngestionJob.document))
                .where(IngestionJob.id == job_id)
            )
            if job is None:
                return

            job.status = IngestionJobStatus.FAILED
            job.step = "failed"
            job.error_message = failure_info.error_message_user
            job.error_code = failure_info.error_code
            job.error_message_user = failure_info.error_message_user
            job.error_message_technical = failure_info.error_message_technical
            job.next_steps = failure_info.next_steps
            job.can_retry = failure_info.can_retry
            self.job_progress_service.fail(
                session,
                job_kind=JobKind.INGESTION,
                job=job,
                message_user=failure_info.error_message_user,
            )
            if job.document is not None:
                job.document.status = DocumentStatus.FAILED
                from app.services.document_workflow_service import get_document_workflow_service

                get_document_workflow_service().sync_for_document(
                    session,
                    document_id=job.document.id,
                    last_job_id=job.id,
                )

    @staticmethod
    def _load_job(session, job_id: UUID) -> IngestionJob:
        job = session.scalar(
            select(IngestionJob)
            .options(joinedload(IngestionJob.document))
            .where(IngestionJob.id == job_id)
        )
        if job is None:
            raise ValueError(f"Ingestion job {job_id} was not found.")
        if job.document is None:
            raise ValueError(f"Document for ingestion job {job_id} was not found.")
        return job

def get_ingestion_service() -> IngestionService:
    return IngestionService()
