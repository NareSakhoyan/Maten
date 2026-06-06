from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from io import BytesIO

import fitz
from PIL import Image, ImageOps

from app.core.config import Settings, get_settings
from app.db.models import ExtractionMethod
from app.services.ocr_service import OCRService, get_ocr_service
from app.utils.mime import is_image_mime, is_pdf_mime
from app.utils.text_normalization import normalize_extracted_text


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_number: int
    extraction_method: ExtractionMethod
    extracted_text: str
    char_count: int
    page_image_png: bytes | None = None


class PageExtractionService:
    def __init__(self, settings: Settings | None = None, ocr_service: OCRService | None = None) -> None:
        self.settings = settings or get_settings()
        self.ocr_service = ocr_service or get_ocr_service()

    def iter_document_pages(
        self,
        file_bytes: bytes,
        mime_type: str,
        *,
        start_page: int = 1,
    ) -> tuple[int, Generator[ExtractedPage, None, None]]:
        if is_pdf_mime(mime_type):
            return self._iter_pdf_pages(file_bytes, start_page=start_page)
        if is_image_mime(mime_type):
            return self._iter_image_page(file_bytes)
        raise ValueError(f"Unsupported file type for extraction: {mime_type}")

    def _iter_pdf_pages(
        self,
        file_bytes: bytes,
        *,
        start_page: int = 1,
    ) -> tuple[int, Generator[ExtractedPage, None, None]]:
        pdf = fitz.open(stream=file_bytes, filetype="pdf")
        page_count = pdf.page_count
        start_index = min(max(start_page, 1), page_count + 1) - 1

        def generator() -> Generator[ExtractedPage, None, None]:
            try:
                for index in range(start_index, page_count):
                    page = pdf.load_page(index)
                    yield self._extract_pdf_page(page, index + 1)
            finally:
                pdf.close()

        return page_count, generator()

    def _iter_image_page(self, file_bytes: bytes) -> tuple[int, Generator[ExtractedPage, None, None]]:
        def generator() -> Generator[ExtractedPage, None, None]:
            page_png = self._normalize_image_to_png(file_bytes)
            text = self.ocr_service.image_to_text(page_png)
            yield ExtractedPage(
                page_number=1,
                extraction_method=ExtractionMethod.OCR,
                extracted_text=text,
                char_count=len(text),
                page_image_png=page_png,
            )

        return 1, generator()

    def _extract_pdf_page(self, page: fitz.Page, page_number: int) -> ExtractedPage:
        raw_text = normalize_extracted_text(page.get_text("text", sort=True) or "")
        if self._has_usable_text_layer(raw_text):
            return ExtractedPage(
                page_number=page_number,
                extraction_method=ExtractionMethod.PDF_TEXT,
                extracted_text=raw_text,
                char_count=len(raw_text),
            )

        rendered_png = self._render_pdf_page_to_png(page)
        ocr_text = self.ocr_service.image_to_text(rendered_png)
        return ExtractedPage(
            page_number=page_number,
            extraction_method=ExtractionMethod.OCR,
            extracted_text=ocr_text,
            char_count=len(ocr_text),
            page_image_png=rendered_png,
        )

    def _render_pdf_page_to_png(self, page: fitz.Page) -> bytes:
        scale = self.settings.ocr_dpi / 72
        matrix = fitz.Matrix(scale, scale)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        return pixmap.tobytes("png")

    @staticmethod
    def _normalize_image_to_png(file_bytes: bytes) -> bytes:
        with Image.open(BytesIO(file_bytes)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()

    @staticmethod
    def _has_usable_text_layer(text: str) -> bool:
        if not text.strip():
            return False

        alpha_count = sum(char.isalpha() for char in text)
        return alpha_count >= 3


def get_page_extraction_service() -> PageExtractionService:
    return PageExtractionService()

