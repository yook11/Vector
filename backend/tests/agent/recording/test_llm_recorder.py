"""LogfireLlmCallRecorder の契約。"""

from __future__ import annotations

import pytest
from logfire.testing import CaptureLogfire

from app.agent.recording.llm import (
    LlmAttemptFailed,
    LlmAttemptSucceeded,
    LogfireLlmCallRecorder,
)
from app.agent.recording.types import LlmCall, Usage
from tests.logfire._metric_helpers import attributes_of, collected_metrics

_OUTCOME_METRIC = "vector.agent.llm_call.outcome"
_DURATION_METRIC = "vector.agent.llm_call.duration"
_TOKENS_METRIC = "vector.agent.llm_call.tokens"


def test_failed_outcome_rejects_empty_failure_code() -> None:
    """分類済み失敗は空の failure_code を拒否する。"""

    with pytest.raises(ValueError):
        LlmAttemptFailed(failure_code="")


async def test_logfire_recorder_emits_outcome_duration_and_present_tokens(
    capfire: CaptureLogfire,
) -> None:
    """1 attempt の終わり方・所要時間・存在する token だけを残す。"""

    recorder = LogfireLlmCallRecorder()
    call = recorder.start(
        agent_name="question_planner",
        provider="gemini",
        model="gemini-test-model",
        attempt_number=1,
    )
    recorder.end(
        call,
        outcome=LlmAttemptSucceeded(),
        usage=Usage(input_tokens=11, output_tokens=7),
    )

    metrics = collected_metrics(capfire)
    expected = {
        "agent_name": "question_planner",
        "provider": "gemini",
        "model": "gemini-test-model",
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


async def test_classified_failure_records_failed_result_and_failure_code(
    capfire: CaptureLogfire,
) -> None:
    """分類済み失敗の outcome だけに result=failed と failure_code を付ける。"""

    recorder = LogfireLlmCallRecorder()
    call = recorder.start(
        agent_name="question_planner",
        provider="gemini",
        model="gemini-test-model",
        attempt_number=1,
    )
    recorder.end(
        call,
        outcome=LlmAttemptFailed(failure_code="ai_error_network"),
        usage=Usage(input_tokens=11, output_tokens=7),
    )

    metrics = collected_metrics(capfire)
    expected = {
        "agent_name": "question_planner",
        "provider": "gemini",
        "model": "gemini-test-model",
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


async def test_stopped_attempt_records_result_none(
    capfire: CaptureLogfire,
) -> None:
    """途中停止では result ラベルを none にする。"""

    recorder = LogfireLlmCallRecorder()
    call = recorder.start(
        agent_name="direct_answer",
        provider="gemini",
        model="gemini-test-model",
        attempt_number=2,
    )
    recorder.end(call, stopped=True)

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "agent_name": "direct_answer",
        "provider": "gemini",
        "model": "gemini-test-model",
        "status": "stopped",
        "result": "none",
    }
    assert all(item["name"] != _TOKENS_METRIC for item in metrics)


async def test_stopped_attempt_ignores_outcome(
    capfire: CaptureLogfire,
) -> None:
    """途中停止では渡された結論型を記録しない。"""

    recorder = LogfireLlmCallRecorder()
    call = recorder.start(
        agent_name="direct_answer",
        provider="gemini",
        model="gemini-test-model",
        attempt_number=2,
    )
    recorder.end(
        call,
        outcome=LlmAttemptFailed(failure_code="ai_error_network"),
        stopped=True,
    )

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC) == {
        "agent_name": "direct_answer",
        "provider": "gemini",
        "model": "gemini-test-model",
        "status": "stopped",
        "result": "none",
    }


async def test_end_without_outcome_records_failed_status(
    capfire: CaptureLogfire,
) -> None:
    """未分類の終わりは status=failed、result=none にする。"""

    recorder = LogfireLlmCallRecorder()
    call = recorder.start(
        agent_name="question_planner",
        provider="gemini",
        model="gemini-test-model",
        attempt_number=1,
    )
    recorder.end(call)

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC) == {
        "agent_name": "question_planner",
        "provider": "gemini",
        "model": "gemini-test-model",
        "status": "failed",
        "result": "none",
    }


def test_start_returns_llm_call_when_clock_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start は失敗しても LlmCall を返す。"""

    def _boom() -> float:
        raise RuntimeError("clock failed")

    monkeypatch.setattr("app.agent.recording.llm.perf_counter", _boom)
    call = LogfireLlmCallRecorder().start(
        agent_name="question_planner",
        provider="gemini",
        model="gemini-test-model",
        attempt_number=1,
    )
    assert isinstance(call, LlmCall)


def test_end_does_not_propagate_metric_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metric 記録の失敗を呼び出し元へ出さない。"""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("metric failed")

    monkeypatch.setattr(
        "app.agent.recording.llm._outcome_counter.add",
        _boom,
    )
    recorder = LogfireLlmCallRecorder()
    call = recorder.start(
        agent_name="question_planner",
        provider="gemini",
        model="gemini-test-model",
        attempt_number=1,
    )
    recorder.end(call, outcome=LlmAttemptSucceeded())
