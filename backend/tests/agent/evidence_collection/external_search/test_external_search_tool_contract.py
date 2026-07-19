"""External Search Tool の公開契約とTavily adapter境界のテスト。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import Any, Literal, get_args, get_origin, get_type_hints

import httpx
import logfire
import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import SecretStr

from app.agent.agent import Agent
from app.agent.planning.contract import ExternalResearchTask
from tests.logfire._span_helpers import domain_attr_keys, one_span_named

_TOOL_SPAN_NAME = "external_search_tool_call"
_ANSWERING_SPAN_NAME = "agent_answering_run"


def _required_module(module_name: str) -> ModuleType:
    try:
        return import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"PR3 external search tool module is missing: {module_name} ({exc.name})"
        )


def _required_attribute(module: ModuleType, name: str) -> Any:
    if not hasattr(module, name):
        pytest.fail(
            f"PR3 external search tool contract is missing: {module.__name__}.{name}"
        )
    return getattr(module, name)


def _contracts() -> ModuleType:
    return _required_module("app.agent.evidence_collection.external_search.contract")


def _package() -> ModuleType:
    return _required_module("app.agent.evidence_collection.external_search")


def _runner_type() -> type[Any]:
    return _required_attribute(
        _required_module("app.agent.evidence_collection.external_search.runner"),
        "ExternalSearchResearchRunner",
    )


def _query_agent() -> Agent[Any, Any]:
    return _required_attribute(
        _required_module("app.agent.evidence_collection.external_search.agent"),
        "EXTERNAL_QUERY_AGENT",
    )


def _selector_agent() -> Agent[Any, Any]:
    return _required_attribute(
        _required_module("app.agent.evidence_collection.external_search.agent"),
        "EXTERNAL_EVIDENCE_SELECTOR_AGENT",
    )


def _tool_input(*, query: str, limit: int) -> Any:
    return _required_attribute(_contracts(), "ExternalSearchToolInput")(
        query=query,
        limit=limit,
    )


def _candidate(*, url: str, title: str, snippet: str) -> Any:
    return _required_attribute(_contracts(), "ExternalSearchCandidate")(
        url=url,
        title=title,
        snippet=snippet,
        source_name="SOURCE_NAME_SENTINEL_TOOL_5c8f",
    )


def _query_draft(queries: list[str]) -> Any:
    return _required_attribute(_contracts(), "ExternalQueryDraft").model_validate(
        {"queries": queries}
    )


def _selection_draft() -> Any:
    return _required_attribute(
        _contracts(), "ExternalEvidenceSelectionDraft"
    ).model_validate({"selections": [], "missing": []})


def _request() -> Any:
    return _required_attribute(_contracts(), "ExternalSearchRequest")(
        tasks=[ExternalResearchTask(collection_goal="GOAL_SENTINEL_TOOL_776d")],
        effective_agent_count=1,
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
        target_time_window="WINDOW_SENTINEL_TOOL_1f93",
    )


@dataclass(frozen=True, slots=True)
class RuntimeCall:
    agent: Agent[Any, Any]
    input: object
    attempt_number: int


class StaticRuntime:
    def __init__(self, outcomes: Sequence[object | BaseException]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[RuntimeCall] = []

    async def invoke[InputT, OutputT](
        self,
        agent: Agent[InputT, OutputT],
        input: InputT,
        *,
        attempt_number: int,
    ) -> OutputT:
        self.calls.append(RuntimeCall(agent, input, attempt_number))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


class FakeExternalSearchTool:
    def __init__(self, outcomes: Sequence[object | BaseException]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[object] = []

    @property
    def name(self) -> Literal["external_search"]:
        return "external_search"

    async def invoke(self, input: object) -> list[Any]:
        self.calls.append(input)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


class BlockingExternalSearchTool:
    def __init__(self) -> None:
        self.cancelled = False

    @property
    def name(self) -> Literal["external_search"]:
        return "external_search"

    async def invoke(self, input: object) -> list[Any]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class FakeTavilyHttpClient:
    def __init__(self, outcomes: Sequence[httpx.Response | BaseException]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, object, object, float]] = []

    async def post(
        self,
        url: str,
        *,
        headers: object,
        json: object,
        timeout: float,
    ) -> httpx.Response:
        self.calls.append((url, headers, json, timeout))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BlockingTavilyHttpClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def post(
        self,
        url: str,
        *,
        headers: object,
        json: object,
        timeout: float,
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


def _runner(*, search_tool: object) -> Any:
    return _runner_type()(
        query_agent=_query_agent(),
        query_runtime=StaticRuntime([_query_draft(["  normalized query  "])]),
        search_tool=search_tool,
        selector_agent=_selector_agent(),
        selector_runtime=StaticRuntime([_selection_draft()]),
    )


def _tavily_tool(client: object, *, api_key: str = "TOOL_SECRET_SENTINEL_d5e1") -> Any:
    return _required_attribute(_package(), "TavilyExternalSearchTool")(
        api_key=SecretStr(api_key),
        client=client,
    )


def _tool_spans(capfire: CaptureLogfire) -> list[ReadableSpan]:
    return [
        span
        for span in capfire.exporter.exported_spans
        if span.name == _TOOL_SPAN_NAME
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]


def _one_tool_span(capfire: CaptureLogfire) -> ReadableSpan:
    spans = _tool_spans(capfire)
    assert len(spans) == 1, (
        f"expected exactly one {_TOOL_SPAN_NAME} span, got {len(spans)}"
    )
    return spans[0]


def _span_text(span: ReadableSpan) -> str:
    values = [span.status.description or ""]
    values.extend(str(value) for value in (span.attributes or {}).values())
    for event in span.events:
        values.append(event.name)
        values.extend(str(value) for value in (event.attributes or {}).values())
    return " ".join(values)


def test_external_search_tool_port_and_tavily_adapter_are_stably_typed() -> None:
    contracts = _contracts()
    input_type = _required_attribute(contracts, "ExternalSearchToolInput")
    tool_port = _required_attribute(contracts, "ExternalSearchTool")
    candidate_type = _required_attribute(contracts, "ExternalSearchCandidate")
    tool_type = _required_attribute(_package(), "TavilyExternalSearchTool")

    assert is_dataclass(input_type)
    assert [field.name for field in fields(input_type)] == ["query", "limit"]
    assert get_type_hints(input_type) == {"query": str, "limit": int}
    assert get_type_hints(tool_port.invoke) == {
        "input": input_type,
        "return": list[candidate_type],
    }
    name_property = tool_port.__dict__["name"]
    name_type = get_type_hints(name_property.fget)["return"]
    assert get_origin(name_type) is Literal
    assert get_args(name_type) == ("external_search",)
    tool = tool_type(api_key=SecretStr("test-key"), client=object())
    assert tool.name == "external_search"
    assert not hasattr(tool, "search")


@pytest.mark.asyncio
async def test_runner_passes_normalized_queries_and_limit_to_typed_tool_input() -> None:
    candidate = _candidate(
        url="https://example.com/TOOL_URL_SENTINEL_203b",
        title="TOOL_TITLE_SENTINEL_4f74",
        snippet="TOOL_SNIPPET_SENTINEL_7a92",
    )
    tool = FakeExternalSearchTool([[candidate]])

    result = await _runner(search_tool=tool).search(_request())

    assert tool.calls == [_tool_input(query="normalized query", limit=10)]
    assert result.task_reports[0].candidate_count == 1


@pytest.mark.asyncio
async def test_runner_keeps_query_backstop_around_tool_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_module = _required_module(
        "app.agent.evidence_collection.external_search.runner"
    )
    assert runner_module.PROVIDER_SEARCH_TIMEOUT_SECONDS == 15
    monkeypatch.setattr(runner_module, "PROVIDER_SEARCH_TIMEOUT_SECONDS", 0.01)
    tool = BlockingExternalSearchTool()
    selector_runtime = StaticRuntime([_selection_draft()])
    runner = _runner_type()(
        query_agent=_query_agent(),
        query_runtime=StaticRuntime([_query_draft(["normalized query"])]),
        search_tool=tool,
        selector_agent=_selector_agent(),
        selector_runtime=selector_runtime,
    )

    result = await runner.search(_request())

    assert tool.cancelled is True
    assert selector_runtime.calls == []
    assert result.task_reports[0].status == "provider_failed"
    assert result.task_reports[0].provider_failed_query_count == 1


@pytest.mark.asyncio
async def test_successful_tool_call_has_one_safe_client_span_in_answer_trace(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinels = {
        "query": "TOOL_QUERY_SENTINEL_0c36",
        "url": (
            "https://source-name-sentinel-tool-5c8f.example/TOOL_URL_SENTINEL_903b"
        ),
        "title": "TOOL_TITLE_SENTINEL_690a",
        "snippet": "TOOL_SNIPPET_SENTINEL_62d8",
        "source": "source-name-sentinel-tool-5c8f.example",
        "published": "2026-07-19T12:34:56+00:00",
        "provider_response": "PROVIDER_RESPONSE_SENTINEL_TOOL_c54a",
        "secret": "TOOL_SECRET_SENTINEL_d5e1",
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
                        "provider_internal": sentinels["provider_response"],
                        "results": [
                            {
                                "url": sentinels["url"],
                                "title": sentinels["title"],
                                "content": sentinels["snippet"],
                                "published_date": sentinels["published"],
                            }
                        ],
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
        tool = _tavily_tool(client, api_key=sentinels["secret"])
        with logfire.span(_ANSWERING_SPAN_NAME):
            candidates = await tool.invoke(
                _tool_input(query=sentinels["query"], limit=1)
            )

    span = _one_tool_span(capfire)
    span_dict = one_span_named(capfire, _TOOL_SPAN_NAME)
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
    assert len(candidates) == 1
    assert span.kind is SpanKind.CLIENT
    assert domain_attr_keys(span_dict["attributes"]) == {"tool_name", "candidate_count"}
    assert span_dict["attributes"]["tool_name"] == "external_search"
    assert span_dict["attributes"]["candidate_count"] == 1
    assert span_dict["context"]["trace_id"] == answer_span["context"]["trace_id"]
    assert len(http_spans) == 1
    assert http_spans[0].parent is not None
    assert http_spans[0].parent.span_id == span.context.span_id
    assert all(sentinel not in _span_text(span) for sentinel in sentinels.values())
    assert all(sentinel not in trace_dump for sentinel in sentinels.values())


@pytest.mark.asyncio
async def test_classified_tool_failure_uses_closed_reason_without_exception_event(
    capfire: CaptureLogfire,
) -> None:
    sentinels = {
        "query": "TOOL_QUERY_SENTINEL_FAILURE_b23d",
        "response": "RESPONSE_BODY_SENTINEL_TOOL_96b2",
        "secret": "TOOL_SECRET_SENTINEL_FAILURE_3ec0",
    }
    tool = _tavily_tool(
        FakeTavilyHttpClient(
            [httpx.Response(429, json={"error": sentinels["response"]})]
        ),
        api_key=sentinels["secret"],
    )

    with pytest.raises(
        _required_attribute(_contracts(), "ExternalSearchProviderError")
    ) as raised:
        await tool.invoke(_tool_input(query=sentinels["query"], limit=1))

    span = _one_tool_span(capfire)
    attributes = dict(span.attributes or {})
    trace_dump = json.dumps(
        capfire.exporter.exported_spans_as_dict(), ensure_ascii=False, default=str
    )
    assert raised.value.reason == "tavily_search_http_status_429"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description in (None, "")
    assert attributes["error.type"] == "tavily_search_http_status_429"
    assert "candidate_count" not in attributes
    assert not [event for event in span.events if event.name == "exception"]
    assert all(sentinel not in _span_text(span) for sentinel in sentinels.values())
    assert all(sentinel not in trace_dump for sentinel in sentinels.values())


def test_classified_tool_error_rejects_arbitrary_reason_values() -> None:
    error_type = _required_attribute(_contracts(), "ExternalSearchProviderError")
    error = error_type(reason="tavily_search_http_error")

    assert error.reason == "tavily_search_http_error"
    with pytest.raises((TypeError, ValueError)):
        error_type(reason="ARBITRARY_REASON_SENTINEL_TOOL_1d2e")
    with pytest.raises((TypeError, ValueError)):
        error_type(reason="tavily_search_http_status_４２９")


@pytest.mark.asyncio
async def test_tool_timeout_cancels_invoke_without_fabricating_span_values(
    capfire: CaptureLogfire,
) -> None:
    client = BlockingTavilyHttpClient()
    tool = _tavily_tool(client)
    invocation = asyncio.create_task(
        tool.invoke(_tool_input(query="TOOL_QUERY_SENTINEL_CANCEL_651e", limit=1))
    )
    await client.started.wait()
    invocation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await invocation

    span = _one_tool_span(capfire)
    attributes = dict(span.attributes or {})
    assert client.cancelled is True
    assert attributes["tool_name"] == "external_search"
    assert "candidate_count" not in attributes
    assert "error.type" not in attributes
