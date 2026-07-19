"""Tavily External Search Tool adapter tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

import app.agent.evidence_collection.external_search as external_search_module
from app.agent.evidence_collection.external_search import (
    CANDIDATE_SNIPPET_MAX_CHARS,
    TAVILY_MAX_RESULTS_LIMIT,
    TAVILY_SEARCH_URL,
    ExternalSearchProviderError,
)

TAVILY_TEST_KEY = "tvly-test-secret"


def _provider(client: httpx.AsyncClient) -> Any:
    return external_search_module.TavilyExternalSearchTool(
        api_key=SecretStr(TAVILY_TEST_KEY),
        client=client,
    )


def _input(*, query: str, limit: int) -> Any:
    return external_search_module.ExternalSearchToolInput(query=query, limit=limit)


async def _invoke(provider: Any, *, query: str, limit: int) -> list[Any]:
    return await provider.invoke(_input(query=query, limit=limit))


def _response(payload: object, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _result(
    *,
    url: str = "https://www.example.com/news",
    title: str = "Example title",
    content: str | None = "Example content",
    published_date: object | None = "2026-07-04T12:30:00Z",
) -> dict[str, object]:
    result: dict[str, object] = {
        "url": url,
        "title": title,
    }
    if content is not None:
        result["content"] = content
    if published_date is not None:
        result["published_date"] = published_date
    return result


@pytest.mark.asyncio
async def test_search_posts_fixed_news_request_with_bearer_header() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response({"results": [_result()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client)

        await _invoke(provider, query="NVIDIA Blackwell", limit=3)

    assert provider.name == "external_search"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == TAVILY_SEARCH_URL
    assert request.headers["Authorization"] == f"Bearer {TAVILY_TEST_KEY}"
    body = json.loads(request.content)
    assert body == {
        "query": "NVIDIA Blackwell",
        "topic": "news",
        "search_depth": "basic",
        "max_results": 3,
        "include_answer": False,
        "include_raw_content": False,
    }


@pytest.mark.asyncio
async def test_search_clamps_requested_max_results_to_tavily_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response({"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client)

        await _invoke(provider, query="NVIDIA Blackwell", limit=30)

    body = json.loads(requests[0].content)
    assert body["max_results"] == TAVILY_MAX_RESULTS_LIMIT


@pytest.mark.asyncio
async def test_search_rejects_non_positive_limit_without_http_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response({"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client)

        with pytest.raises(ValueError, match="limit"):
            await _invoke(provider, query="NVIDIA Blackwell", limit=0)
        with pytest.raises(ValueError, match="limit"):
            await _invoke(provider, query="NVIDIA Blackwell", limit=-1)

    assert calls == 0


def test_provider_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError, match="TAVILY_API_KEY"):
        external_search_module.TavilyExternalSearchTool(
            api_key=SecretStr(""), client=object()
        )


@pytest.mark.asyncio
async def test_search_maps_results_to_candidates_preserving_rank() -> None:
    payload = {
        "results": [
            _result(
                url="https://www.example.com/news",
                title="  First title  ",
                content="  First snippet  ",
                published_date="2026-07-04T12:30:00Z",
            ),
            _result(
                url="https://investor.example.com/second",
                title="Second title",
                content="",
                published_date="2026-07-04T21:30:00+09:00",
            ),
        ]
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(payload))
    ) as client:
        provider = _provider(client)

        candidates = await _invoke(provider, query="NVIDIA Blackwell", limit=10)

    assert [str(candidate.url) for candidate in candidates] == [
        "https://www.example.com/news",
        "https://investor.example.com/second",
    ]
    assert candidates[0].title == "First title"
    assert candidates[0].snippet == "First snippet"
    assert candidates[0].published_at == datetime(2026, 7, 4, 12, 30, tzinfo=UTC)
    assert candidates[0].source_name == "example.com"
    assert candidates[1].snippet is None
    assert candidates[1].published_at == datetime(
        2026,
        7,
        4,
        21,
        30,
        tzinfo=timezone(timedelta(hours=9)),
    )
    assert candidates[1].source_name == "investor.example.com"


@pytest.mark.asyncio
async def test_search_caps_candidates_to_requested_limit() -> None:
    payload = {
        "results": [
            _result(url=f"https://example.com/{index}", title=f"title-{index}")
            for index in range(3)
        ]
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(payload))
    ) as client:
        provider = _provider(client)

        candidates = await _invoke(provider, query="NVIDIA Blackwell", limit=2)

    assert [candidate.title for candidate in candidates] == ["title-0", "title-1"]


@pytest.mark.asyncio
async def test_search_truncates_content_to_candidate_snippet_cap() -> None:
    payload = {
        "results": [
            _result(content="x" * (CANDIDATE_SNIPPET_MAX_CHARS + 25)),
        ]
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(payload))
    ) as client:
        provider = _provider(client)

        candidates = await _invoke(provider, query="NVIDIA Blackwell", limit=10)

    assert len(candidates) == 1
    assert candidates[0].snippet == "x" * CANDIDATE_SNIPPET_MAX_CHARS


def test_candidate_rejects_over_cap_snippet_when_constructed_directly() -> None:
    with pytest.raises(ValidationError):
        external_search_module.ExternalSearchCandidate(
            url="https://example.com/news",
            title="Example",
            snippet="x" * (CANDIDATE_SNIPPET_MAX_CHARS + 1),
        )


@pytest.mark.parametrize(
    ("published_date", "expected"),
    [
        (
            "2026-07-04T12:30:00Z",
            datetime(2026, 7, 4, 12, 30, tzinfo=UTC),
        ),
        (
            "2026-07-04T21:30:00+09:00",
            datetime(2026, 7, 4, 21, 30, tzinfo=timezone(timedelta(hours=9))),
        ),
        (
            "2026-07-04T12:30:00",
            datetime(2026, 7, 4, 12, 30, tzinfo=UTC),
        ),
        (
            "2026-07-04",
            datetime(2026, 7, 4, tzinfo=UTC),
        ),
        # probe 実測 (2026-07-05): Tavily topic=news の published_date は RFC 1123
        (
            "Fri, 03 Jul 2026 16:10:52 GMT",
            datetime(2026, 7, 3, 16, 10, 52, tzinfo=UTC),
        ),
    ],
)
@pytest.mark.asyncio
async def test_search_parses_published_date(
    published_date: str,
    expected: datetime,
) -> None:
    payload = {"results": [_result(published_date=published_date)]}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(payload))
    ) as client:
        provider = _provider(client)

        candidates = await _invoke(provider, query="NVIDIA Blackwell", limit=10)

    assert candidates[0].published_at == expected


@pytest.mark.parametrize("published_date", ["not a date", None, 123])
@pytest.mark.asyncio
async def test_search_keeps_candidate_when_published_date_is_unknown(
    published_date: object | None,
) -> None:
    payload = {"results": [_result(published_date=published_date)]}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(payload))
    ) as client:
        provider = _provider(client)

        candidates = await _invoke(provider, query="NVIDIA Blackwell", limit=10)

    assert len(candidates) == 1
    assert candidates[0].published_at is None


@pytest.mark.asyncio
async def test_search_drops_only_result_with_invalid_url_or_empty_title() -> None:
    payload = {
        "results": [
            _result(url="ftp://example.com/news", title="Bad scheme"),
            _result(url="http://169.254.169.254/news", title="Private IP"),
            _result(url="https://example.com/empty-title", title="  "),
            _result(url="https://example.com/valid", title="Valid"),
        ]
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(payload))
    ) as client:
        provider = _provider(client)

        candidates = await _invoke(provider, query="NVIDIA Blackwell", limit=10)

    assert [str(candidate.url) for candidate in candidates] == [
        "https://example.com/valid"
    ]


@pytest.mark.parametrize("status_code", [401, 429, 500])
@pytest.mark.asyncio
async def test_search_wraps_non_2xx_without_leaking_response_body_or_key(
    status_code: int,
) -> None:
    response_body = {"error": f"body mentions {TAVILY_TEST_KEY}"}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: _response(response_body, status_code=status_code)
        )
    ) as client:
        provider = _provider(client)

        with pytest.raises(ExternalSearchProviderError) as exc_info:
            await _invoke(provider, query="NVIDIA Blackwell", limit=10)

    message = str(exc_info.value)
    assert str(status_code) in message
    assert TAVILY_TEST_KEY not in message
    assert "body mentions" not in message
    assert exc_info.value.reason == f"tavily_search_http_status_{status_code}"


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda request: httpx.ConnectError("connect failed", request=request),
        lambda request: httpx.ReadTimeout("timed out", request=request),
    ],
)
@pytest.mark.asyncio
async def test_search_wraps_httpx_transport_errors(
    error_factory: Callable[[httpx.Request], Exception],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_factory(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client)

        with pytest.raises(ExternalSearchProviderError) as exc_info:
            await _invoke(provider, query="NVIDIA Blackwell", limit=10)

    assert TAVILY_TEST_KEY not in str(exc_info.value)
    assert exc_info.value.reason == "tavily_search_http_error"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.asyncio
async def test_search_wraps_json_decode_error() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"{"))
    ) as client:
        provider = _provider(client)

        with pytest.raises(
            ExternalSearchProviderError, match="invalid_json"
        ) as exc_info:
            await _invoke(provider, query="NVIDIA Blackwell", limit=10)

    assert exc_info.value.reason == "tavily_search_invalid_json"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize("payload", [{}, {"results": None}, {"results": {}}])
@pytest.mark.asyncio
async def test_search_wraps_missing_or_non_list_results(payload: object) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(payload))
    ) as client:
        provider = _provider(client)

        with pytest.raises(
            ExternalSearchProviderError, match="invalid_results"
        ) as exc_info:
            await _invoke(provider, query="NVIDIA Blackwell", limit=10)

    assert exc_info.value.reason == "tavily_search_invalid_results"


@pytest.mark.asyncio
async def test_search_returns_empty_list_for_normal_empty_results() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response({"results": []}))
    ) as client:
        provider = _provider(client)

        candidates = await _invoke(provider, query="NVIDIA Blackwell", limit=10)

    assert candidates == []
