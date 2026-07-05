"""Tavily Search API adapter for the external search provider port."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr, ValidationError

from app.agent.external_search.contract import (
    CANDIDATE_SNIPPET_MAX_CHARS,
    ExternalSearchCandidate,
    ExternalSearchProviderError,
)
from app.shared.security.safe_url import SafeUrl

__all__ = [
    "TAVILY_MAX_RESULTS_LIMIT",
    "TAVILY_REQUEST_TIMEOUT_SECONDS",
    "TAVILY_SEARCH_URL",
    "TavilySearchProvider",
]

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_REQUEST_TIMEOUT_SECONDS = 10
TAVILY_MAX_RESULTS_LIMIT = 20


class TavilyHttpClient(Protocol):
    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
        timeout: float,
    ) -> httpx.Response: ...


class TavilySearchProvider:
    """SearchProvider port の Tavily 実装。整形のみ行い、選別はしない。"""

    def __init__(self, *, api_key: SecretStr, client: TavilyHttpClient) -> None:
        if not api_key.get_secret_value():
            raise ValueError("TAVILY_API_KEY is not configured")
        self._api_key = api_key
        self._client = client

    async def search(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[ExternalSearchCandidate]:
        if limit <= 0:
            raise ValueError("limit must be greater than 0")

        response = await self._post_search(query=query, limit=limit)
        data = _response_json(response)
        results = data.get("results")
        if not isinstance(results, list):
            raise ExternalSearchProviderError("tavily_search_invalid_results")

        candidates: list[ExternalSearchCandidate] = []
        for result in results:
            candidate = _candidate_from_result(result)
            if candidate is not None:
                candidates.append(candidate)
        return candidates[:limit]

    async def _post_search(self, *, query: str, limit: int) -> httpx.Response:
        body = {
            "query": query,
            "topic": "news",
            "search_depth": "basic",
            "max_results": min(limit, TAVILY_MAX_RESULTS_LIMIT),
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            response = await self._client.post(
                TAVILY_SEARCH_URL,
                headers={
                    "Authorization": (f"Bearer {self._api_key.get_secret_value()}")
                },
                json=body,
                timeout=TAVILY_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as exc:
            raise ExternalSearchProviderError("tavily_search_http_error") from exc

        if not 200 <= response.status_code < 300:
            raise ExternalSearchProviderError(
                f"tavily_search_http_status_{response.status_code}"
            )
        return response


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise ExternalSearchProviderError("tavily_search_invalid_json") from exc
    if not isinstance(data, dict):
        raise ExternalSearchProviderError("tavily_search_invalid_json")
    return data


def _candidate_from_result(result: object) -> ExternalSearchCandidate | None:
    if not isinstance(result, Mapping):
        return None

    title = _clean_required_text(result.get("title"))
    if title is None:
        return None

    url = _safe_url(result.get("url"))
    if url is None:
        return None

    snippet = _clean_optional_snippet(result.get("content"))
    published_at = _parse_published_date(result.get("published_date"))
    return ExternalSearchCandidate(
        url=url,
        title=title,
        snippet=snippet,
        published_at=published_at,
        source_name=_source_name(url.root),
    )


def _clean_required_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _clean_optional_snippet(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:CANDIDATE_SNIPPET_MAX_CHARS]


def _safe_url(value: object) -> SafeUrl | None:
    try:
        return SafeUrl.model_validate(value)
    except ValidationError:
        return None


def _parse_published_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _source_name(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    host = urlparse(url.strip()).hostname
    if host is None:
        return None
    if host.startswith("www."):
        return host[4:]
    return host
