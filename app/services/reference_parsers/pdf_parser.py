from __future__ import annotations

from io import BytesIO

import fitz
from PIL import Image, ImageOps

from app.core.config import Settings, get_settings
from app.db.models import ReferenceImportMethod
from app.services.ocr_service import OCRService, get_ocr_service
from app.utils.text_normalization import normalize_extracted_text

from .base import ConservativeReferenceTextExtractor, ReferenceParseResult


OCR_WARNING_MESSAGE = "This reference source was imported from scanned PDF using OCR. Matches may contain OCR noise."


class PdfReferenceParser:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        ocr_service: OCRService | None = None,
        extractor: ConservativeReferenceTextExtractor | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.ocr_service = ocr_service or get_ocr_service()
        self.extractor = extractor or ConservativeReferenceTextExtractor(self.settings)

    def parse(self, content: bytes) -> ReferenceParseResult:
        pdf = fitz.open(stream=content, filetype="pdf")
        try:
            direct_lines: list[str] = []
            direct_text_char_count = 0
            for index in range(pdf.page_count):
                page = pdf.load_page(index)
                page_text = normalize_extracted_text(page.get_text("text", sort=True) or "")
                if page_text:
                    direct_text_char_count += len(page_text)
                    direct_lines.extend(page_text.splitlines())

            if self._has_usable_direct_text(direct_text_char_count):
                return ReferenceParseResult(
                    rows=self.extractor.entries_from_text_blocks(direct_lines),
                    rows_read=len(direct_lines),
                    import_method=ReferenceImportMethod.PDF_TEXT,
                )

            ocr_lines: list[str] = []
            for index in range(pdf.page_count):
                page = pdf.load_page(index)
                rendered_png = self._render_pdf_page_to_png(page)
                ocr_text = self.ocr_service.image_to_text(rendered_png)
                if ocr_text:
                    ocr_lines.extend(ocr_text.splitlines())

            return ReferenceParseResult(
                rows=self.extractor.entries_from_text_blocks(ocr_lines, split_simple_separators=False),
                rows_read=len(ocr_lines),
                import_method=ReferenceImportMethod.PDF_OCR,
                warning_message=OCR_WARNING_MESSAGE,
            )
        finally:
            pdf.close()

    def _render_pdf_page_to_png(self, page: fitz.Page) -> bytes:
        scale = self.settings.ocr_dpi / 72
        matrix = fitz.Matrix(scale, scale)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        raw_png = pixmap.tobytes("png")
        with Image.open(BytesIO(raw_png)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()

    def _has_usable_direct_text(self, char_count: int) -> bool:
        return char_count >= self.settings.reference_pdf_text_min_length
