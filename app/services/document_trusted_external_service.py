from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

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
from app.services.external_lookup_service import ExternalLookupService, _as_utc, get_external_lookup_service


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

        latest_cache_subquery = self._latest_cache_subquery(
            provider_id=provider_id,
            normalized_forms=normalized_forms,
        )

        cache_by_form: dict[str, ExternalLookupCache] = {}
        cache_rows = session.scalars(
            select(ExternalLookupCache)
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

        cache_ids = [cache_row.id for cache_row in cache_rows]
        match_count_by_cache_id = self._match_count_by_cache_id(session, cache_ids=cache_ids)
        first_result_by_cache_id = self._first_result_by_cache_id(session, cache_ids=cache_ids)

        snapshots: dict[str, NayiriLookupSnapshot] = {}
        for normalized_form in normalized_forms:
            cache_row = cache_by_form.get(normalized_form)
            first_result = first_result_by_cache_id.get(cache_row.id) if cache_row is not None else None
            match_count = match_count_by_cache_id.get(cache_row.id, 0) if cache_row is not None else 0
            snapshots[normalized_form] = self._snapshot_from_cache(
                cache_row,
                provider_display_name=provider_display_name,
                first_result=first_result,
                match_count=match_count,
            )
        return snapshots

    def nayiri_found_normalized_forms(
        self,
        session: Session,
        *,
        normalized_forms: list[str],
    ) -> list[str]:
        if not normalized_forms:
            return []
        provider_row = session.scalar(
            select(ExternalProvider).where(ExternalProvider.key == NAYIRI_PROVIDER_KEY)
        )
        if provider_row is None or not provider_row.is_active:
            return []

        latest_cache_subquery = self._latest_cache_subquery(
            provider_id=provider_row.id,
            normalized_forms=normalized_forms,
        )
        now = _as_utc(self.external_lookup_service.now_fn())
        return list(
            session.scalars(
                select(ExternalLookupCache.normalized_query)
                .join(
                    latest_cache_subquery,
                    (ExternalLookupCache.normalized_query == latest_cache_subquery.c.normalized_query)
                    & (ExternalLookupCache.created_at == latest_cache_subquery.c.latest_created_at),
                )
                .where(
                    ExternalLookupCache.provider_id == provider_row.id,
                    ExternalLookupCache.user_id.is_(None),
                    ExternalLookupCache.search_mode == ExternalLookupSearchMode.NORMALIZED,
                    ExternalLookupCache.status == ExternalLookupStatus.COMPLETED,
                    (ExternalLookupCache.expires_at.is_(None) | (ExternalLookupCache.expires_at > now)),
                    exists(
                        select(ExternalLookupResult.id).where(
                            ExternalLookupResult.cache_id == ExternalLookupCache.id
                        )
                    ),
                )
                .distinct()
                .order_by(ExternalLookupCache.normalized_query.asc())
            ).all()
        )

    def nayiri_not_found_normalized_forms(
        self,
        session: Session,
        *,
        normalized_forms: list[str],
    ) -> list[str]:
        if not normalized_forms:
            return []
        provider_row = session.scalar(
            select(ExternalProvider).where(ExternalProvider.key == NAYIRI_PROVIDER_KEY)
        )
        if provider_row is None or not provider_row.is_active:
            return []

        latest_cache_subquery = self._latest_cache_subquery(
            provider_id=provider_row.id,
            normalized_forms=normalized_forms,
        )
        now = _as_utc(self.external_lookup_service.now_fn())
        return list(
            session.scalars(
                select(ExternalLookupCache.normalized_query)
                .join(
                    latest_cache_subquery,
                    (ExternalLookupCache.normalized_query == latest_cache_subquery.c.normalized_query)
                    & (ExternalLookupCache.created_at == latest_cache_subquery.c.latest_created_at),
                )
                .where(
                    ExternalLookupCache.provider_id == provider_row.id,
                    ExternalLookupCache.user_id.is_(None),
                    ExternalLookupCache.search_mode == ExternalLookupSearchMode.NORMALIZED,
                    ExternalLookupCache.status == ExternalLookupStatus.COMPLETED,
                    (ExternalLookupCache.expires_at.is_(None) | (ExternalLookupCache.expires_at > now)),
                    ~exists(
                        select(ExternalLookupResult.id).where(
                            ExternalLookupResult.cache_id == ExternalLookupCache.id
                        )
                    ),
                )
                .distinct()
                .order_by(ExternalLookupCache.normalized_query.asc())
            ).all()
        )

    def summarize_document(
        self,
        session: Session,
        *,
        user_id: UUID,
        document_id: UUID,
    ) -> dict[str, int]:
        provider_id = select(ExternalProvider.id).where(ExternalProvider.key == NAYIRI_PROVIDER_KEY).scalar_subquery()
        provider_is_active = (
            select(ExternalProvider.is_active).where(ExternalProvider.key == NAYIRI_PROVIDER_KEY).scalar_subquery()
        )
        forms_subquery = (
            select(Occurrence.normalized_token.label("normalized_form"))
            .join(Document, Occurrence.document_id == Document.id)
            .where(
                Document.user_id == user_id,
                Occurrence.document_id == document_id,
            )
            .distinct()
            .subquery()
        )
        latest_cache_subquery = (
            select(
                ExternalLookupCache.normalized_query.label("normalized_query"),
                func.max(ExternalLookupCache.created_at).label("latest_created_at"),
            )
            .join(forms_subquery, ExternalLookupCache.normalized_query == forms_subquery.c.normalized_form)
            .where(
                ExternalLookupCache.provider_id == provider_id,
                ExternalLookupCache.user_id.is_(None),
                ExternalLookupCache.search_mode == ExternalLookupSearchMode.NORMALIZED,
            )
            .group_by(ExternalLookupCache.normalized_query)
            .subquery()
        )
        now = _as_utc(self.external_lookup_service.now_fn())
        fresh_completed_cache = (
            ExternalLookupCache.provider_id == provider_id,
            ExternalLookupCache.user_id.is_(None),
            ExternalLookupCache.search_mode == ExternalLookupSearchMode.NORMALIZED,
            ExternalLookupCache.status == ExternalLookupStatus.COMPLETED,
            (ExternalLookupCache.expires_at.is_(None) | (ExternalLookupCache.expires_at > now)),
        )
        has_result = exists(
            select(ExternalLookupResult.id).where(
                ExternalLookupResult.cache_id == ExternalLookupCache.id,
            )
        )
        total_forms, found_count, not_found_count, is_provider_active = session.execute(
            select(
                func.count(forms_subquery.c.normalized_form),
                func.count(forms_subquery.c.normalized_form).filter(
                    *fresh_completed_cache,
                    has_result,
                ),
                func.count(forms_subquery.c.normalized_form).filter(
                    *fresh_completed_cache,
                    ~has_result,
                ),
                provider_is_active,
            )
            .select_from(forms_subquery)
            .outerjoin(
                latest_cache_subquery,
                forms_subquery.c.normalized_form == latest_cache_subquery.c.normalized_query,
            )
            .outerjoin(
                ExternalLookupCache,
                and_(
                    ExternalLookupCache.normalized_query == latest_cache_subquery.c.normalized_query,
                    ExternalLookupCache.created_at == latest_cache_subquery.c.latest_created_at,
                    ExternalLookupCache.provider_id == provider_id,
                    ExternalLookupCache.user_id.is_(None),
                    ExternalLookupCache.search_mode == ExternalLookupSearchMode.NORMALIZED,
                ),
            )
        ).one()
        total_forms = int(total_forms or 0)
        counts = {
            "found_count": 0,
            "not_found_count": 0,
            "unchecked_count": total_forms,
            "unavailable_count": 0,
            "total_forms": total_forms,
        }
        if total_forms == 0:
            return counts
        if is_provider_active is False:
            counts["unchecked_count"] = 0
            counts["unavailable_count"] = total_forms
            return counts
        if is_provider_active is None:
            return counts
        counts["found_count"] = int(found_count or 0)
        counts["not_found_count"] = int(not_found_count or 0)
        counts["unchecked_count"] = max(total_forms - counts["found_count"] - counts["not_found_count"], 0)
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

    @staticmethod
    def _latest_cache_subquery(*, provider_id: UUID, normalized_forms: list[str]):
        return (
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

    @staticmethod
    def _match_count_by_cache_id(session: Session, *, cache_ids: list[UUID]) -> dict[UUID, int]:
        if not cache_ids:
            return {}
        rows = session.execute(
            select(ExternalLookupResult.cache_id, func.count(ExternalLookupResult.id))
            .where(ExternalLookupResult.cache_id.in_(cache_ids))
            .group_by(ExternalLookupResult.cache_id)
        ).all()
        return {cache_id: int(count) for cache_id, count in rows}

    @staticmethod
    def _first_result_by_cache_id(
        session: Session,
        *,
        cache_ids: list[UUID],
    ) -> dict[UUID, ExternalLookupResult]:
        if not cache_ids:
            return {}
        result_rank = (
            func.row_number()
            .over(
                partition_by=ExternalLookupResult.cache_id,
                order_by=ExternalLookupResult.created_at.asc(),
            )
            .label("result_rank")
        )
        ranked_results = (
            select(
                ExternalLookupResult.id.label("result_id"),
                ExternalLookupResult.cache_id.label("cache_id"),
                result_rank,
            )
            .where(ExternalLookupResult.cache_id.in_(cache_ids))
            .subquery()
        )
        first_result_ids = list(
            session.scalars(
                select(ranked_results.c.result_id).where(ranked_results.c.result_rank == 1)
            ).all()
        )
        if not first_result_ids:
            return {}
        rows = session.scalars(
            select(ExternalLookupResult).where(ExternalLookupResult.id.in_(first_result_ids))
        ).all()
        return {row.cache_id: row for row in rows}

    def _snapshot_from_cache(
        self,
        cache_row: ExternalLookupCache | None,
        *,
        provider_display_name: str,
        first_result: ExternalLookupResult | None = None,
        match_count: int | None = None,
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

        resolved_match_count = match_count if match_count is not None else len(cache_row.results)
        first = first_result if first_result is not None else (cache_row.results[0] if cache_row.results else None)
        if first is not None and resolved_match_count > 0:
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
                match_count=resolved_match_count,
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
