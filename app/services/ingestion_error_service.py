from __future__ import annotations

from dataclasses import dataclass

import pytesseract
from sqlalchemy.exc import SQLAlchemyError


@dataclass(frozen=True, slots=True)
class IngestionFailureInfo:
    error_code: str
    error_message_user: str
    error_message_technical: str
    next_steps: list[str]
    can_retry: bool


class IngestionRetryError(Exception):
    def __init__(self, *, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class IngestionErrorService:
    def map_exception(self, exc: Exception) -> IngestionFailureInfo:
        technical = f"{type(exc).__name__}: {exc}"
        message = str(exc).lower()

        if "unsupported file type" in message:
            return self._info(
                "unsupported_file_type",
                "This file type is not supported for ingestion.",
                technical,
                [
                    "Upload a PDF or supported image file.",
                    "If you believe this file should work, contact the administrator.",
                ],
                can_retry=False,
            )
        if isinstance(exc, FileNotFoundError):
            return self._info(
                "file_missing",
                "The original uploaded file could not be found.",
                technical,
                [
                    "Re-upload the document and try again.",
                    "If this keeps happening, contact the administrator.",
                ],
                can_retry=False,
            )
        if "download" in message and "not found" in message:
            return self._info(
                "file_missing",
                "The original uploaded file could not be found.",
                technical,
                [
                    "Re-upload the document and try again.",
                    "If this keeps happening, contact the administrator.",
                ],
                can_retry=False,
            )
        if "download" in message or "storage" in message:
            return self._info(
                "storage_download_failed",
                "The uploaded file could not be downloaded for processing.",
                technical,
                [
                    "Retry the job.",
                    "If it fails again, re-upload the document or contact the administrator.",
                ],
                can_retry=True,
            )
        if isinstance(exc, pytesseract.TesseractNotFoundError) or "tessdata" in message or "tesseract" in message:
            return self._info(
                "tessdata_missing",
                "OCR is temporarily unavailable on the server.",
                technical,
                [
                    "Retry the job in a few minutes.",
                    "If the problem continues, contact the administrator.",
                ],
                can_retry=True,
            )
        if "decode image" in message or "ocr" in message:
            return self._info(
                "ocr_failed",
                "OCR could not be completed for this document.",
                technical,
                [
                    "Retry the job.",
                    "If it fails again, upload a clearer scan or contact the administrator.",
                ],
                can_retry=True,
            )
        if "pdf" in message and ("open" in message or "cannot" in message or "failed" in message):
            return self._info(
                "pdf_open_failed",
                "This PDF could not be opened for processing.",
                technical,
                [
                    "Retry the job.",
                    "If it fails again, re-save the PDF and upload it again.",
                ],
                can_retry=True,
            )
        if isinstance(exc, SQLAlchemyError):
            return self._info(
                "database_write_failed",
                "The document could not be saved due to a server database error.",
                technical,
                [
                    "Retry the job.",
                    "If it fails again, contact the administrator.",
                ],
                can_retry=True,
            )
        if "forbidden" in message or "permission" in message:
            return self._info(
                "auth_forbidden",
                "The server could not access a required resource for this job.",
                technical,
                [
                    "Retry the job.",
                    "If it fails again, contact the administrator.",
                ],
                can_retry=False,
            )
        if "job" in message and "not found" in message:
            return self._info(
                "job_state_invalid",
                "This ingestion job is no longer in a valid state.",
                technical,
                [
                    "Refresh the page and try again.",
                    "If the problem continues, contact the administrator.",
                ],
                can_retry=False,
            )
        return self._info(
            "unknown_ingestion_error",
            "The document could not be processed due to an unexpected error.",
            technical,
            [
                "Retry the job.",
                "If it fails again, contact the administrator.",
            ],
            can_retry=True,
        )

    @staticmethod
    def _info(
        error_code: str,
        error_message_user: str,
        error_message_technical: str,
        next_steps: list[str],
        *,
        can_retry: bool,
    ) -> IngestionFailureInfo:
        return IngestionFailureInfo(
            error_code=error_code,
            error_message_user=error_message_user,
            error_message_technical=error_message_technical,
            next_steps=next_steps,
            can_retry=can_retry,
        )


def get_ingestion_error_service() -> IngestionErrorService:
    return IngestionErrorService()
