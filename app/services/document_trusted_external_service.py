from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Document,
    ExternalLookupCache,
    ExternalLookupResult,
    ExternalLookupSearchMode,
    ExternalLookupStatus,
    ExternalProvider,
    Occurrence,
)
from app.schemas.word import (
    DocumentTrustedExternalCanonicalizationStatus,
    DocumentTrustedExternalStatus,
    TrustedExternalLookupStatus,
)
from app.utils.text_normalization import normalize_token
from app.services.external_lookup_service import ExternalLookupService, get_external_lookup_service


NAYIRI_PROVIDER_KEY = "nayiri_web"


@dataclass(frozen=True, slots=True)
class NayiriLookupSnapshot:
    status: DocumentTrustedExternalStatus
    provider_display_name: str | None = None
    match_count: int = 0
    matched_form: str | None = None
    source_title: str | None = None
    reference_link: str | None = None
    snippet: str | None = None
    canonicalization_status: DocumentTrustedExternalCanonicalizationStatus = (
        DocumentTrustedExternalCanonicalizationStatus.UNRESOLVED
    )


class DocumentTrustedExternalService:
    def __init__(self, *, external_lookup_service: ExternalLookupService | None = None) -> None:
        self.external_lookup_service = external_lookup_service or get_external_lookup_service()

    def list_document_normalized_forms(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
    ) -> list[str]:
        return list(
            session.scalars(
                select(Occurrence.normalized_token)
                .join(Document, Occurrence.document_id == Document.id)
                .where(
                    Document.user_id == user_id,
                    Occurrence.document_id == document_id,
                )
                .distinct()
                .order_by(Occurrence.normalized_token.asc())
            ).all()
        )

    def nayiri_status_map(
        self,
        session: Session,
        *,
        normalized_forms: list[str],
    ) -> dict[str, NayiriLookupSnapshot]:
        if not normalized_forms:
            return {}

        if not self.external_lookup_service.settings.external_lookup_enabled:
            unavailable = NayiriLookupSnapshot(
                status=DocumentTrustedExternalStatus.UNAVAILABLE,
                provider_display_name="Nayiri",
            )
            return {form: unavailable for form in normalized_forms}

        if NAYIRI_PROVIDER_KEY not in self.external_lookup_service.providers:
            unavailable = NayiriLookupSnapshot(
                status=DocumentTrustedExternalStatus.UNAVAILABLE,
                provider_display_name="Nayiri",
            )
            return {form: unavailable for form in normalized_forms}

        provider_row = session.scalar(
            select(ExternalProvider).where(ExternalProvider.key == NAYIRI_PROVIDER_KEY)
        )
        if provider_row is None:
            unchecked = NayiriLookupSnapshot(
                status=DocumentTrustedExternalStatus.UNCHECKED,
                provider_display_name="Nayiri",
            )
            return {form: unchecked for form in normalized_forms}
        if not provider_row.is_active:
            unavailable = NayiriLookupSnapshot(
                status=DocumentTrustedExternalStatus.UNAVAILABLE,
                provider_display_name=provider_row.display_name,
            )
            return {form: unavailable for form in normalized_forms}

        provider_id = provider_row.id
        provider_display_name = provider_row.display_name

        latest_cache_subquery = (
            select(
                ExternalLookupCache.normalized_query.label("normalized_query"),
                func.max(ExternalLookupCache.created_at).label("latest_created_at"),
            )
            .where(
                ExternalLookupCache.provider_id == provider_id,
                ExternalLookupCache.user_id.is_(None),
                ExternalLookupCache.search_mode == ExternalLookupSearchMode.NORMALIZED,
                ExternalLookupCache.normalized_query.in_(normalized_forms),
            )
            .group_by(ExternalLookupCache.normalized_query)
            .subquery()
        )

        cache_by_form: dict[str, ExternalLookupCache] = {}
        cache_rows = session.scalars(
            select(ExternalLookupCache)
            .options(selectinload(ExternalLookupCache.results))
            .join(
                latest_cache_subquery,
                (ExternalLookupCache.normalized_query == latest_cache_subquery.c.normalized_query)
                & (ExternalLookupCache.created_at == latest_cache_subquery.c.latest_created_at),
            )
            .where(
                ExternalLookupCache.provider_id == provider_id,
                ExternalLookupCache.user_id.is_(None),
                ExternalLookupCache.search_mode == ExternalLookupSearchMode.NORMALIZED,
            )
        ).all()
        for cache_row in cache_rows:
            cache_by_form[cache_row.normalized_query] = cache_row

        snapshots: dict[str, NayiriLookupSnapshot] = {}
        for normalized_form in normalized_forms:
            cache_row = cache_by_form.get(normalized_form)
            snapshots[normalized_form] = self._snapshot_from_cache(
                cache_row,
                provider_display_name=provider_display_name,
            )
        return snapshots

    def summarize_document(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
    ) -> dict[str, int]:
        forms = self.list_document_normalized_forms(session, user_id=user_id, document_id=document_id)
        status_map = self.nayiri_status_map(session, normalized_forms=forms)
        counts = {
            "found_count": 0,
            "not_found_count": 0,
            "unchecked_count": 0,
            "unavailable_count": 0,
            "total_forms": len(forms),
        }
        for snapshot in status_map.values():
            if snapshot.status is DocumentTrustedExternalStatus.FOUND:
                counts["found_count"] += 1
            elif snapshot.status is DocumentTrustedExternalStatus.NOT_FOUND:
                counts["not_found_count"] += 1
            elif snapshot.status is DocumentTrustedExternalStatus.UNAVAILABLE:
                counts["unavailable_count"] += 1
            else:
                counts["unchecked_count"] += 1
        return counts

    def combined_has_reference_match(
        self,
        *,
        imported_has_match: bool,
        nayiri_snapshot: NayiriLookupSnapshot,
    ) -> bool:
        return imported_has_match or nayiri_snapshot.status is DocumentTrustedExternalStatus.FOUND

    def needs_nayiri_lookup(
        self,
        session: Session,
        *,
        normalized_form: str,
    ) -> bool:
        if not self.external_lookup_service.settings.external_lookup_enabled:
            return False
        if NAYIRI_PROVIDER_KEY not in self.external_lookup_service.providers:
            return False

        provider_row = session.scalar(
            select(ExternalProvider).where(ExternalProvider.key == NAYIRI_PROVIDER_KEY)
        )
        if provider_row is None:
            return True

        cached = self.external_lookup_service._latest_cache(  # noqa: SLF001
            session,
            provider_id=provider_row.id,
            normalized_query=normalized_form,
            search_mode=ExternalLookupSearchMode.NORMALIZED,
        )
        if cached is None:
            return True
        return not self.external_lookup_service._cache_is_fresh(cached)  # noqa: SLF001

    def _snapshot_from_cache(
        self,
        cache_row: ExternalLookupCache | None,
        *,
        provider_display_name: str,
    ) -> NayiriLookupSnapshot:
        if cache_row is None:
            return NayiriLookupSnapshot(
                status=DocumentTrustedExternalStatus.UNCHECKED,
                provider_display_name=provider_display_name,
            )

        if not self.external_lookup_service._cache_is_fresh(cache_row):  # noqa: SLF001
            return NayiriLookupSnapshot(
                status=DocumentTrustedExternalStatus.UNCHECKED,
                provider_display_name=provider_display_name,
            )

        if cache_row.status is ExternalLookupStatus.FAILED:
            return NayiriLookupSnapshot(
                status=DocumentTrustedExternalStatus.UNAVAILABLE,
                provider_display_name=provider_display_name,
            )

        if cache_row.results:
            first = cache_row.results[0]
            metadata = first.metadata_json or {}
            query_text = (cache_row.query_text or "").strip()
            matched_form = (first.matched_form or "").strip()
            query_normalized = normalize_token(query_text)
            matched_normalized = normalize_token(matched_form)
            canonicalization_status = DocumentTrustedExternalCanonicalizationStatus.UNRESOLVED
            if metadata.get("morphology_fallback"):
                canonicalization_status = DocumentTrustedExternalCanonicalizationStatus.MORPHOLOGY_ASSISTED
            elif query_text and matched_form and query_text == matched_form:
                canonicalization_status = DocumentTrustedExternalCanonicalizationStatus.DIRECT_MATCH
            elif query_normalized and matched_normalized and query_normalized == matched_normalized:
                canonicalization_status = DocumentTrustedExternalCanonicalizationStatus.CANONICALIZED_BY_NAYIRI

            return NayiriLookupSnapshot(
                status=DocumentTrustedExternalStatus.FOUND,
                provider_display_name=provider_display_name,
                match_count=len(cache_row.results),
                matched_form=first.matched_form,
                source_title=first.source_title,
                reference_link=first.reference_link,
                snippet=first.snippet,
                canonicalization_status=canonicalization_status,
            )

        return NayiriLookupSnapshot(
            status=DocumentTrustedExternalStatus.NOT_FOUND,
            provider_display_name=provider_display_name,
            match_count=0,
        )

    @staticmethod
    def map_lookup_batch_status(status: TrustedExternalLookupStatus) -> DocumentTrustedExternalStatus:
        if status is TrustedExternalLookupStatus.COMPLETED:
            return DocumentTrustedExternalStatus.FOUND
        if status is TrustedExternalLookupStatus.NO_RESULTS:
            return DocumentTrustedExternalStatus.NOT_FOUND
        return DocumentTrustedExternalStatus.UNAVAILABLE


def get_document_trusted_external_service() -> DocumentTrustedExternalService:
    return DocumentTrustedExternalService()
