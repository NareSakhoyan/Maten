from __future__ import annotations

from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from app.db.models import ReferenceImportMethod

from .base import ConservativeReferenceTextExtractor, ReferenceParseResult


WORD_NAMESPACE = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


class DocxReferenceParser:
    def __init__(self, extractor: ConservativeReferenceTextExtractor | None = None) -> None:
        self.extractor = extractor or ConservativeReferenceTextExtractor()

    def parse(self, content: bytes) -> ReferenceParseResult:
        try:
            with ZipFile(BytesIO(content)) as archive:
                document_xml = archive.read("word/document.xml")
        except Exception as exc:
            raise ValueError("Invalid DOCX reference import.") from exc

        root = ET.fromstring(document_xml)
        paragraphs: list[str] = []
        for paragraph in root.findall(".//w:p", WORD_NAMESPACE):
            text_parts = [node.text or "" for node in paragraph.findall(".//w:t", WORD_NAMESPACE)]
            paragraph_text = "".join(text_parts).strip()
            if paragraph_text:
                paragraphs.append(paragraph_text)

        return ReferenceParseResult(
            rows=self.extractor.entries_from_text_blocks(paragraphs),
            rows_read=len(paragraphs),
            import_method=ReferenceImportMethod.DOCX,
        )
