from __future__ import annotations

from app.db.models import ReferenceImportMethod

from .base import ConservativeReferenceTextExtractor, ReferenceParseResult


class TxtReferenceParser:
    def __init__(self, extractor: ConservativeReferenceTextExtractor | None = None) -> None:
        self.extractor = extractor or ConservativeReferenceTextExtractor()

    def parse(self, content: bytes) -> ReferenceParseResult:
        text = content.decode("utf-8-sig")
        lines = text.splitlines()
        return ReferenceParseResult(
            rows=self.extractor.entries_from_text_blocks(lines, split_simple_separators=False),
            rows_read=len(lines),
            import_method=ReferenceImportMethod.TXT,
        )
