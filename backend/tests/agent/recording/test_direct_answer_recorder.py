"""LogfireDirectAnswerRecorder の契約。"""

from __future__ import annotations

import pytest
from logfire.testing import CaptureLogfire

from app.agent.recording.direct_answer import LogfireDirectAnswerRecorder
from app.agent.recording.types import PhaseCall
from tests.logfire._metric_helpers import attributes_of, collected_metrics

_OUTCOME_METRIC = "vector.agent.direct_answer.outcome"
_DURATION_METRIC = "vector.agent.direct_answer.duration"


async def test_logfire_recorder_emits_duration_and_existing_outcome(
    capfire: CaptureLogfire,
) -> None:
    """完了時は duration と既存 outcome カウンターを1回ずつ残す。"""

    recorder = LogfireDirectAnswerRecorder()
    call = await recorder.start()
    await recorder.end(
        call,
        outcome="answered",
        retry_used=True,
    )

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "completed",
        "outcome": "answered",
    }
    duration = next(item for item in metrics if item["name"] == _DURATION_METRIC)
    assert duration["data"]["data_points"][0]["count"] == 1
    assert duration["data"]["data_points"][0]["sum"] >= 0
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "result": "answered",
        "retry_used": True,
        "failure_code": "none",
    }


async def test_failed_records_existing_outcome_labels(
    capfire: CaptureLogfire,
) -> None:
    """分類済み失敗は既存カウンターへ failure_code を載せる。"""

    recorder = LogfireDirectAnswerRecorder()
    call = await recorder.start()
    await recorder.end(
        call,
        outcome="failed",
        retry_used=False,
        failure_code="ai_error_network",
    )

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "failed",
        "outcome": "failed",
    }
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "result": "failed",
        "retry_used": False,
        "failure_code": "ai_error_network",
    }


async def test_stopped_does_not_emit_existing_outcome_counter(
    capfire: CaptureLogfire,
) -> None:
    """途中停止では既存カウンターを打たない。"""

    recorder = LogfireDirectAnswerRecorder()
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

    recorder = LogfireDirectAnswerRecorder()
    call = await recorder.start()
    await recorder.end(call)

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "failed",
        "outcome": "none",
    }
    assert all(item["name"] != _OUTCOME_METRIC for item in metrics)


async def test_start_returns_phase_call_when_clock_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start は失敗しても PhaseCall を返す。"""

    def _boom() -> float:
        raise RuntimeError("clock failed")

    monkeypatch.setattr("app.agent.recording.direct_answer.perf_counter", _boom)
    call = await LogfireDirectAnswerRecorder().start()
    assert isinstance(call, PhaseCall)


async def test_end_does_not_propagate_metric_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metric 記録の失敗を呼び出し元へ出さない。"""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("metric failed")

    monkeypatch.setattr(
        "app.agent.recording.direct_answer._duration_histogram.record",
        _boom,
    )
    recorder = LogfireDirectAnswerRecorder()
    call = await recorder.start()
    await recorder.end(call, outcome="answered")
