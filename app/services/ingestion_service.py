from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import joinedload

from app.core.database import session_scope
from app.db.models import Document, DocumentPage, DocumentStatus, IngestionJob, IngestionJobStatus, Occurrence
from app.services.ingestion_error_service import IngestionErrorService, get_ingestion_error_service
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
    ) -> None:
        self.storage_service = storage_service or get_storage_service()
        self.page_extraction_service = page_extraction_service or get_page_extraction_service()
        self.occurrence_service = occurrence_service or get_occurrence_service()
        self.ingestion_error_service = ingestion_error_service or get_ingestion_error_service()

    def process_job(self, job_id: UUID | str) -> None:
        job_uuid = UUID(str(job_id))
        page_iterator = None
        page_count = 0

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

                document.status = DocumentStatus.PROCESSING

                session.execute(delete(Occurrence).where(Occurrence.document_id == document.id))
                session.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))

                original_bytes = self.storage_service.download_bytes(document.storage_bucket, document.storage_path)
                mime_type = detect_mime_type(document.original_filename, original_bytes, document.mime_type)
                page_count, page_iterator = self.page_extraction_service.iter_document_pages(original_bytes, mime_type)
                document.page_count = page_count
                job.step = "extracting_pages"

            if page_iterator is None:
                raise RuntimeError(f"Could not initialize extraction for job {job_uuid}.")

            processed_pages = 0
            for extracted_page in page_iterator:
                with session_scope() as session:
                    job = self._load_job(session, job_uuid)
                    document = job.document
                    reconstructed_text = reconstruct_page_text(extracted_page.extracted_text)

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

                    self.occurrence_service.store_page_occurrences(
                        session,
                        document_id=document.id,
                        page_id=page.id,
                        page_number=page.page_number,
                        text=page.reconstructed_text or page.extracted_text,
                    )

                    processed_pages += 1
                    job.step = f"processing_page_{processed_pages}_of_{page_count}"
                    job.progress_percent = self._progress(processed_pages, page_count)

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
                job.status = IngestionJobStatus.COMPLETED
                job.step = "completed"
                job.progress_percent = 100
                job.finished_at = datetime.now(timezone.utc)

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
            job.finished_at = datetime.now(timezone.utc)
            if job.document is not None:
                job.document.status = DocumentStatus.FAILED

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

    @staticmethod
    def _progress(processed_pages: int, page_count: int) -> int:
        if page_count <= 0:
            return 0
        return min(99, int((processed_pages / page_count) * 100))


def get_ingestion_service() -> IngestionService:
    return IngestionService()
