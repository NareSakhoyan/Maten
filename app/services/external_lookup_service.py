from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    ExternalLookupCache,
    ExternalLookupSearchMode,
    ExternalLookupStatus,
    ExternalLookupResult,
    ExternalProvider,
)
from app.schemas.word import TrustedExternalLookupStatus, WordSearchMode
from app.services.external_sources.base import (
    ExternalEvidenceItem,
    ExternalLookupProvider,
    ExternalLookupProviderError,
)
from app.services.external_sources.nayiri_provider import NayiriProvider
from app.utils.text_normalization import normalize_token


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(slots=True)
class ExternalLookupBatch:
    items: list[ExternalEvidenceItem]
    status: TrustedExternalLookupStatus


class ExternalLookupService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        providers: Iterable[ExternalLookupProvider] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        provider_list = list(providers) if providers is not None else self._default_providers(self.settings)
        self.providers = {provider.provider_key(): provider for provider in provider_list}
        self._known_provider_keys = {"nayiri_web", *self.providers.keys()}
        if len(self.providers) != len(provider_list):
            raise ValueError("Trusted external providers must have unique provider keys.")
        self.now_fn = now_fn or _utc_now

    def lookup(
        self,
        session: Session,
        *,
        user_id: UUID | None,
        query: str,
        mode: WordSearchMode,
        provider_keys: list[str] | None = None,
    ) -> ExternalLookupBatch:
        normalized_query = normalize_token(query)
        if not normalized_query:
            raise ValueError("q must not be empty.")

        providers = self._resolve_provider_rows(session, provider_keys=provider_keys)
        if not providers:
            return ExternalLookupBatch(items=[], status=TrustedExternalLookupStatus.UNAVAILABLE)

        items: list[ExternalEvidenceItem] = []
        provider_statuses: list[TrustedExternalLookupStatus] = []
        search_mode = self._search_mode(mode)
        for provider_row in providers:
            cached = self._latest_cache(
                session,
                provider_id=provider_row.id,
                normalized_query=normalized_query,
                search_mode=search_mode,
            )
            if cached is not None and self._cache_is_fresh(cached):
                batch = self._batch_from_cache(cached, provider_row=provider_row)
            else:
                provider = self.providers.get(provider_row.key)
                if provider is None or not self.settings.external_lookup_enabled:
                    batch = ExternalLookupBatch(items=[], status=TrustedExternalLookupStatus.UNAVAILABLE)
                else:
                    batch = self._fetch_and_cache(
                        session,
                        provider_row=provider_row,
                        provider=provider,
                        query_text=query.strip(),
                        normalized_query=normalized_query,
                        mode=mode,
                        user_id=user_id,
                    )
            provider_statuses.append(batch.status)
            items.extend(batch.items)

        deduped_items = {
            (
                item.provider_key,
                item.matched_form,
                item.reference_link or "",
                item.source_title or "",
            ): item
            for item in items
        }
        return ExternalLookupBatch(
            items=sorted(
                deduped_items.values(),
                key=lambda item: (
                    item.provider_display_name,
                    item.source_title or "",
                    item.matched_form,
                    item.reference_link or "",
                ),
            ),
            status=self._aggregate_status(provider_statuses, items=list(deduped_items.values())),
        )

    def lookup_cached(
        self,
        session: Session,
        *,
        query: str,
        mode: WordSearchMode,
        provider_keys: list[str] | None = None,
    ) -> ExternalLookupBatch:
        """Return trusted external evidence from the persistent cache without network I/O."""
        normalized_query = normalize_token(query)
        if not normalized_query:
            raise ValueError("q must not be empty.")

        providers = self._resolve_existing_provider_rows(session, provider_keys=provider_keys)
        if not providers:
            return ExternalLookupBatch(items=[], status=TrustedExternalLookupStatus.UNAVAILABLE)

        items: list[ExternalEvidenceItem] = []
        statuses: list[TrustedExternalLookupStatus] = []
        search_mode = self._search_mode(mode)
        for provider_row in providers:
            cached = self._latest_cache(
                session,
                provider_id=provider_row.id,
                normalized_query=normalized_query,
                search_mode=search_mode,
            )
            if cached is None or not self._cache_is_fresh(cached):
                statuses.append(TrustedExternalLookupStatus.UNAVAILABLE)
                continue
            batch = self._batch_from_cache(cached, provider_row=provider_row)
            statuses.append(batch.status)
            items.extend(batch.items)

        deduped_items = {
            (
                item.provider_key,
                item.matched_form,
                item.reference_link or "",
                item.source_title or "",
            ): item
            for item in items
        }
        return ExternalLookupBatch(
            items=sorted(
                deduped_items.values(),
                key=lambda item: (
                    item.provider_display_name,
                    item.source_title or "",
                    item.matched_form,
                    item.reference_link or "",
                ),
            ),
            status=self._aggregate_status(statuses, items=list(deduped_items.values())),
        )

    def _resolve_provider_rows(
        self,
        session: Session,
        *,
        provider_keys: list[str] | None,
    ) -> list[ExternalProvider]:
        requested_keys = list(dict.fromkeys(provider_keys or self.providers.keys()))
        unknown_keys = [key for key in requested_keys if key not in self._known_provider_keys]
        if unknown_keys:
            raise ValueError(f"Unknown trusted external provider keys: {', '.join(sorted(unknown_keys))}.")
        if not requested_keys:
            return []

        existing_rows = {
            row.key: row
            for row in session.scalars(
                select(ExternalProvider).where(ExternalProvider.key.in_(requested_keys))
            )
        }
        resolved_rows: list[ExternalProvider] = []
        for key in requested_keys:
            provider = self.providers.get(key)
            row = existing_rows.get(key)
            if provider is not None:
                if row is None:
                    row = ExternalProvider(
                        key=provider.provider_key(),
                        display_name=provider.provider_display_name(),
                        is_active=True,
                    )
                    session.add(row)
                    session.flush()
                    existing_rows[key] = row
                elif row.display_name != provider.provider_display_name():
                    row.display_name = provider.provider_display_name()
                if row.is_active:
                    resolved_rows.append(row)
                continue

            if row is not None and row.is_active:
                resolved_rows.append(row)
        return resolved_rows

    def _resolve_existing_provider_rows(
        self,
        session: Session,
        *,
        provider_keys: list[str] | None,
    ) -> list[ExternalProvider]:
        requested_keys = list(dict.fromkeys(provider_keys or self._known_provider_keys))
        unknown_keys = [key for key in requested_keys if key not in self._known_provider_keys]
        if unknown_keys:
            raise ValueError(f"Unknown trusted external provider keys: {', '.join(sorted(unknown_keys))}.")
        if not requested_keys:
            return []
        return list(
            session.scalars(
                select(ExternalProvider)
                .where(
                    ExternalProvider.key.in_(requested_keys),
                    ExternalProvider.is_active.is_(True),
                )
                .order_by(ExternalProvider.display_name.asc(), ExternalProvider.key.asc())
            )
        )

    @staticmethod
    def _default_providers(settings: Settings) -> list[ExternalLookupProvider]:
        providers: list[ExternalLookupProvider] = []
        if settings.nayiri_provider_enabled:
            providers.append(NayiriProvider(settings=settings))
        return providers

    @staticmethod
    def _search_mode(mode: WordSearchMode) -> ExternalLookupSearchMode:
        return ExternalLookupSearchMode(mode.value)

    def _latest_cache(
        self,
        session: Session,
        *,
        provider_id,
        normalized_query: str,
        search_mode: ExternalLookupSearchMode,
    ) -> ExternalLookupCache | None:
        return session.scalar(
            select(ExternalLookupCache)
            .where(
                ExternalLookupCache.provider_id == provider_id,
                ExternalLookupCache.user_id.is_(None),
                ExternalLookupCache.normalized_query == normalized_query,
                ExternalLookupCache.search_mode == search_mode,
            )
            .order_by(ExternalLookupCache.created_at.desc(), ExternalLookupCache.id.desc())
        )

    def _cache_is_fresh(self, cache_row: ExternalLookupCache) -> bool:
        expires_at = _as_utc(cache_row.expires_at)
        return expires_at is None or expires_at > _as_utc(self.now_fn())

    def _batch_from_cache(
        self,
        cache_row: ExternalLookupCache,
        *,
        provider_row: ExternalProvider,
    ) -> ExternalLookupBatch:
        items: list[ExternalEvidenceItem] = []
        for result_row in cache_row.results:
            items.append(
                ExternalEvidenceItem(
                    provider_key=provider_row.key,
                    provider_display_name=provider_row.display_name,
                    matched_form=result_row.matched_form,
                    normalized_form=result_row.normalized_form,
                    source_title=result_row.source_title,
                    source_subtitle=result_row.source_subtitle,
                    snippet=result_row.snippet,
                    reference_link=result_row.reference_link,
                    match_type=result_row.match_type,
                    match_score=float(result_row.match_score) if result_row.match_score is not None else None,
                    metadata_json=result_row.metadata_json,
                    fetched_at=_as_utc(cache_row.fetched_at),
                    created_at=_as_utc(result_row.created_at),
                )
            )
        return ExternalLookupBatch(
            items=items,
            status=self._status_for_cache(cache_row),
        )

    def _fetch_and_cache(
        self,
        session: Session,
        *,
        provider_row: ExternalProvider,
        provider: ExternalLookupProvider,
        query_text: str,
        normalized_query: str,
        mode: WordSearchMode,
        user_id: UUID | None,
    ) -> ExternalLookupBatch:
        fetched_at = _as_utc(self.now_fn()) or _utc_now()
        expires_at = self._expires_at(fetched_at)

        try:
            raw_items = provider.search_word(
                query=query_text,
                normalized_query=normalized_query,
                mode=mode,
            )
        except ExternalLookupProviderError as exc:
            logger.warning(
                "Trusted external provider lookup failed: provider=%s query=%s error=%s",
                provider_row.key,
                normalized_query,
                str(exc),
            )
            session.add(
                ExternalLookupCache(
                    user_id=None,
                    provider_id=provider_row.id,
                    query_text=query_text,
                    normalized_query=normalized_query,
                    search_mode=self._search_mode(mode),
                    status=ExternalLookupStatus.FAILED,
                    fetched_at=fetched_at,
                    expires_at=expires_at,
                )
            )
            self._safe_commit(session)
            return ExternalLookupBatch(items=[], status=TrustedExternalLookupStatus.UNAVAILABLE)

        items = self._sanitize_items(
            raw_items,
            provider_row=provider_row,
            fetched_at=fetched_at,
        )
        cache_row = ExternalLookupCache(
            user_id=None,
            provider_id=provider_row.id,
            query_text=query_text,
            normalized_query=normalized_query,
            search_mode=self._search_mode(mode),
            status=ExternalLookupStatus.COMPLETED,
            fetched_at=fetched_at,
            expires_at=expires_at,
        )
        session.add(cache_row)
        session.flush()

        persisted_rows: list[ExternalLookupResult] = []
        for item in items:
            row = ExternalLookupResult(
                cache_id=cache_row.id,
                provider_id=provider_row.id,
                matched_form=item.matched_form,
                normalized_form=item.normalized_form,
                source_title=item.source_title,
                source_subtitle=item.source_subtitle,
                snippet=item.snippet,
                reference_link=item.reference_link,
                metadata_json=item.metadata_json,
                match_type=item.match_type,
                match_score=item.match_score,
            )
            session.add(row)
            persisted_rows.append(row)
        session.flush()

        if not self._safe_commit(session):
            return ExternalLookupBatch(
                items=items,
                status=TrustedExternalLookupStatus.COMPLETED if items else TrustedExternalLookupStatus.NO_RESULTS,
            )

        return ExternalLookupBatch(
            items=[
                replace(
                    item,
                    created_at=_as_utc(row.created_at) or fetched_at,
                )
                for item, row in zip(items, persisted_rows, strict=False)
            ],
            status=TrustedExternalLookupStatus.COMPLETED if items else TrustedExternalLookupStatus.NO_RESULTS,
        )

    def _sanitize_items(
        self,
        items: list[ExternalEvidenceItem],
        *,
        provider_row: ExternalProvider,
        fetched_at: datetime,
    ) -> list[ExternalEvidenceItem]:
        sanitized: list[ExternalEvidenceItem] = []
        seen: set[tuple[str, str, str, str]] = set()
        for item in items:
            matched_form = item.matched_form.strip()
            normalized_form = normalize_token(item.normalized_form or matched_form) or None
            source_title = self._clean_optional(item.source_title)
            source_subtitle = self._clean_optional(item.source_subtitle)
            snippet = self._clean_optional(item.snippet)
            reference_link = self._clean_optional(item.reference_link)

            if not matched_form:
                continue
            if not self._is_traceable(
                source_title=source_title,
                snippet=snippet,
                reference_link=reference_link,
            ):
                continue

            dedupe_key = (
                provider_row.key,
                normalized_form or matched_form,
                source_title or "",
                reference_link or "",
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            sanitized.append(
                ExternalEvidenceItem(
                    provider_key=provider_row.key,
                    provider_display_name=provider_row.display_name,
                    matched_form=matched_form,
                    normalized_form=normalized_form,
                    source_title=source_title,
                    source_subtitle=source_subtitle,
                    snippet=snippet,
                    reference_link=reference_link,
                    match_type=item.match_type,
                    match_score=float(item.match_score) if item.match_score is not None else None,
                    metadata_json=item.metadata_json,
                    fetched_at=fetched_at,
                    created_at=item.created_at,
                )
            )
        return sanitized

    def _expires_at(self, fetched_at: datetime) -> datetime | None:
        ttl_hours = self.settings.external_lookup_cache_ttl_hours
        if ttl_hours <= 0:
            return None
        return fetched_at + timedelta(hours=ttl_hours)

    @staticmethod
    def _status_for_cache(cache_row: ExternalLookupCache) -> TrustedExternalLookupStatus:
        if cache_row.status is ExternalLookupStatus.FAILED:
            return TrustedExternalLookupStatus.UNAVAILABLE
        if cache_row.results:
            return TrustedExternalLookupStatus.COMPLETED
        return TrustedExternalLookupStatus.NO_RESULTS

    @staticmethod
    def _aggregate_status(
        statuses: list[TrustedExternalLookupStatus],
        *,
        items: list[ExternalEvidenceItem],
    ) -> TrustedExternalLookupStatus:
        if items:
            return TrustedExternalLookupStatus.COMPLETED
        if any(status is TrustedExternalLookupStatus.NO_RESULTS for status in statuses):
            return TrustedExternalLookupStatus.NO_RESULTS
        return TrustedExternalLookupStatus.UNAVAILABLE

    @staticmethod
    def _clean_optional(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @staticmethod
    def _is_traceable(*, source_title: str | None, snippet: str | None, reference_link: str | None) -> bool:
        return bool(source_title or snippet or reference_link)

    @staticmethod
    def _safe_commit(session: Session) -> bool:
        try:
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Failed to persist trusted external lookup cache.")
            return False
        return True


@lru_cache(maxsize=1)
def get_external_lookup_service() -> ExternalLookupService:
    return ExternalLookupService()
