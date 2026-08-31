"""External search gateway の公開契約と adapter 境界のテスト。

対象は「いま配線されている gateway」= AgentCore adapter。Tavily は PR で
外されるまで test_tavily.py が wire format を持つ。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import httpx
import logfire
import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind, StatusCode

import app.agent.evidence_collection.external_search.agentcore as agentcore_module
from app.agent.evidence_collection.external_search import (
    AgentCoreWebSearchGateway,
)
from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchFailureReason,
    ExternalSearchProviderError,
    ExternalSearchRequest,
)
from app.agent.recording.external_search import (
    ExternalSearchSucceeded,
    LogfireExternalSearchRecorder,
)
from tests.logfire._span_helpers import domain_attr_keys, one_span_named

GATEWAY_URL = "https://gw-test.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com"
# 署名に実際に載る値。Authorization ヘッダの Credential= に現れるので、
# trace へ漏れないことの検証対象になる。
_ACCESS_KEY_SENTINEL = "TOOLACCESSKEYSENTINELD5E1"
_SECRET_KEY_SENTINEL = "TOOL_SECRET_SENTINEL_d5e1"
_SPAN_NAME = "external_search_call"
_EXTERNAL_SEARCH_SPAN_NAME = "external_search"
_ANSWERING_SPAN_NAME = "agent_answering_run"


def _search_request(
    *, query: str, limit: int, date_filter: object | None = None
) -> ExternalSearchRequest:
    return ExternalSearchRequest(
        query=query,
        limit=limit,
        date_filter=date_filter,
    )


class FakeGatewayHttpClient:
    def __init__(self, outcomes: Sequence[httpx.Response | BaseException]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, object, bytes]] = []

    async def post(
        self,
        url: str,
        *,
        headers: object,
        content: bytes,
    ) -> httpx.Response:
        self.calls.append((url, headers, content))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BlockingGatewayHttpClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def post(
        self,
        url: str,
        *,
        headers: object,
        content: bytes,
    ) -> httpx.Response:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class StaticAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def __aiter__(self):
        yield self._content


def _gateway(client: object) -> AgentCoreWebSearchGateway:
    return AgentCoreWebSearchGateway(
        gateway_url=GATEWAY_URL,
        region="ap-northeast-1",
        client=client,
    )


def _mcp_response(results: list[object]) -> httpx.Response:
    """MCP の二重 JSON 応答。tool 出力は content[0].text に JSON 文字列で載る。"""
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps({"results": results})}],
            },
        },
    )


@pytest.fixture(autouse=True)
def _static_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """署名は本物を通し、資格情報だけ固定値にする (実 AWS へ出ない)。"""
    from botocore.credentials import Credentials

    class _Session:
        def get_credentials(self) -> Credentials:
            return Credentials(_ACCESS_KEY_SENTINEL, _SECRET_KEY_SENTINEL)

    monkeypatch.setattr(agentcore_module, "_botocore_session", _Session)


def _gateway_spans(capfire: CaptureLogfire) -> list[ReadableSpan]:
    return [
        span
        for span in capfire.exporter.exported_spans
        if span.name == _SPAN_NAME
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]


def _one_gateway_span(capfire: CaptureLogfire) -> ReadableSpan:
    spans = _gateway_spans(capfire)
    assert len(spans) == 1, f"expected exactly one {_SPAN_NAME} span, got {len(spans)}"
    return spans[0]


def _span_text(span: ReadableSpan) -> str:
    values = [span.status.description or ""]
    values.extend(str(value) for value in (span.attributes or {}).values())
    for event in span.events:
        values.append(event.name)
        values.extend(str(value) for value in (event.attributes or {}).values())
    return " ".join(values)


@pytest.mark.asyncio
async def test_successful_call_has_one_safe_client_span_in_answer_trace(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinels = {
        "query": "TOOL_QUERY_SENTINEL_0c36",
        "url": (
            "https://source-name-sentinel-tool-5c8f.example/TOOL_URL_SENTINEL_903b"
        ),
        "title": "TOOL_TITLE_SENTINEL_690a",
        "content": "TOOL_CONTENT_SENTINEL_62d8",
        "source": "source-name-sentinel-tool-5c8f.example",
        "published": "12:34PM, Sunday, July 19 2026, PDT",
        "provider_response": "PROVIDER_RESPONSE_SENTINEL_TOOL_c54a",
        "access_key": _ACCESS_KEY_SENTINEL,
        "secret": _SECRET_KEY_SENTINEL,
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=StaticAsyncByteStream(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "isError": False,
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "provider_internal": sentinels[
                                                "provider_response"
                                            ],
                                            "results": [
                                                {
                                                    "url": sentinels["url"],
                                                    "title": sentinels["title"],
                                                    "text": sentinels["content"],
                                                    "publishedDate": sentinels[
                                                        "published"
                                                    ],
                                                }
                                            ],
                                        }
                                    ),
                                }
                            ],
                        },
                    }
                ).encode()
            ),
        )

    monkeypatch.setenv("LOGFIRE_HTTPX_CAPTURE_ALL", "true")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        logfire.instrument_httpx(
            client,
            capture_all=False,
            capture_headers=False,
            capture_request_body=False,
            capture_response_body=False,
        )
        gateway = _gateway(client)
        with logfire.span(_ANSWERING_SPAN_NAME):
            hits = await gateway.search(
                _search_request(query=sentinels["query"], limit=1)
            )

    span = _one_gateway_span(capfire)
    span_dict = one_span_named(capfire, _SPAN_NAME)
    answer_span = one_span_named(capfire, _ANSWERING_SPAN_NAME)
    http_spans = [
        exported_span
        for exported_span in capfire.exporter.exported_spans
        if exported_span.name == "POST"
        and exported_span.kind is SpanKind.CLIENT
        and (exported_span.attributes or {}).get("logfire.span_type") == "span"
    ]
    trace_dump = json.dumps(
        capfire.exporter.exported_spans_as_dict(), ensure_ascii=False, default=str
    )
    assert len(requests) == 1
    assert len(hits) == 1
    assert span.kind is SpanKind.CLIENT
    assert domain_attr_keys(span_dict["attributes"]) == {"hit_count"}
    assert span_dict["attributes"]["hit_count"] == 1
    assert span_dict["context"]["trace_id"] == answer_span["context"]["trace_id"]
    assert len(http_spans) == 1
    assert http_spans[0].parent is not None
    assert http_spans[0].parent.span_id == span.context.span_id
    assert all(sentinel not in _span_text(span) for sentinel in sentinels.values())
    assert all(sentinel not in trace_dump for sentinel in sentinels.values())


@pytest.mark.asyncio
async def test_gateway_span_is_child_of_external_search_span(
    capfire: CaptureLogfire,
) -> None:
    gateway = _gateway(
        FakeGatewayHttpClient(
            [
                _mcp_response(
                    [{"url": "https://example.com/a", "title": "a", "text": "body"}]
                )
            ]
        )
    )

    async with LogfireExternalSearchRecorder().record() as recording:
        await gateway.search(_search_request(query="query", limit=1))
        recording.report_outcome(ExternalSearchSucceeded())

    search_span = one_span_named(capfire, _EXTERNAL_SEARCH_SPAN_NAME)
    gateway_span = one_span_named(capfire, _SPAN_NAME)
    assert gateway_span["parent"]["span_id"] == search_span["context"]["span_id"]


@pytest.mark.asyncio
async def test_classified_failure_uses_closed_reason_without_exception_event(
    capfire: CaptureLogfire,
) -> None:
    sentinels = {
        "query": "TOOL_QUERY_SENTINEL_FAILURE_b23d",
        "response": "RESPONSE_BODY_SENTINEL_TOOL_96b2",
        "access_key": _ACCESS_KEY_SENTINEL,
        "secret": _SECRET_KEY_SENTINEL,
    }
    gateway = _gateway(
        FakeGatewayHttpClient(
            [httpx.Response(429, json={"error": sentinels["response"]})]
        )
    )

    with pytest.raises(ExternalSearchProviderError) as raised:
        await gateway.search(_search_request(query=sentinels["query"], limit=1))

    span = _one_gateway_span(capfire)
    attributes = dict(span.attributes or {})
    trace_dump = json.dumps(
        capfire.exporter.exported_spans_as_dict(), ensure_ascii=False, default=str
    )
    assert raised.value.reason == "external_search_http_status_429"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description in (None, "")
    assert attributes["error.type"] == "external_search_http_status_429"
    assert "hit_count" not in attributes
    assert not [event for event in span.events if event.name == "exception"]
    assert all(sentinel not in _span_text(span) for sentinel in sentinels.values())
    assert all(sentinel not in trace_dump for sentinel in sentinels.values())


def test_classified_error_accepts_every_static_reason_code() -> None:
    """status を伴わない全 reason が str 経路でも通ること。

    受理集合を手書きで並べると enum に member を足したときに黙って落ちる。
    HTTP_STATUS だけが status 付きで、それ以外は静的という契約を固定する。
    """
    static_members = [
        member
        for member in ExternalSearchFailureReason
        if member is not ExternalSearchFailureReason.HTTP_STATUS
    ]
    assert static_members  # 列挙が空なら以下の assert が空虚になる

    for member in static_members:
        assert ExternalSearchProviderError(reason=member.value).reason == member.value


def test_classified_error_rejects_arbitrary_reason_values() -> None:
    error_type = ExternalSearchProviderError
    error = error_type(reason="external_search_http_error")

    assert error.reason == "external_search_http_error"
    with pytest.raises((TypeError, ValueError)):
        error_type(reason="ARBITRARY_REASON_SENTINEL_TOOL_1d2e")
    with pytest.raises((TypeError, ValueError)):
        error_type(reason="external_search_http_status_４２９")


@pytest.mark.asyncio
async def test_timeout_cancels_search_without_fabricating_span_values(
    capfire: CaptureLogfire,
) -> None:
    client = BlockingGatewayHttpClient()
    gateway = _gateway(client)
    invocation = asyncio.create_task(
        gateway.search(
            _search_request(query="TOOL_QUERY_SENTINEL_CANCEL_651e", limit=1)
        )
    )
    await client.started.wait()
    invocation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await invocation

    span = _one_gateway_span(capfire)
    attributes = dict(span.attributes or {})
    assert client.cancelled is True
    assert domain_attr_keys(attributes) == set()
    assert "hit_count" not in attributes
    assert "error.type" not in attributes
