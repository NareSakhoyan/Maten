from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
import logging
from threading import Lock
from time import monotonic, sleep
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup, NavigableString, Tag
from httpx import Client as HTTPXClient
from httpx import Timeout

from app.core.config import Settings, get_settings
from app.db.models import ReferenceMatchType
from app.schemas.word import WordSearchMode
from app.services.external_sources.base import (
    ExternalEvidenceItem,
    ExternalLookupProvider,
    ExternalLookupProviderError,
)
from app.utils.text_normalization import normalize_token


_FUZZY_THRESHOLD = 90.0
_QUERY_LANGUAGE = "hy_LB"
_QUERY_DICTIONARY = "HY_HY"
_QUERY_RESULT_MODE = "0"
_RESPONSE_PREVIEW_CHARS = 400


logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NayiriProvider(ExternalLookupProvider):
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: HTTPXClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.nayiri_provider_base_url
        self.http_client = http_client or HTTPXClient(
            timeout=Timeout(self.settings.external_lookup_http_timeout_seconds),
            headers={
                "User-Agent": "BaghramyanBackend/0.1 (trusted-external-lookup; contact: backend)",
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
        )
        self._rate_limit_ms = self.settings.nayiri_provider_rate_limit_ms
        self._throttle_lock = Lock()
        self._last_request_monotonic = 0.0

    def provider_key(self) -> str:
        return "nayiri_web"

    def provider_display_name(self) -> str:
        return "Nayiri"

    def search_word(
        self,
        *,
        query: str,
        normalized_query: str,
        mode: WordSearchMode,
    ) -> list[ExternalEvidenceItem]:
        if not query.strip() or not normalized_query:
            return []

        self._throttle()
        request_url = self._build_request_url(query.strip())
        logger.info(
            "Nayiri lookup start: query=%r normalized_query=%r mode=%s request_url=%s",
            query.strip(),
            normalized_query,
            mode.value,
            request_url,
        )
        try:
            response = self.http_client.get(
                self.base_url,
                params=self._request_params(query.strip()),
            )
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover - network path covered through service failure test
            logger.warning(
                "Nayiri lookup HTTP failure: query=%r normalized_query=%r request_url=%s error=%s",
                query.strip(),
                normalized_query,
                request_url,
                str(exc),
            )
            raise ExternalLookupProviderError("Nayiri lookup failed.") from exc

        resolved_url = str(getattr(response, "url", request_url) or request_url)
        response_text = response.text or ""
        logger.info(
            "Nayiri response received: query=%r status_code=%s resolved_url=%s body_chars=%s body_preview=%r",
            query.strip(),
            getattr(response, "status_code", None),
            resolved_url,
            len(response_text),
            self._body_preview(response_text),
        )
        try:
            results = self._parse_search_html(
                response_text,
                query=query.strip(),
                normalized_query=normalized_query,
                mode=mode,
                request_url=resolved_url,
            )
            logger.info(
                "Nayiri lookup decision: query=%r normalized_query=%r exists=%s result_count=%s",
                query.strip(),
                normalized_query,
                bool(results),
                len(results),
            )
            return results
        except Exception as exc:
            logger.warning(
                "Nayiri lookup parse failure: query=%r normalized_query=%r resolved_url=%s error=%s",
                query.strip(),
                normalized_query,
                resolved_url,
                str(exc),
            )
            raise ExternalLookupProviderError("Nayiri lookup HTML parsing failed.") from exc

    def _request_params(self, query: str) -> dict[str, str]:
        return {
            "dt": _QUERY_DICTIONARY,
            "r": _QUERY_RESULT_MODE,
            "l": _QUERY_LANGUAGE,
            "query": query,
        }

    def _build_request_url(self, query: str) -> str:
        return f"{self.base_url}?{urlencode(self._request_params(query))}"

    def _parse_search_html(
        self,
        html_text: str,
        *,
        query: str,
        normalized_query: str,
        mode: WordSearchMode,
        request_url: str,
    ) -> list[ExternalEvidenceItem]:
        soup = BeautifulSoup(html_text, "html.parser")
        matched_form = self._extract_matched_form(soup, query=query)
        classified = self._classify_match(query=query, normalized_query=normalized_query, matched_form=matched_form, mode=mode)
        logger.info(
            "Nayiri parse matched-form: query=%r matched_form=%r normalized_matched_form=%r",
            query,
            matched_form,
            normalize_token(matched_form),
        )
        if classified is None:
            logger.info(
                "Nayiri parse decision: query=%r exists=false reason=%s",
                query,
                "matched_form_not_compatible_with_requested_mode",
            )
            return []
        match_type, match_score, normalized_form = classified
        candidate_nodes = soup.find_all("li")
        logger.info(
            "Nayiri parse classification: query=%r match_type=%s match_score=%s candidate_nodes=%s",
            query,
            match_type.value,
            match_score,
            len(candidate_nodes),
        )
        fetched_at = _utc_now()

        results: list[ExternalEvidenceItem] = []
        seen: set[tuple[str, str, str]] = set()
        for item_node in candidate_nodes:
            anchor = item_node.find("a", href=True)
            if anchor is None:
                continue

            source_title = self._clean_text(anchor.get_text(" ", strip=True))
            reference_link = self._resolve_reference_link(anchor["href"], request_url=request_url)
            if not source_title or not reference_link:
                continue

            source_subtitle = self._section_title_for(item_node)
            snippet = self._snippet_for(item_node, anchor=anchor)
            if not self._is_traceable(
                source_title=source_title,
                snippet=snippet,
                reference_link=reference_link,
            ):
                continue

            dedupe_key = (matched_form, source_title, reference_link)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            results.append(
                ExternalEvidenceItem(
                    provider_key=self.provider_key(),
                    provider_display_name=self.provider_display_name(),
                    matched_form=matched_form,
                    normalized_form=normalized_form,
                    source_title=source_title,
                    source_subtitle=source_subtitle,
                    snippet=snippet,
                    reference_link=reference_link,
                    match_type=match_type,
                    match_score=match_score,
                    fetched_at=fetched_at,
                    metadata_json={
                        "request_url": request_url,
                        "section_title": source_subtitle,
                        "href": anchor["href"],
                    },
                )
            )
        if results:
            logger.info(
                "Nayiri parse decision: query=%r exists=true accepted_results=%s first_titles=%r",
                query,
                len(results),
                [item.source_title for item in results[:3]],
            )
        else:
            logger.info(
                "Nayiri parse decision: query=%r exists=false reason=%s",
                query,
                "no_traceable_result_items",
            )
        return results

    def _extract_matched_form(self, soup: BeautifulSoup, *, query: str) -> str:
        candidate_selectors = [
            ".lemma-line",
            ".search-result-headword",
            ".entry-headword",
            ".result-headword",
            "h1",
            "h2",
        ]
        for selector in candidate_selectors:
            for node in soup.select(selector):
                candidate = self._matched_form_from_text(self._clean_text(node.get_text(" ", strip=True)))
                if candidate:
                    return candidate

        normalized_query = normalize_token(query)
        for text_node in soup.find_all(string=True):
            if not isinstance(text_node, NavigableString):
                continue
            text = self._clean_text(str(text_node))
            if not text:
                continue
            candidate = self._matched_form_from_text(text)
            if candidate and normalize_token(candidate) == normalized_query:
                return candidate
        return query.strip()

    @staticmethod
    def _matched_form_from_text(text: str) -> str | None:
        if not text:
            return None
        if "→" in text:
            left = text.split("→", maxsplit=1)[0].strip(" «»")
            return left or None
        if len(text.split()) == 1:
            return text
        return None

    def _section_title_for(self, item_node: Tag) -> str | None:
        section_parent = item_node.find_parent(["section", "div"])
        while section_parent is not None:
            heading = section_parent.find(["h2", "h3", "h4"])
            if heading is not None:
                title = self._clean_text(heading.get_text(" ", strip=True))
                if title:
                    return title
            section_parent = section_parent.find_parent(["section", "div"])

        for sibling in item_node.previous_siblings:
            if isinstance(sibling, Tag) and sibling.name in {"h2", "h3", "h4"}:
                title = self._clean_text(sibling.get_text(" ", strip=True))
                if title:
                    return title
        return None

    def _snippet_for(self, item_node: Tag, *, anchor: Tag) -> str | None:
        raw_text = self._clean_text(item_node.get_text(" ", strip=True))
        anchor_text = self._clean_text(anchor.get_text(" ", strip=True))
        if not raw_text:
            return None
        if raw_text == anchor_text:
            return None
        snippet = raw_text.replace(anchor_text, "", 1).strip(" -\u2013\u2014")
        return snippet or None

    @staticmethod
    def _resolve_reference_link(href: str, *, request_url: str) -> str | None:
        cleaned_href = href.strip()
        if not cleaned_href or cleaned_href.startswith("#") or cleaned_href.lower().startswith("javascript:"):
            return None
        return urljoin(request_url, cleaned_href)

    @staticmethod
    def _classify_match(
        *,
        query: str,
        normalized_query: str,
        matched_form: str,
        mode: WordSearchMode,
    ) -> tuple[ReferenceMatchType, float | None, str | None] | None:
        normalized_form = normalize_token(matched_form)
        if not normalized_form:
            return None
        if matched_form == query:
            return ReferenceMatchType.EXACT, 100.0, normalized_form
        if normalized_form == normalized_query:
            return ReferenceMatchType.NORMALIZED, 100.0, normalized_form
        if mode is not WordSearchMode.FUZZY:
            return None
        score = float(SequenceMatcher(a=normalized_query, b=normalized_form).ratio() * 100)
        if score < _FUZZY_THRESHOLD:
            return None
        return ReferenceMatchType.FUZZY, score, normalized_form

    @staticmethod
    def _clean_text(value: str | None) -> str:
        if not value:
            return ""
        return " ".join(value.split()).strip()

    @staticmethod
    def _is_traceable(*, source_title: str | None, snippet: str | None, reference_link: str | None) -> bool:
        return bool(source_title or snippet or reference_link)

    @staticmethod
    def _body_preview(value: str) -> str:
        collapsed = " ".join(value.split())
        if len(collapsed) <= _RESPONSE_PREVIEW_CHARS:
            return collapsed
        return f"{collapsed[:_RESPONSE_PREVIEW_CHARS]}..."

    def _throttle(self) -> None:
        if self._rate_limit_ms <= 0:
            return
        with self._throttle_lock:
            now = monotonic()
            remaining = (self._rate_limit_ms / 1000) - (now - self._last_request_monotonic)
            if remaining > 0:
                sleep(remaining)
            self._last_request_monotonic = monotonic()
