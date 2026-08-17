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
    EXTERNAL_CONTENT_MAX_CHARS,
    EXTERNAL_SEARCH_TOOL_NAME,
    ExternalSearchDateFilter,
    ExternalSearchHit,
    ExternalSearchProviderError,
    ExternalSearchToolFailureReason,
    ExternalSearchToolInput,
    ExternalSearchToolName,
)
from app.agent.evidence_collection.external_search.metrics import (
    record_external_hit_dropped,
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
_MISSING_HITS = object()


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
    """完成済みqueryをTavilyで実行し、検証済みhitへ変換する。"""

    __slots__ = ("_api_key", "_client")

    def __init__(self, *, api_key: SecretStr, client: TavilyHttpClient) -> None:
        if not api_key.get_secret_value():
            raise ValueError("TAVILY_API_KEY is not configured")
        self._api_key = api_key
        self._client = client

    @property
    def name(self) -> ExternalSearchToolName:
        return EXTERNAL_SEARCH_TOOL_NAME

    async def search(
        self,
        input: ExternalSearchToolInput,
    ) -> list[ExternalSearchHit]:
        if input.limit <= 0:
            raise ValueError("limit must be greater than 0")

        classified_error: ExternalSearchProviderError | None = None
        hits: list[ExternalSearchHit] | object = _MISSING_HITS
        with logfire.span(
            _TOOL_SPAN_NAME,
            _span_kind=SpanKind.CLIENT,
            tool_name=self.name,
        ) as span:
            try:
                hits = await self._execute(input)
            except ExternalSearchProviderError as exc:
                classified_error = exc
                span.set_attribute(ERROR_TYPE, exc.reason)
                span.set_status(StatusCode.ERROR)
            else:
                span.set_attribute("hit_count", len(hits))

        if classified_error is not None:
            raise classified_error
        if hits is _MISSING_HITS:
            raise RuntimeError("Tavily tool completed without hits")
        return cast(list[ExternalSearchHit], hits)

    async def _execute(
        self,
        input: ExternalSearchToolInput,
    ) -> list[ExternalSearchHit]:
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

        hits: list[ExternalSearchHit] = []
        for result in results:
            hit = _hit_from_result(result)
            if hit is not None:
                hits.append(hit)
        return hits[: input.limit]

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
        # ProxyError は RequestError の subclass なので先に判定する。まとめて受けると
        # egress の設定ミスが provider 障害として記録され、切り分けが逆を向く。
        transport_failure: ExternalSearchToolFailureReason | None = None
        try:
            response = await self._client.post(
                TAVILY_SEARCH_URL,
                headers={
                    "Authorization": (f"Bearer {self._api_key.get_secret_value()}")
                },
                json=body,
                timeout=TAVILY_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.ProxyError:
            transport_failure = ExternalSearchToolFailureReason.PROXY_ERROR
        except httpx.RequestError:
            transport_failure = ExternalSearchToolFailureReason.HTTP_ERROR

        if transport_failure is not None:
            raise ExternalSearchProviderError(reason=transport_failure)

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


def _hit_from_result(result: object) -> ExternalSearchHit | None:
    if not isinstance(result, Mapping):
        record_external_hit_dropped(reason="result_not_mapping")
        return None

    title = _clean_required_text(result.get("title"))
    if title is None:
        record_external_hit_dropped(reason="title_missing")
        return None

    url = _safe_url(result.get("url"))
    if url is None:
        record_external_hit_dropped(reason="url_unsafe")
        return None

    content = _clean_optional_content(result.get("content"))
    # 本文だけ落として残しても出典として使えないため、超過はhitごと捨てる。
    if content is not None and len(content) > EXTERNAL_CONTENT_MAX_CHARS:
        record_external_hit_dropped(reason="content_too_long")
        return None

    published_at = _parse_published_date(result.get("published_date"))
    return ExternalSearchHit(
        url=url,
        title=title,
        content=content,
        published_at=published_at,
        source_name=_source_name(url.root),
    )


def _clean_required_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _clean_optional_content(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


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
