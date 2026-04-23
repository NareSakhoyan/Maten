from __future__ import annotations

import enum


class SupportedReferenceImportMethod(str, enum.Enum):
    TXT = "txt"
    CSV = "csv"
    DOCX = "docx"
    PDF_TEXT = "pdf_text"
    PDF_OCR = "pdf_ocr"
