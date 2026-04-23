from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ExternalLookupCache, ExternalLookupResult, ExternalLookupStatus, ExternalProvider, ReferenceMatchType
from app.schemas.word import TrustedExternalLookupStatus, WordSearchMode
from app.services.external_lookup_service import ExternalLookupService
from app.services.external_sources.base import ExternalEvidenceItem, ExternalLookupProvider, ExternalLookupProviderError
from app.services.external_sources.nayiri_provider import NayiriProvider
from conftest import PRIMARY_USER_ID


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "nayiri"


class FakeHTTPResponse:
    def __init__(self, text: str, *, status_code: int = 200, url: str | None = None) -> None:
        self.text = text
        self.status_code = status_code
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTPClient:
    def __init__(self, response: FakeHTTPResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, params: dict[str, str] | None = None):
        self.calls.append({"url": url, "params": params})
        return self.response


class StubProvider(ExternalLookupProvider):
    def __init__(
        self,
        *,
        provider_key: str = "nayiri_web",
        provider_display_name: str = "Nayiri",
        items: list[ExternalEvidenceItem] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._provider_key = provider_key
        self._provider_display_name = provider_display_name
        self.items = items or []
        self.error = error
        self.calls = 0

    def provider_key(self) -> str:
        return self._provider_key

    def provider_display_name(self) -> str:
        return self._provider_display_name

    def search_word(
        self,
        *,
        query: str,
        normalized_query: str,
        mode: WordSearchMode,
    ) -> list[ExternalEvidenceItem]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.items


def _fixture_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def test_nayiri_provider_parses_saved_html_fixture() -> None:
    html = _fixture_text("search_results.html")
    client = FakeHTTPClient(
        FakeHTTPResponse(
            html,
            url="http://www.nayiri.com/search?dt=HY_HY&r=0&l=hy_LB&query=%D5%B0%D5%A1%D5%B5",
        )
    )
    provider = NayiriProvider(http_client=client)

    results = provider.search_word(
        query="Հայ",
        normalized_query="հայ",
        mode=WordSearchMode.NORMALIZED,
    )

    assert len(results) == 2
    assert client.calls == [
        {
            "url": "http://www.nayiri.com/search",
            "params": {"dt": "HY_HY", "r": "0", "l": "hy_LB", "query": "Հայ"},
        }
    ]

    first = results[0]
    assert first.provider_key == "nayiri_web"
    assert first.provider_display_name == "Nayiri"
    assert first.matched_form == "Հայ"
    assert first.normalized_form == "հայ"
    assert first.source_title == "(1992) Հայոց լեզուի նոր բառարան"
    assert first.source_subtitle == "Բացատրական"
    assert first.reference_link == "http://www.nayiri.com/?id=0001&lang=hy"
    assert first.snippet == "հայ — ազգ, ժողովուրդ"
    assert first.match_type is ReferenceMatchType.EXACT


def test_nayiri_provider_logs_response_and_exists_decision(caplog) -> None:
    html = _fixture_text("search_results.html")
    client = FakeHTTPClient(
        FakeHTTPResponse(
            html,
            url="http://www.nayiri.com/search?dt=HY_HY&r=0&l=hy_LB&query=%D5%B0%D5%A1%D5%B5",
        )
    )
    provider = NayiriProvider(http_client=client)

    with caplog.at_level(logging.INFO, logger="app.services.external_sources.nayiri_provider"):
        results = provider.search_word(
            query="Հայ",
            normalized_query="հայ",
            mode=WordSearchMode.NORMALIZED,
        )

    assert len(results) == 2
    assert "Nayiri response received" in caplog.text
    assert "body_preview=" in caplog.text
    assert "Nayiri parse matched-form" in caplog.text
    assert "Nayiri parse classification" in caplog.text
    assert "exists=true" in caplog.text


def test_nayiri_provider_returns_empty_for_empty_fixture() -> None:
    html = _fixture_text("search_no_results.html")
    client = FakeHTTPClient(FakeHTTPResponse(html, url="http://www.nayiri.com/search?query=%D5%B0%D5%A1%D5%B5"))
    provider = NayiriProvider(http_client=client)

    results = provider.search_word(
        query="Հայ",
        normalized_query="հայ",
        mode=WordSearchMode.NORMALIZED,
    )

    assert results == []


def test_nayiri_provider_logs_no_results_decision(caplog) -> None:
    html = _fixture_text("search_no_results.html")
    client = FakeHTTPClient(FakeHTTPResponse(html, url="http://www.nayiri.com/search?query=%D5%B0%D5%A1%D5%B5"))
    provider = NayiriProvider(http_client=client)

    with caplog.at_level(logging.INFO, logger="app.services.external_sources.nayiri_provider"):
        results = provider.search_word(
            query="Հայ",
            normalized_query="հայ",
            mode=WordSearchMode.NORMALIZED,
        )

    assert results == []
    assert "Nayiri response received" in caplog.text
    assert "exists=false" in caplog.text
    assert "no_traceable_result_items" in caplog.text


def test_external_lookup_service_persists_and_reuses_cache(db_session: Session) -> None:
    fixed_now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    provider = StubProvider(
        items=[
            ExternalEvidenceItem(
                provider_key="nayiri_web",
                provider_display_name="Nayiri",
                matched_form="Հայաստան",
                normalized_form="հայաստան",
                source_title="Nayiri Entry",
                snippet="Հայաստան snippet",
                reference_link="https://example.test/nayiri/1",
                match_type=ReferenceMatchType.NORMALIZED,
                match_score=100.0,
            )
        ]
    )
    service = ExternalLookupService(providers=[provider], now_fn=lambda: fixed_now)

    first_batch = service.lookup(
        db_session,
        user_id=PRIMARY_USER_ID,
        query="Հայաստան",
        mode=WordSearchMode.NORMALIZED,
    )
    second_batch = service.lookup(
        db_session,
        user_id=PRIMARY_USER_ID,
        query="Հայաստան",
        mode=WordSearchMode.NORMALIZED,
    )

    assert len(first_batch.items) == 1
    assert len(second_batch.items) == 1
    assert first_batch.status is TrustedExternalLookupStatus.COMPLETED
    assert second_batch.status is TrustedExternalLookupStatus.COMPLETED
    assert provider.calls == 1
    assert db_session.scalar(select(func.count(ExternalProvider.id))) == 1
    assert db_session.scalar(select(func.count(ExternalLookupCache.id))) == 1
    assert db_session.scalar(select(func.count(ExternalLookupResult.id))) == 1

    provider_row = db_session.scalar(select(ExternalProvider))
    assert provider_row is not None
    assert provider_row.key == "nayiri_web"

    cache_row = db_session.scalar(select(ExternalLookupCache))
    assert cache_row is not None
    assert cache_row.status is ExternalLookupStatus.COMPLETED
    assert cache_row.normalized_query == "հայաստան"

    result_row = db_session.scalar(select(ExternalLookupResult))
    assert result_row is not None
    assert result_row.matched_form == "Հայաստան"
    assert result_row.reference_link == "https://example.test/nayiri/1"


def test_external_lookup_service_caches_successful_empty_result(db_session: Session) -> None:
    fixed_now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    provider = StubProvider(items=[])
    service = ExternalLookupService(providers=[provider], now_fn=lambda: fixed_now)

    first_batch = service.lookup(
        db_session,
        user_id=PRIMARY_USER_ID,
        query="Հայաստան",
        mode=WordSearchMode.NORMALIZED,
    )
    second_batch = service.lookup(
        db_session,
        user_id=PRIMARY_USER_ID,
        query="Հայաստան",
        mode=WordSearchMode.NORMALIZED,
    )

    assert first_batch.items == []
    assert second_batch.items == []
    assert first_batch.status is TrustedExternalLookupStatus.NO_RESULTS
    assert second_batch.status is TrustedExternalLookupStatus.NO_RESULTS
    assert provider.calls == 1
    assert db_session.scalar(select(func.count(ExternalLookupCache.id))) == 1
    assert db_session.scalar(select(func.count(ExternalLookupResult.id))) == 0

    cache_row = db_session.scalar(select(ExternalLookupCache))
    assert cache_row is not None
    assert cache_row.status is ExternalLookupStatus.COMPLETED


def test_external_lookup_service_caches_failures_without_retrying_immediately(db_session: Session) -> None:
    fixed_now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    provider = StubProvider(error=ExternalLookupProviderError("provider unavailable"))
    service = ExternalLookupService(providers=[provider], now_fn=lambda: fixed_now)

    first_batch = service.lookup(
        db_session,
        user_id=PRIMARY_USER_ID,
        query="Հայաստան",
        mode=WordSearchMode.NORMALIZED,
    )
    second_batch = service.lookup(
        db_session,
        user_id=PRIMARY_USER_ID,
        query="Հայաստան",
        mode=WordSearchMode.NORMALIZED,
    )

    assert first_batch.items == []
    assert second_batch.items == []
    assert first_batch.status is TrustedExternalLookupStatus.UNAVAILABLE
    assert second_batch.status is TrustedExternalLookupStatus.UNAVAILABLE
    assert provider.calls == 1
    assert db_session.scalar(select(func.count(ExternalLookupCache.id))) == 1
    assert db_session.scalar(select(func.count(ExternalLookupResult.id))) == 0

    cache_row = db_session.scalar(select(ExternalLookupCache))
    assert cache_row is not None
    assert cache_row.status is ExternalLookupStatus.FAILED
