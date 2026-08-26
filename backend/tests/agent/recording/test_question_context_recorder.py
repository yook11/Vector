"""LogfireQuestionContextRecorder の契約。"""

from __future__ import annotations

import json

import pytest
from logfire.testing import CaptureLogfire

from app.agent.recording.question_context import LogfireQuestionContextRecorder
from app.agent.recording.types import PhaseCall
from tests.logfire._metric_helpers import attributes_of, collected_metrics

_OUTCOME_METRIC = "vector.agent.question_context.outcome"
_DURATION_METRIC = "vector.agent.question_context.duration"
_SENTINEL = "QUESTION_SENTINEL_question_context"
_PROMPT_VERSION = "question-context-v1"
_AI_MODEL = "gemini-test-model"


async def test_logfire_recorder_emits_duration_and_existing_outcome(
    capfire: CaptureLogfire,
) -> None:
    """完了時は duration と既存 outcome カウンターを1回ずつ残す。"""

    recorder = LogfireQuestionContextRecorder()
    call = await recorder.start()
    await recorder.end(
        call,
        outcome="prepared",
        prompt_version=_PROMPT_VERSION,
        ai_model=_AI_MODEL,
    )

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "completed",
        "outcome": "prepared",
    }
    duration = next(item for item in metrics if item["name"] == _DURATION_METRIC)
    assert duration["data"]["data_points"][0]["count"] == 1
    assert duration["data"]["data_points"][0]["sum"] >= 0
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "result": "prepared",
        "prompt_version": _PROMPT_VERSION,
        "ai_model": _AI_MODEL,
    }
    dumped = json.dumps(metrics, default=str)
    assert _SENTINEL not in dumped
    assert "run_id" not in dumped


async def test_failed_records_existing_outcome_labels(
    capfire: CaptureLogfire,
) -> None:
    """分類済み失敗は既存カウンターへ failure_code を載せる。"""

    recorder = LogfireQuestionContextRecorder()
    call = await recorder.start()
    await recorder.end(
        call,
        outcome="failed",
        prompt_version=_PROMPT_VERSION,
        ai_model=_AI_MODEL,
        failure_code="generator_unavailable",
    )

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "failed",
        "outcome": "failed",
    }
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "result": "failed",
        "prompt_version": _PROMPT_VERSION,
        "ai_model": _AI_MODEL,
        "failure_code": "generator_unavailable",
    }


async def test_stopped_does_not_emit_existing_outcome_counter(
    capfire: CaptureLogfire,
) -> None:
    """途中停止では既存カウンターを打たない。"""

    recorder = LogfireQuestionContextRecorder()
    call = await recorder.start()
    await recorder.end(call, stopped=True)

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "stopped",
        "outcome": "none",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)


async def test_end_without_outcome_records_failed_status(
    capfire: CaptureLogfire,
) -> None:
    """未分類の終わりは status=failed とし、既存カウンターは打たない。"""

    recorder = LogfireQuestionContextRecorder()
    call = await recorder.start()
    await recorder.end(call)

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "failed",
        "outcome": "none",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)


async def test_missing_process_labels_skip_existing_outcome_counter(
    capfire: CaptureLogfire,
) -> None:
    """prompt_version / ai_model が無いときは既存カウンターを打たない。"""

    recorder = LogfireQuestionContextRecorder()
    call = await recorder.start()
    await recorder.end(call, outcome="prepared")

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "completed",
        "outcome": "prepared",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)


async def test_start_returns_phase_call_when_clock_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start は失敗しても PhaseCall を返す。"""

    def _boom() -> float:
        raise RuntimeError("clock failed")

    monkeypatch.setattr("app.agent.recording.question_context.perf_counter", _boom)
    call = await LogfireQuestionContextRecorder().start()
    assert isinstance(call, PhaseCall)


async def test_end_does_not_propagate_metric_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metric 記録の失敗を呼び出し元へ出さない。"""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("metric failed")

    monkeypatch.setattr(
        "app.agent.recording.question_context._duration_histogram.record",
        _boom,
    )
    recorder = LogfireQuestionContextRecorder()
    call = await recorder.start()
    await recorder.end(
        call,
        outcome="prepared",
        prompt_version=_PROMPT_VERSION,
        ai_model=_AI_MODEL,
    )
