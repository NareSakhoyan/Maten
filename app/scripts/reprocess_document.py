from __future__ import annotations

import argparse
from uuid import UUID

from sqlalchemy import delete, select, update

from app.core.database import session_scope
from app.db.models import Document, DocumentPage, DocumentStatus, LexemeForm, Occurrence
from app.services.occurrence_service import OccurrenceService
from app.services.page_extraction_service import PageExtractionService
from app.services.storage_service import StorageService
from app.utils.mime import detect_mime_type
from app.utils.text_reconstruction import reconstruct_page_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Reprocess document page text and occurrences.")
    parser.add_argument("document_id", help="Document UUID to rebuild from source storage.")
    args = parser.parse_args()

    document_id = UUID(args.document_id)
    storage_service = StorageService()
    page_extraction_service = PageExtractionService()
    occurrence_service = OccurrenceService()

    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is None:
            raise SystemExit(f"Document {document_id} was not found.")

        document.status = DocumentStatus.PROCESSING
        original_bytes = storage_service.download_bytes(document.storage_bucket, document.storage_path)
        mime_type = detect_mime_type(document.original_filename, original_bytes, document.mime_type)
        page_count, page_iterator = page_extraction_service.iter_document_pages(original_bytes, mime_type)

        from app.services.lexicon_group_index_service import get_lexicon_group_index_service

        index_service = get_lexicon_group_index_service()
        index_service.clear_document_index(
            session,
            user_id=document.user_id,
            document_id=document.id,
        )
        session.execute(delete(Occurrence).where(Occurrence.document_id == document.id))
        session.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))
        session.flush()

        for extracted_page in page_iterator:
            reconstructed_text = reconstruct_page_text(extracted_page.extracted_text)
            page = DocumentPage(
                document_id=document.id,
                page_number=extracted_page.page_number,
                extraction_method=extracted_page.extraction_method,
                page_image_bucket=None,
                page_image_path=None,
                raw_extracted_text=extracted_page.extracted_text,
                reconstructed_text=reconstructed_text,
                extracted_text=reconstructed_text,
                char_count=len(reconstructed_text),
            )
            session.add(page)
            session.flush()

            occurrences = occurrence_service.store_page_occurrences(
                session,
                document_id=document.id,
                page_id=page.id,
                page_number=page.page_number,
                text=reconstructed_text,
            )
            index_service.apply_page_occurrences(
                session,
                user_id=document.user_id,
                document_id=document.id,
                document_title=document.title,
                page_id=page.id,
                occurrences=occurrences,
            )

        document.page_count = page_count
        document.status = DocumentStatus.COMPLETED
        session.flush()

        lexeme_links = session.execute(
            select(LexemeForm.normalized_form, LexemeForm.lexeme_id).where(LexemeForm.user_id == str(document.user_id))
        ).all()
        for normalized_form, lexeme_id in lexeme_links:
            session.execute(
                update(Occurrence)
                .where(
                    Occurrence.document_id == document.id,
                    Occurrence.normalized_token == normalized_form,
                )
                .values(lexeme_id=lexeme_id)
            )

        session.commit()

    print(f"Reprocessed document {document_id}.")


if __name__ == "__main__":
    main()
