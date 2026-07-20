"""External Search ToolとしてTavily Search APIを呼ぶadapter。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Final, Protocol, cast
from urllib.parse import urlparse

import httpx
import logfire
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import SecretStr, ValidationError

from app.agent.evidence_collection.external_search.contract import (
    CANDIDATE_SNIPPET_MAX_CHARS,
    EXTERNAL_SEARCH_TOOL_NAME,
    ExternalSearchCandidate,
    ExternalSearchDateFilter,
    ExternalSearchProviderError,
    ExternalSearchToolFailureReason,
    ExternalSearchToolInput,
    ExternalSearchToolName,
)
from app.shared.security.safe_url import SafeUrl

__all__ = [
    "TAVILY_MAX_RESULTS_LIMIT",
    "TAVILY_REQUEST_TIMEOUT_SECONDS",
    "TAVILY_SEARCH_URL",
    "TavilyExternalSearchTool",
]

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_REQUEST_TIMEOUT_SECONDS = 10
TAVILY_MAX_RESULTS_LIMIT = 20
_TOOL_SPAN_NAME: Final[str] = "external_search_tool_call"
_MISSING_CANDIDATES = object()


class TavilyHttpClient(Protocol):
    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
        timeout: float,
    ) -> httpx.Response: ...


class TavilyExternalSearchTool:
    """完成済みqueryをTavilyで実行し、検証済みcandidateへ変換する。"""

    __slots__ = ("_api_key", "_client")

    def __init__(self, *, api_key: SecretStr, client: TavilyHttpClient) -> None:
        if not api_key.get_secret_value():
            raise ValueError("TAVILY_API_KEY is not configured")
        self._api_key = api_key
        self._client = client

    @property
    def name(self) -> ExternalSearchToolName:
        return EXTERNAL_SEARCH_TOOL_NAME

    async def invoke(
        self,
        input: ExternalSearchToolInput,
    ) -> list[ExternalSearchCandidate]:
        if input.limit <= 0:
            raise ValueError("limit must be greater than 0")

        classified_error: ExternalSearchProviderError | None = None
        candidates: list[ExternalSearchCandidate] | object = _MISSING_CANDIDATES
        with logfire.span(
            _TOOL_SPAN_NAME,
            _span_kind=SpanKind.CLIENT,
            tool_name=self.name,
        ) as span:
            try:
                candidates = await self._execute(input)
            except ExternalSearchProviderError as exc:
                classified_error = exc
                span.set_attribute(ERROR_TYPE, exc.reason)
                span.set_status(StatusCode.ERROR)
            else:
                span.set_attribute("candidate_count", len(candidates))

        if classified_error is not None:
            raise classified_error
        if candidates is _MISSING_CANDIDATES:
            raise RuntimeError("Tavily tool completed without candidates")
        return cast(list[ExternalSearchCandidate], candidates)

    async def _execute(
        self,
        input: ExternalSearchToolInput,
    ) -> list[ExternalSearchCandidate]:
        response = await self._post_search(
            query=input.query,
            limit=input.limit,
            date_filter=input.date_filter,
        )
        data = _response_json(response)
        results = data.get("results")
        if not isinstance(results, list):
            raise ExternalSearchProviderError(
                reason=ExternalSearchToolFailureReason.INVALID_RESULTS
            )

        candidates: list[ExternalSearchCandidate] = []
        for result in results:
            candidate = _candidate_from_result(result)
            if candidate is not None:
                candidates.append(candidate)
        return candidates[: input.limit]

    async def _post_search(
        self,
        *,
        query: str,
        limit: int,
        date_filter: ExternalSearchDateFilter | None,
    ) -> httpx.Response:
        body: dict[str, object] = {
            "query": query,
            "topic": "news",
            "search_depth": "basic",
            "max_results": min(limit, TAVILY_MAX_RESULTS_LIMIT),
            "include_answer": False,
            "include_raw_content": False,
        }
        if date_filter is not None:
            provider_start_date = date_filter.start_date - timedelta(days=1)
            body["start_date"] = provider_start_date.isoformat()
            body["end_date"] = date_filter.end_date.isoformat()
        try:
            response = await self._client.post(
                TAVILY_SEARCH_URL,
                headers={
                    "Authorization": (f"Bearer {self._api_key.get_secret_value()}")
                },
                json=body,
                timeout=TAVILY_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.RequestError:
            response = None

        if response is None:
            raise ExternalSearchProviderError(
                reason=ExternalSearchToolFailureReason.HTTP_ERROR
            )

        if not 200 <= response.status_code < 300:
            raise ExternalSearchProviderError(
                reason=ExternalSearchToolFailureReason.HTTP_STATUS,
                status_code=response.status_code,
            )
        return response


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ExternalSearchProviderError(
            reason=ExternalSearchToolFailureReason.INVALID_JSON
        )
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
