"""本番の初期化と共通HTTP経路を通して、exportされるspanの契約を検証する。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import httpcore
import httpx
import logfire
import pytest
import structlog
from logfire.testing import TestExporter as SpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode

from app.http.external import make_external_async_client
from app.logfire import setup as logfire_setup

_URL = "https://articles.example/news"


@dataclass
class ResponseBody:
    chunks: tuple[bytes, ...] = (b"article",)
    consumed: list[bytes] = field(default_factory=list)
    closed: bool = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.consumed.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class HttpExchange:
    body: ResponseBody = field(default_factory=ResponseBody)
    response_headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    requests: list[httpcore.Request] = field(default_factory=list)
    request_bodies: list[bytes] = field(default_factory=list)
    error: Exception | None = None


@pytest.fixture
def exchange(monkeypatch: pytest.MonkeyPatch) -> HttpExchange:
    """DNSの返答とHTTPXより下位の送受信だけを差し替える。"""
    result = HttpExchange()
    monkeypatch.setenv("EGRESS_PROXY_URL", "http://proxy.vector.internal:3128")
    monkeypatch.setattr(
        "app.http.destination_resolution._resolve_host",
        AsyncMock(return_value=["93.184.216.34"]),
    )

    async def send(
        self: httpcore.AsyncHTTPProxy, request: httpcore.Request
    ) -> httpcore.Response:
        result.requests.append(request)
        result.request_bodies.append(
            b"".join([chunk async for chunk in request.stream])
        )
        if result.error is not None:
            raise result.error
        return httpcore.Response(
            200, headers=result.response_headers, content=result.body
        )

    monkeypatch.setattr(httpcore.AsyncHTTPProxy, "handle_async_request", send)
    return result


@pytest.fixture
def exported_spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[ReadableSpan]]:
    """実bootstrapの送信先をメモリに限定し、グローバルな計測設定を回収する。"""
    exporter = SpanExporter()
    configure = logfire.configure
    structlog_config = structlog.get_config()
    structlog_configured = structlog.is_configured()
    monkeypatch.setattr(logfire_setup.settings, "logfire_token", None)
    monkeypatch.setattr(logfire_setup.settings, "env", "production")

    def configure_in_memory(**kwargs: Any) -> None:
        configure(
            **{
                **kwargs,
                "send_to_logfire": False,
                "additional_span_processors": [SimpleSpanProcessor(exporter)],
                "metrics": False,
            }
        )

    monkeypatch.setattr(logfire_setup.logfire, "configure", configure_in_memory)
    try:
        logfire_setup.setup_logfire("vector-httpx-test")
        yield exporter.exported_spans
    finally:
        HTTPXClientInstrumentor().uninstrument()
        configure(send_to_logfire=False, console=False, metrics=False)
        structlog.reset_defaults()
        if structlog_configured:
            structlog.configure(**structlog_config)


def _http_span(spans: list[ReadableSpan]) -> ReadableSpan:
    clients = [
        span
        for span in spans
        if span.kind == SpanKind.CLIENT
        and (span.attributes or {}).get("logfire.span_type") != "pending_span"
    ]
    assert len(clients) == 1
    return clients[0]


def _span_text(spans: list[ReadableSpan]) -> str:
    return "\n".join(span.to_json() for span in spans)


@pytest.mark.asyncio
async def test_request_exports_one_http_span(
    exchange: HttpExchange, exported_spans: list[ReadableSpan]
) -> None:
    """共通transportによる1回の送信がmethodとstatusを持つspanを生成する。"""
    async with make_external_async_client() as client:
        await client.get(_URL)

    assert len(exchange.requests) == 1
    span = _http_span(exported_spans)
    assert span.attributes["http.method"] == "GET"
    assert span.attributes["http.status_code"] == 200


@pytest.mark.asyncio
async def test_http_span_belongs_to_active_parent(
    exchange: HttpExchange, exported_spans: list[ReadableSpan]
) -> None:
    """HTTP spanは呼び出し元spanと同じtraceに属し、その直接の子になる。"""
    with logfire.span("article_fetch") as parent:
        parent_context = parent.get_span_context()
        async with make_external_async_client() as client:
            await client.get(_URL)

    span = _http_span(exported_spans)
    assert span.context.trace_id == parent_context.trace_id
    assert span.parent.span_id == parent_context.span_id


@pytest.mark.asyncio
async def test_request_authorization_header_is_not_exported(
    exchange: HttpExchange, exported_spans: list[ReadableSpan]
) -> None:
    """実際に送ったAuthorizationの値はspanに記録されない。"""
    marker = "Bearer outbound-header-sentinel"
    async with make_external_async_client() as client:
        await client.get(_URL, headers={"Authorization": marker})

    assert (b"Authorization", marker.encode()) in exchange.requests[0].headers
    assert not any(
        "request.header" in key for key in _http_span(exported_spans).attributes
    )
    assert marker not in _span_text(exported_spans)


@pytest.mark.asyncio
async def test_response_header_is_not_exported(
    exchange: HttpExchange, exported_spans: list[ReadableSpan]
) -> None:
    """受信した任意の応答ヘッダーはspanに記録されない。"""
    marker = "response-header-sentinel"
    exchange.response_headers = [(b"x-article-note", marker.encode())]
    async with make_external_async_client() as client:
        response = await client.get(_URL)

    assert response.headers["x-article-note"] == marker
    assert not any(
        "response.header" in key for key in _http_span(exported_spans).attributes
    )
    assert marker not in _span_text(exported_spans)


@pytest.mark.asyncio
async def test_request_body_is_not_exported(
    exchange: HttpExchange, exported_spans: list[ReadableSpan]
) -> None:
    """実際に送った本文はspanに記録されない。"""
    marker = "request-body-sentinel"
    async with make_external_async_client() as client:
        await client.post(_URL, content=marker, headers={"Content-Type": "text/plain"})

    assert exchange.request_bodies == [marker.encode()]
    assert not any(
        "request.body" in key for key in _http_span(exported_spans).attributes
    )
    assert marker not in _span_text(exported_spans)


@pytest.mark.asyncio
async def test_response_body_is_not_exported(
    exchange: HttpExchange, exported_spans: list[ReadableSpan]
) -> None:
    """読み取った応答本文はspanに記録されない。"""
    marker = "response-body-sentinel"
    exchange.body.chunks = (marker.encode(),)
    exchange.response_headers = [(b"content-type", b"text/plain")]
    async with make_external_async_client() as client:
        response = await client.get(_URL)

    assert response.text == marker
    assert not any(
        "response.body" in key for key in _http_span(exported_spans).attributes
    )
    assert marker not in _span_text(exported_spans)


@pytest.mark.asyncio
async def test_stream_is_read_lazily(
    exchange: HttpExchange, exported_spans: list[ReadableSpan]
) -> None:
    """計測がstream本文を先読みせず、呼び出し側が全チャンクを取得できる。"""
    exchange.body.chunks = (b"first", b"second")
    async with make_external_async_client() as client:
        async with client.stream("GET", _URL) as response:
            assert exchange.body.consumed == []
            assert [chunk async for chunk in response.aiter_bytes()] == [
                b"first",
                b"second",
            ]

    _http_span(exported_spans)


@pytest.mark.asyncio
async def test_stream_is_closed_on_context_exit(
    exchange: HttpExchange, exported_spans: list[ReadableSpan]
) -> None:
    """本文を読み切らなくてもstreamの終了時に元の応答が閉じられる。"""
    async with make_external_async_client() as client:
        async with client.stream("GET", _URL):
            assert not exchange.body.closed

    assert exchange.body.closed


@pytest.mark.asyncio
async def test_timeout_propagates_without_retry(
    exchange: HttpExchange, exported_spans: list[ReadableSpan]
) -> None:
    """計測を通ってもread timeoutはHTTPXの同じ例外型として1回で伝播する。"""
    exchange.error = httpcore.ReadTimeout("read deadline exceeded")
    async with make_external_async_client() as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.get(_URL)

    assert len(exchange.requests) == 1


@pytest.mark.asyncio
async def test_timeout_exports_failed_span(
    exchange: HttpExchange, exported_spans: list[ReadableSpan]
) -> None:
    """read timeoutのspanには失敗状態と例外型が残る。"""
    exchange.error = httpcore.ReadTimeout("read deadline exceeded")
    async with make_external_async_client() as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.get(_URL)

    span = _http_span(exported_spans)
    assert span.status.status_code == StatusCode.ERROR
    event = next(event for event in span.events if event.name == "exception")
    assert event.attributes["exception.type"] == "httpx.ReadTimeout"


@pytest.mark.asyncio
async def test_http_exception_free_text_is_redacted(
    exchange: HttpExchange, exported_spans: list[ReadableSpan]
) -> None:
    """通信例外の自由文は本番bootstrapが組み込むredactorで伏せられる。"""
    marker = "private-article-sentinel@example.com"
    exchange.error = httpcore.ReadTimeout(marker)
    async with make_external_async_client() as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.get(_URL)

    span = _http_span(exported_spans)
    event = next(event for event in span.events if event.name == "exception")
    assert event.attributes["exception.message"] == "[redacted]"
    assert event.attributes["exception.stacktrace"] == "[redacted]"
    assert span.status.description == "[redacted]"
    assert marker not in _span_text(exported_spans)
