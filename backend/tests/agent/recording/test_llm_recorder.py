"""LogfireLlmCallRecorder と span result 写像の契約。"""

from __future__ import annotations

import pytest
from logfire.testing import CaptureLogfire

from app.agent.recording.llm import (
    LogfireLlmCallRecorder,
    outcome_from_span_result,
)
from app.agent.recording.types import LlmCall, LlmCallResult, Usage
from tests.logfire._metric_helpers import attributes_of, collected_metrics

_OUTCOME_METRIC = "vector.agent.llm_call.outcome"
_DURATION_METRIC = "vector.agent.llm_call.duration"
_TOKENS_METRIC = "vector.agent.llm_call.tokens"


@pytest.mark.parametrize(
    ("span_result", "result"),
    [
        ("succeeded", LlmCallResult.SUCCEEDED),
        ("blocked", LlmCallResult.BLOCKED),
        ("invalid_response", LlmCallResult.INVALID_RESPONSE),
        ("provider_error", LlmCallResult.PROVIDER_ERROR),
    ],
)
def test_span_result_maps_to_llm_result(
    span_result: str,
    result: LlmCallResult,
) -> None:
    """span の result から LLM 結論だけを決める。"""

    assert outcome_from_span_result(span_result) is result


def test_unknown_span_result_is_rejected() -> None:
    """未知の span result を結論へ勝手に足さない。"""

    with pytest.raises(ValueError):
        outcome_from_span_result("unknown")


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
        result=LlmCallResult.SUCCEEDED,
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


@pytest.mark.parametrize(
    ("result", "status_label", "result_label"),
    [
        (LlmCallResult.BLOCKED, "failed", "blocked"),
        (LlmCallResult.INVALID_RESPONSE, "failed", "invalid_response"),
        (LlmCallResult.PROVIDER_ERROR, "failed", "provider_error"),
    ],
)
async def test_failed_results_record_failed_status(
    capfire: CaptureLogfire,
    result: LlmCallResult,
    status_label: str,
    result_label: str,
) -> None:
    """失敗結論は status=failed と result ラベルへ写す。"""

    recorder = LogfireLlmCallRecorder()
    call = recorder.start(
        agent_name="question_planner",
        provider="gemini",
        model="gemini-test-model",
        attempt_number=1,
    )
    recorder.end(call, result=result)

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC) == {
        "agent_name": "question_planner",
        "provider": "gemini",
        "model": "gemini-test-model",
        "status": status_label,
        "result": result_label,
    }


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


async def test_end_without_result_records_failed_status(
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
    recorder.end(call, result=LlmCallResult.SUCCEEDED)
