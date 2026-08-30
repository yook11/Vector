"""LogfireLlmCallRecorder の契約。"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from opentelemetry.trace import StatusCode

from app.agent.recording.llm import LogfireLlmCallRecorder
from app.agent.recording.types import Usage
from app.agent.runtime.llm_failure import LlmAttemptFailed
from tests.agent.runtime._tracing_helpers import (
    exception_events,
    one_provider_attempt_span,
)
from tests.logfire._metric_helpers import attributes_of, collected_metrics

_OUTCOME_METRIC = "vector.agent.llm_call.outcome"
_DURATION_METRIC = "vector.agent.llm_call.duration"
_TOKENS_METRIC = "vector.agent.llm_call.tokens"

_RECORD_KWARGS = {
    "agent_name": "question_planner",
    "provider": "gemini",
    "model": "gemini-test-model",
    "attempt_number": 1,
    "prompt_version": "v1",
    "operation_name": "generate_content",
    "gen_ai_provider": "gcp.gemini",
    "mode": "call",
}


def test_failed_outcome_rejects_empty_failure_code() -> None:
    """分類済み失敗は空の failure_code を拒否する。"""

    with pytest.raises(ValueError):
        LlmAttemptFailed(failure_code="")


async def test_success_emits_outcome_duration_and_present_tokens(
    capfire: CaptureLogfire,
) -> None:
    """正常終了は成功として終わり方・所要時間・存在する token を残す。"""

    recorder = LogfireLlmCallRecorder()
    async with recorder.record(**_RECORD_KWARGS) as recording:
        recording.report_usage(Usage(input_tokens=11, output_tokens=7))

    metrics = collected_metrics(capfire)
    expected = {
        "agent_name": "question_planner",
        "provider": "gemini",
        "model": "gemini-test-model",
        "attempt_number": 1,
        "status": "completed",
        "result": "succeeded",
    }
    assert attributes_of(metrics, _OUTCOME_METRIC) == expected
    duration = next(item for item in metrics if item["name"] == _DURATION_METRIC)
    point = duration["data"]["data_points"][0]
    assert point["attributes"] == expected
    assert point["count"] == 1
    assert point["sum"] >= 0
    token_attrs = [
        dp["attributes"]
        for dp in next(item for item in metrics if item["name"] == _TOKENS_METRIC)[
            "data"
        ]["data_points"]
    ]
    assert token_attrs == [
        {**expected, "direction": "input"},
        {**expected, "direction": "output"},
    ]
    span = one_provider_attempt_span(capfire)
    assert dict(span.attributes or {})["result"] == "succeeded"
    assert exception_events(span) == []


async def test_classified_failure_records_failed_result_and_failure_code(
    capfire: CaptureLogfire,
) -> None:
    """分類済み失敗の outcome だけに result=failed と failure_code を付ける。"""

    recorder = LogfireLlmCallRecorder()
    error = RuntimeError("classified by runtime")
    with pytest.raises(RuntimeError) as raised:
        async with recorder.record(**_RECORD_KWARGS) as recording:
            recording.report_outcome(
                LlmAttemptFailed(failure_code="ai_error_network"),
                span_result="provider_error",
            )
            recording.report_usage(Usage(input_tokens=11, output_tokens=7))
            raise error

    assert raised.value is error
    metrics = collected_metrics(capfire)
    expected = {
        "agent_name": "question_planner",
        "provider": "gemini",
        "model": "gemini-test-model",
        "attempt_number": 1,
        "status": "failed",
        "result": "failed",
    }
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        **expected,
        "failure_code": "ai_error_network",
    }
    duration = next(item for item in metrics if item["name"] == _DURATION_METRIC)
    assert duration["data"]["data_points"][0]["attributes"] == expected
    token_attrs = [
        dp["attributes"]
        for dp in next(item for item in metrics if item["name"] == _TOKENS_METRIC)[
            "data"
        ]["data_points"]
    ]
    assert token_attrs == [
        {**expected, "direction": "input"},
        {**expected, "direction": "output"},
    ]
    span = one_provider_attempt_span(capfire)
    attributes = dict(span.attributes or {})
    assert attributes["result"] == "provider_error"
    assert attributes["error.type"] == "ai_error_network"
    assert span.status.status_code is StatusCode.ERROR
    assert exception_events(span) == []


async def test_stopped_attempt_records_duration_without_outcome(
    capfire: CaptureLogfire,
) -> None:
    """途中停止は duration だけを result=none で残し、outcome は打たない。"""

    recorder = LogfireLlmCallRecorder()
    error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as raised:
        async with recorder.record(
            **{**_RECORD_KWARGS, "agent_name": "direct_answer", "attempt_number": 2}
        ):
            raise error

    assert raised.value is error
    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "agent_name": "direct_answer",
        "provider": "gemini",
        "model": "gemini-test-model",
        "attempt_number": 2,
        "status": "stopped",
        "result": "none",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)
    assert all(item["name"] != _TOKENS_METRIC for item in metrics)
    span = one_provider_attempt_span(capfire)
    assert "result" not in dict(span.attributes or {})
    assert exception_events(span) == []
    assert span.status.status_code is not StatusCode.ERROR


async def test_stopped_attempt_discards_reported_failure(
    capfire: CaptureLogfire,
) -> None:
    """途中停止では報告済みの失敗を記録しない。"""

    recorder = LogfireLlmCallRecorder()
    with pytest.raises(asyncio.CancelledError):
        async with recorder.record(**_RECORD_KWARGS) as recording:
            recording.report_outcome(
                LlmAttemptFailed(failure_code="ai_error_network"),
                span_result="provider_error",
            )
            raise asyncio.CancelledError()

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC)["result"] == "none"
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)


async def test_unclassified_error_records_duration_without_outcome(
    capfire: CaptureLogfire,
) -> None:
    """未分類の終わりは duration だけを result=none で残す。"""

    recorder = LogfireLlmCallRecorder()
    error = RuntimeError("unclassified")
    with pytest.raises(RuntimeError) as raised:
        async with recorder.record(**_RECORD_KWARGS):
            raise error

    assert raised.value is error
    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "agent_name": "question_planner",
        "provider": "gemini",
        "model": "gemini-test-model",
        "attempt_number": 1,
        "status": "failed",
        "result": "none",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)
    span = one_provider_attempt_span(capfire)
    assert "result" not in dict(span.attributes or {})
    assert exception_events(span)


async def test_clock_failure_skips_duration(
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    """clock が取れなければ duration を打たない。"""

    def _boom() -> float:
        raise RuntimeError("clock failed")

    monkeypatch.setattr("app.agent.recording.llm.perf_counter", _boom)
    async with LogfireLlmCallRecorder().record(**_RECORD_KWARGS):
        pass

    metrics = collected_metrics(capfire)
    assert all(item["name"] != _DURATION_METRIC for item in metrics)
    assert attributes_of(metrics, _OUTCOME_METRIC)["result"] == "succeeded"


async def test_outcome_failure_does_not_change_business_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metric 記録の失敗を呼び出し元へ出さない。"""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("metric failed")

    monkeypatch.setattr(
        "app.agent.recording.llm._outcome_counter.add",
        _boom,
    )
    async with LogfireLlmCallRecorder().record(**_RECORD_KWARGS):
        pass


@pytest.mark.parametrize("failure_point", ["create", "enter", "exit"])
async def test_span_failure_does_not_change_business_result(
    failure_point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSpan:
        def __enter__(self) -> FailingSpan:
            if failure_point == "enter":
                raise RuntimeError("span enter failed")
            return self

        def __exit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: TracebackType | None,
        ) -> None:
            if failure_point == "exit":
                raise RuntimeError("span exit failed")

        def set_attribute(self, *_args: object, **_kwargs: object) -> None:
            return None

        def set_status(self, *_args: object, **_kwargs: object) -> None:
            return None

    def _span(*_args: object, **_kwargs: object) -> FailingSpan:
        if failure_point == "create":
            raise RuntimeError("span create failed")
        return FailingSpan()

    monkeypatch.setattr("app.agent.recording.llm.logfire.span", _span)
    async with LogfireLlmCallRecorder().record(**_RECORD_KWARGS):
        pass


async def test_duration_failure_does_not_block_outcome(
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    """duration 記録の失敗を outcome から独立させる。"""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("duration failed")

    monkeypatch.setattr(
        "app.agent.recording.llm._duration_histogram.record",
        _boom,
    )
    async with LogfireLlmCallRecorder().record(**_RECORD_KWARGS):
        pass

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC)["result"] == (
        "succeeded"
    )


async def test_span_exit_failure_does_not_replace_business_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSpan:
        def __enter__(self) -> FailingSpan:
            return self

        def __exit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: TracebackType | None,
        ) -> None:
            raise RuntimeError("span exit failed")

        def set_attribute(self, *_args: object, **_kwargs: object) -> None:
            return None

        def set_status(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(
        "app.agent.recording.llm.logfire.span",
        lambda *_args, **_kwargs: FailingSpan(),
    )
    business_error = ValueError("business failed")

    with pytest.raises(ValueError) as raised:
        async with LogfireLlmCallRecorder().record(**_RECORD_KWARGS):
            raise business_error

    assert raised.value is business_error


async def test_report_outcome_rejects_unknown_span_result() -> None:
    """span_result は provider-attempt の失敗桶だけを受け付ける。"""

    with pytest.raises(ValueError):
        async with LogfireLlmCallRecorder().record(**_RECORD_KWARGS) as recording:
            recording.report_outcome(
                LlmAttemptFailed(failure_code="ai_error_network"),
                span_result="succeeded",
            )


async def test_call_mode_does_not_open_detached_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = _FakeTracer()
    monkeypatch.setattr("app.agent.recording.llm._TRACER", tracer)
    async with LogfireLlmCallRecorder().record(**_RECORD_KWARGS):
        pass

    assert tracer.spans == []


async def test_stream_mode_opens_detached_span_with_parent_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = _FakeTracer()
    parent = object()
    monkeypatch.setattr("app.agent.recording.llm._TRACER", tracer)

    async with LogfireLlmCallRecorder().record(
        **{**_RECORD_KWARGS, "mode": "stream"},
        parent_context=parent,
    ) as recording:
        recording.report_usage(Usage(input_tokens=11))

    assert tracer.start_contexts == [parent]
    span = tracer.spans[0]
    assert span.end_calls == 1
    assert span.attributes["result"] == "succeeded"
    assert span.attributes["gen_ai.usage.input_tokens"] == 11
    assert span.exception_events == []


async def test_stream_classified_failure_has_no_exception_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = _FakeTracer()
    monkeypatch.setattr("app.agent.recording.llm._TRACER", tracer)
    error = RuntimeError("classified")

    with pytest.raises(RuntimeError) as raised:
        async with LogfireLlmCallRecorder().record(
            **{**_RECORD_KWARGS, "mode": "stream"}
        ) as recording:
            recording.report_outcome(
                LlmAttemptFailed(failure_code="ai_error_network"),
                span_result="blocked",
            )
            raise error

    assert raised.value is error
    span = tracer.spans[0]
    assert span.attributes["result"] == "blocked"
    assert span.attributes["error.type"] == "ai_error_network"
    assert span.status_code is StatusCode.ERROR
    assert span.exception_events == []
    assert span.end_calls == 1


async def test_stream_unclassified_error_records_exception_without_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer = _FakeTracer()
    monkeypatch.setattr("app.agent.recording.llm._TRACER", tracer)
    error = RuntimeError("unclassified stream")

    with pytest.raises(RuntimeError) as raised:
        async with LogfireLlmCallRecorder().record(
            **{**_RECORD_KWARGS, "mode": "stream"}
        ):
            raise error

    assert raised.value is error
    span = tracer.spans[0]
    assert "result" not in span.attributes
    assert span.status_code is StatusCode.ERROR
    assert span.exception_events == [error]
    assert span.end_calls == 1


class _FakeStreamSpan:
    def __init__(self, attributes: dict[str, Any] | None) -> None:
        self.attributes = dict(attributes or {})
        self.end_calls = 0
        self.exception_events: list[BaseException] = []
        self.status_code: object | None = None

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_status(self, status_code: object, _description: str | None = None) -> None:
        self.status_code = status_code

    def record_exception(self, error: BaseException) -> None:
        self.exception_events.append(error)

    def end(self) -> None:
        self.end_calls += 1


class _FakeTracer:
    def __init__(self) -> None:
        self.spans: list[_FakeStreamSpan] = []
        self.start_contexts: list[object] = []

    def start_span(
        self,
        _name: str,
        *,
        context: object | None = None,
        attributes: dict[str, Any] | None = None,
        **_kwargs: object,
    ) -> _FakeStreamSpan:
        span = _FakeStreamSpan(attributes)
        self.spans.append(span)
        self.start_contexts.append(context)
        return span
