"""LogfireExternalSearchRecorder の契約。"""

from __future__ import annotations

import json

import pytest
from logfire.testing import CaptureLogfire

from app.agent.recording.external_search import LogfireExternalSearchRecorder
from app.agent.recording.types import PhaseCall
from tests.logfire._metric_helpers import attributes_of, collected_metrics

_OUTCOME_METRIC = "vector.agent.external_search.outcome"
_DURATION_METRIC = "vector.agent.external_search.duration"
_SENTINEL = "TASK_CONTENTS_SENTINEL_external_search"


async def test_logfire_recorder_emits_outcome_and_duration(
    capfire: CaptureLogfire,
) -> None:
    """1 search の終わり方と所要時間だけを残す。"""

    recorder = LogfireExternalSearchRecorder()
    call = await recorder.start()
    await recorder.end(
        call,
        outcome="succeeded",
    )

    metrics = collected_metrics(capfire)
    expected = {"status": "completed", "outcome": "succeeded"}
    assert attributes_of(metrics, _OUTCOME_METRIC) == expected
    duration = next(item for item in metrics if item["name"] == _DURATION_METRIC)
    point = duration["data"]["data_points"][0]
    assert point["attributes"] == expected
    assert point["count"] == 1
    assert point["sum"] >= 0
    dumped = json.dumps(metrics, default=str)
    assert _SENTINEL not in dumped
    assert "run_id" not in dumped


async def test_stopped_records_outcome_none(capfire: CaptureLogfire) -> None:
    """途中停止では outcome ラベルを none にする。"""

    recorder = LogfireExternalSearchRecorder()
    call = await recorder.start()
    await recorder.end(call, stopped=True)

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _OUTCOME_METRIC) == {
        "status": "stopped",
        "outcome": "none",
    }


async def test_query_generation_failed_records_failed_status(
    capfire: CaptureLogfire,
) -> None:
    """query を作れなかった工程は failed として残す。"""

    recorder = LogfireExternalSearchRecorder()
    call = await recorder.start()
    await recorder.end(
        call,
        outcome="query_generation_failed",
    )

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC) == {
        "status": "failed",
        "outcome": "query_generation_failed",
    }


async def test_end_without_outcome_records_failed_status(
    capfire: CaptureLogfire,
) -> None:
    """未分類の終わりは status=failed、outcome=none にする。"""

    recorder = LogfireExternalSearchRecorder()
    call = await recorder.start()
    await recorder.end(call)

    assert attributes_of(collected_metrics(capfire), _OUTCOME_METRIC) == {
        "status": "failed",
        "outcome": "none",
    }


async def test_start_returns_phase_call_when_clock_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start は失敗しても PhaseCall を返す。"""

    def _boom() -> float:
        raise RuntimeError("clock failed")

    monkeypatch.setattr("app.agent.recording.external_search.perf_counter", _boom)
    call = await LogfireExternalSearchRecorder().start()
    assert isinstance(call, PhaseCall)


async def test_end_does_not_propagate_metric_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metric 記録の失敗を呼び出し元へ出さない。"""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("metric failed")

    monkeypatch.setattr(
        "app.agent.recording.external_search._outcome_counter.add",
        _boom,
    )
    recorder = LogfireExternalSearchRecorder()
    call = await recorder.start()
    await recorder.end(call, outcome="succeeded")
