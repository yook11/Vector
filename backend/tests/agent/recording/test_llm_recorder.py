"""LogfireLlmCallRecorder と span result 写像の契約。"""

from __future__ import annotations

import json

import pytest
from logfire.testing import CaptureLogfire

from app.agent.recording.llm import (
    LogfireLlmCallRecorder,
    close_llm_call,
    outcome_from_span_result,
    usage_from_deepseek_usage,
    usage_from_gemini_metadata,
)
from app.agent.recording.types import LlmCallResult, PhaseStatus, Usage
from tests.agent.recording._fakes import RecordingLlmCallRecorder
from tests.logfire._metric_helpers import attributes_of, collected_metrics

_OUTCOME_METRIC = "vector.agent.llm_call.outcome"
_DURATION_METRIC = "vector.agent.llm_call.duration"
_TOKENS_METRIC = "vector.agent.llm_call.tokens"
_SENTINEL = "TASK_CONTENTS_SENTINEL_llm_call"


@pytest.mark.parametrize(
    ("span_result", "status", "result"),
    [
        ("succeeded", PhaseStatus.COMPLETED, LlmCallResult.SUCCEEDED),
        ("blocked", PhaseStatus.FAILED, LlmCallResult.BLOCKED),
        ("invalid_response", PhaseStatus.FAILED, LlmCallResult.INVALID_RESPONSE),
        ("provider_error", PhaseStatus.FAILED, LlmCallResult.PROVIDER_ERROR),
    ],
)
def test_span_result_maps_to_status_and_llm_result(
    span_result: str,
    status: PhaseStatus,
    result: LlmCallResult,
) -> None:
    """span の result から共通 status と LLM 結論を一意に決める。"""

    assert outcome_from_span_result(span_result) == (status, result)


def test_unknown_span_result_is_rejected() -> None:
    """未知の span result を結論へ勝手に足さない。"""

    with pytest.raises(ValueError):
        outcome_from_span_result("unknown")


def test_gemini_usage_keeps_missing_fields_unset() -> None:
    """欠損 token を 0 で埋めない。"""

    usage = usage_from_gemini_metadata(type("Usage", (), {"prompt_token_count": 11})())

    assert usage == Usage(input_tokens=11)


def test_deepseek_usage_reads_nested_cache_and_reasoning_tokens() -> None:
    """DeepSeek の入れ子 usage を optional な token 集合へ写す。"""

    usage = usage_from_deepseek_usage(
        type(
            "Usage",
            (),
            {
                "prompt_tokens": 4,
                "completion_tokens": 6,
                "prompt_tokens_details": type("D", (), {"cached_tokens": 1})(),
                "completion_tokens_details": type("D", (), {"reasoning_tokens": 2})(),
            },
        )()
    )

    assert usage == Usage(
        input_tokens=4,
        output_tokens=6,
        cache_read_input_tokens=1,
        reasoning_output_tokens=2,
    )


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
        status=PhaseStatus.COMPLETED,
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
    dumped = json.dumps(metrics, default=str)
    assert _SENTINEL not in dumped
    assert "run_id" not in dumped


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
    recorder.end(
        call,
        status=PhaseStatus.STOPPED,
        result=None,
        usage=None,
    )

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "agent_name": "direct_answer",
        "provider": "gemini",
        "model": "gemini-test-model",
        "status": "stopped",
        "result": "none",
    }
    assert all(item["name"] != _TOKENS_METRIC for item in metrics)


def test_close_llm_call_swallows_recorder_errors() -> None:
    """end の失敗で呼び出し元を落とさない。"""

    class _RaisingRecorder(RecordingLlmCallRecorder):
        def end(self, call, *, status, result, usage) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError("recorder failed")

    recorder = _RaisingRecorder()
    call = recorder.start(
        agent_name="question_planner",
        provider="gemini",
        model="gemini-test-model",
        attempt_number=1,
    )

    close_llm_call(
        recorder,
        call,
        status=PhaseStatus.FAILED,
        result=None,
        usage=None,
    )
