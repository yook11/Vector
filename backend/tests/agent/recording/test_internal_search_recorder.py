"""LogfireInternalSearchRecorder の契約。"""

from __future__ import annotations

import json

import pytest
from logfire.testing import CaptureLogfire

from app.agent.recording.internal_search import LogfireInternalSearchRecorder
from app.agent.recording.types import PhaseCall
from tests.logfire._metric_helpers import attributes_of, collected_metrics

_OUTCOME_METRIC = "vector.agent.internal_retrieval.outcome"
_DURATION_METRIC = "vector.agent.internal_search.duration"
_SENTINEL = "TASK_CONTENTS_SENTINEL_internal_search"


async def test_logfire_recorder_emits_duration_and_existing_outcome(
    capfire: CaptureLogfire,
) -> None:
    """完了時は duration と既存 outcome カウンターを1回ずつ残す。"""

    recorder = LogfireInternalSearchRecorder()
    call = await recorder.start()
    await recorder.end(
        call,
        outcome="succeeded",
        query_count=2,
    )

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _DURATION_METRIC) == {
        "status": "completed",
        "outcome": "succeeded",
    }
    duration = next(item for item in metrics if item["name"] == _DURATION_METRIC)
    assert duration["data"]["data_points"][0]["count"] == 1
    assert duration["data"]["data_points"][0]["sum"] >= 0
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "result": "succeeded",
        "query_count": 2,
    }
    dumped = json.dumps(metrics, default=str)
    assert _SENTINEL not in dumped
    assert "run_id" not in dumped


async def test_failed_known_error_records_failure_phase(
    capfire: CaptureLogfire,
) -> None:
    """既知の内部失敗は既存カウンターへ failure_phase を載せる。"""

    recorder = LogfireInternalSearchRecorder()
    call = await recorder.start()
    await recorder.end(
        call,
        outcome="failed",
        query_count=1,
        failure_phase="query_embedding",
    )

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "result": "failed",
        "query_count": 1,
        "failure_phase": "query_embedding",
    }


async def test_stopped_does_not_emit_existing_outcome_counter(
    capfire: CaptureLogfire,
) -> None:
    """outcome なしでは既存カウンターを打たない。"""

    recorder = LogfireInternalSearchRecorder()
    call = await recorder.start()
    await recorder.end(
        call,
        query_count=1,
        stopped=True,
    )

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

    recorder = LogfireInternalSearchRecorder()
    call = await recorder.start()
    await recorder.end(call, query_count=1)

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

    monkeypatch.setattr("app.agent.recording.internal_search.perf_counter", _boom)
    call = await LogfireInternalSearchRecorder().start()
    assert isinstance(call, PhaseCall)


async def test_end_does_not_propagate_metric_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metric 記録の失敗を呼び出し元へ出さない。"""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("metric failed")

    monkeypatch.setattr(
        "app.agent.recording.internal_search._duration_histogram.record",
        _boom,
    )
    recorder = LogfireInternalSearchRecorder()
    call = await recorder.start()
    await recorder.end(
        call,
        outcome="succeeded",
        query_count=1,
    )
