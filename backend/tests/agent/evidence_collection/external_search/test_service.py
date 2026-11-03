"""ExternalSearchService.search の工程記録契約。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import StatusCode

from app.agent.evidence_collection.external_search.agent import EXTERNAL_QUERY_AGENT
from app.agent.evidence_collection.external_search.contract import (
    ExternalQueryDraft,
    ExternalSearchFailureCode,
    ExternalSearchHit,
    ExternalSearchProviderError,
)
from app.agent.evidence_collection.external_search.service import ExternalSearchService
from app.agent.recording.external_search import (
    ExternalSearchFailed,
    ExternalSearchSucceeded,
)
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.ai_providers.errors import AIProviderResultError, AIProviderResultReason
from tests.agent.recording._fakes import RecordingExternalSearchRecorder
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._span_helpers import exception_event, spans_named

_AS_OF = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _query_draft(queries: list[str]) -> ExternalQueryDraft:
    return ExternalQueryDraft(queries=queries)


def _hit(url: str) -> ExternalSearchHit:
    return ExternalSearchHit(url=url, title=url, content="content")


class _FakeGateway:
    def __init__(
        self,
        results_by_query: dict[str, list[ExternalSearchHit]] | None = None,
        *,
        errors_by_query: dict[str, BaseException] | None = None,
    ) -> None:
        self._results = results_by_query or {}
        self._errors = errors_by_query or {}
        self.calls: list[Any] = []

    async def search(self, request: Any) -> list[ExternalSearchHit]:
        self.calls.append(request)
        if request.query in self._errors:
            raise self._errors[request.query]
        return list(self._results.get(request.query, []))


def _service(
    runtime: ScriptedAgentRuntime,
    gateway: _FakeGateway | None = None,
    recorder: RecordingExternalSearchRecorder | None = None,
) -> tuple[ExternalSearchService, RecordingExternalSearchRecorder]:
    recording = recorder or RecordingExternalSearchRecorder()
    service = ExternalSearchService(
        query_runtime=runtime,
        search_gateway=gateway or _FakeGateway(),
        recorder=recording,
    )
    return service, recording


async def _search(service: ExternalSearchService) -> Any:
    return await service.search(
        research_goal="NVIDIA の供給",
        as_of=_AS_OF,
        target_time_window=None,
        task_index=0,
    )


async def test_search_records_completed_succeeded_when_hits_return() -> None:
    gateway = _FakeGateway({"q": [_hit("https://example.com/a")]})
    service, recorder = _service(
        ScriptedAgentRuntime([_query_draft(["q"])]),
        gateway,
    )

    execution = await _search(service)

    assert [str(hit.url) for hit in execution.hits] == ["https://example.com/a"]
    assert len(recorder.records) == 1
    recorded = recorder.records[0]
    assert recorded.outcomes == [ExternalSearchSucceeded()]
    assert recorded.error is None
    assert len(recorded.query_generations) == 1


async def test_search_records_completed_succeeded_for_zero_hits() -> None:
    service, recorder = _service(ScriptedAgentRuntime([_query_draft(["q"])]))

    execution = await _search(service)

    assert execution.hits == []
    recorded = recorder.records[0]
    assert recorded.outcomes == [ExternalSearchSucceeded()]
    assert recorded.error is None


async def test_search_records_completed_on_partial_provider_failure() -> None:
    gateway = _FakeGateway(
        {"ok": [_hit("https://example.com/ok")]},
        errors_by_query={
            "bad": ExternalSearchProviderError(reason="external_search_http_error")
        },
    )
    service, recorder = _service(
        ScriptedAgentRuntime([_query_draft(["ok", "bad"])]),
        gateway,
    )

    execution = await _search(service)

    assert execution.provider_failed_query_count == 1
    assert [str(hit.url) for hit in execution.hits] == ["https://example.com/ok"]
    recorded = recorder.records[0]
    assert recorded.outcomes == [ExternalSearchSucceeded()]
    assert recorded.error is None


async def test_search_records_completed_when_query_generation_fails() -> None:
    service, recorder = _service(
        ScriptedAgentRuntime(
            [AgentResponseInvalidError(AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH)]
        )
    )

    execution = await _search(service)

    assert execution.generated_queries == ()
    recorded = recorder.records[0]
    assert recorded.outcomes == [
        ExternalSearchFailed(
            failure_code=ExternalSearchFailureCode.QUERY_GENERATION_FAILED
        )
    ]
    assert recorded.error is None


@pytest.mark.parametrize(
    ("failure", "expected_error_type"),
    [
        pytest.param(
            AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON),
            "response_not_json",
            id="response-defect",
        ),
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.RESPONSE_UNPARSEABLE),
            "ai_provider_result_error",
            id="provider",
        ),
        pytest.param(TimeoutError(), "query_generation_timeout", id="timeout"),
    ],
)
async def test_failed_query_generation_closes_its_phase_as_failed_with_the_cause(
    failure: Exception,
    expected_error_type: str,
    capfire: CaptureLogfire,
) -> None:
    """クエリ生成の失敗は工程のspanを原因のcodeつきの失敗で閉じ、検索は空の結果で続ける。"""
    service = ExternalSearchService(
        query_runtime=ScriptedAgentRuntime([failure]),
        search_gateway=_FakeGateway(),
    )

    execution = await _search(service)

    phase = next(
        span
        for span in spans_named(capfire, "agent_phase")
        if span["attributes"].get("agent_name") == EXTERNAL_QUERY_AGENT.name
    )
    raw_phase = next(
        span
        for span in capfire.exporter.exported_spans
        if span.context is not None
        and span.context.span_id == phase["context"]["span_id"]
        and (span.attributes or {}).get("logfire.span_type") == "span"
    )
    assert (
        execution.generated_queries,
        raw_phase.status.status_code,
        phase["attributes"][ERROR_TYPE],
        exception_event(phase),
    ) == ((), StatusCode.ERROR, expected_error_type, None)


async def test_search_records_completed_when_every_provider_call_fails() -> None:
    gateway = _FakeGateway(
        errors_by_query={
            "q1": ExternalSearchProviderError(reason="external_search_http_error"),
            "q2": ExternalSearchProviderError(reason="external_search_http_error"),
        }
    )
    service, recorder = _service(
        ScriptedAgentRuntime([_query_draft(["q1", "q2"])]),
        gateway,
    )

    execution = await _search(service)

    assert execution.hits == []
    recorded = recorder.records[0]
    assert recorded.outcomes == [
        ExternalSearchFailed(failure_code=ExternalSearchFailureCode.SEARCH_FAILED)
    ]
    assert recorded.error is None


async def test_search_records_stopped_on_cancel() -> None:
    service, recorder = _service(ScriptedAgentRuntime([asyncio.CancelledError()]))

    with pytest.raises(asyncio.CancelledError):
        await _search(service)

    recorded = recorder.records[0]
    assert recorded.outcomes == []
    assert isinstance(recorded.error, asyncio.CancelledError)


async def test_search_records_failed_without_outcome_for_unclassified_error() -> None:
    gateway = _FakeGateway(errors_by_query={"q": RuntimeError("gateway bug")})
    service, recorder = _service(
        ScriptedAgentRuntime([_query_draft(["q"])]),
        gateway,
    )

    with pytest.raises(RuntimeError, match="gateway bug"):
        await _search(service)

    recorded = recorder.records[0]
    assert recorded.outcomes == []
    assert isinstance(recorded.error, RuntimeError)
