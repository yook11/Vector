"""A3 観測死活 (CloudWatch EMF ``observation_up`` 二重 sink) の emit 契約テスト。

``observe_pipeline_queue_health`` は成功した stage で ``observation_up=1``、
``StreamHealthError`` を送出した stage で ``observation_up=0`` の EMF 1 行を
stage ごとに出す (specs/observability/cloudwatch-alerting.md A3)。Logfire gauge
記録内容自体の正本は ``test_queue_health_task.py``。本ファイルは EMF emit の
stage ごとの成否分岐と構造だけを検証する。
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.queue.stream_health import (
    StreamHealthError,
    StreamHealthFailureReason,
    StreamHealthSnapshot,
    StreamHealthStage,
    StreamHealthTarget,
)
from app.queue.tasks import queue_health as module

_OBSERVATION_UP_METRIC = "observation_up"
_STAGE_SPECS = (
    ("acquisition", "pipeline:acquisition"),
    ("completion", "pipeline:completion"),
    ("curation", "pipeline:curation"),
    ("assessment", "pipeline:assessment"),
)
_STAGES = tuple(stage for stage, _ in _STAGE_SPECS)
_FOUR_TARGETS = tuple(
    StreamHealthTarget(
        stage=cast(StreamHealthStage, stage),
        stream=stream,
        group="taskiq",
    )
    for stage, stream in _STAGE_SPECS
)
_FAILURE_REASON_BY_STAGE = {
    "acquisition": "stream_missing",
    "completion": "group_missing",
    "curation": "lag_unknown",
    "assessment": "redis_unavailable",
}


def _emf_lines(captured_stdout: str) -> list[dict[str, Any]]:
    """stdout から EMF 行 (``_aws`` キーを持つ JSON 行) だけを抽出する。"""
    lines: list[dict[str, Any]] = []
    for line in captured_stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and "_aws" in record:
            lines.append(record)
    return lines


def _successful_observation_snapshot(
    target: StreamHealthTarget, timestamp: float
) -> StreamHealthSnapshot:
    """観測成功を表す、observation_up emit 用の最小 snapshot。"""
    return StreamHealthSnapshot(
        stage=target.stage,
        stream=target.stream,
        group=target.group,
        observation_timestamp=timestamp,
        retained_entries=0,
        lag=0,
        pending=0,
        oldest_undelivered_enqueue_age=None,
        oldest_pending_enqueue_age=None,
        oldest_outstanding_enqueue_age=None,
    )


def _stream_health_error(target: StreamHealthTarget) -> StreamHealthError:
    return StreamHealthError(
        stage=target.stage,
        reason=cast(StreamHealthFailureReason, _FAILURE_REASON_BY_STAGE[target.stage]),
    )


def _patch_targets_and_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "get_redis", MagicMock(return_value=object()))
    monkeypatch.setattr(module, "PIPELINE_QUEUE_TARGETS", _FOUR_TARGETS)


@pytest.mark.asyncio
async def test_all_stages_success_emit_observation_up_one_per_stage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """全4 stage成功時、stageごとにobservation_up=1のEMF行が1行、計4行出る。"""
    _patch_targets_and_redis(monkeypatch)
    snapshots = [
        _successful_observation_snapshot(target, 1_000.0 + index)
        for index, target in enumerate(_FOUR_TARGETS)
    ]
    monkeypatch.setattr(module, "read_stream_health", AsyncMock(side_effect=snapshots))

    await module.observe_pipeline_queue_health()
    emf_lines = _emf_lines(capsys.readouterr().out)

    assert [line["stage"] for line in emf_lines] == list(_STAGES)
    assert [line[_OBSERVATION_UP_METRIC] for line in emf_lines] == [1, 1, 1, 1]
    metric_def = emf_lines[0]["_aws"]["CloudWatchMetrics"][0]
    assert metric_def["Namespace"] == "Vector/Pipeline"
    assert metric_def["Dimensions"] == [["stage"]]
    assert metric_def["Metrics"] == [{"Name": _OBSERVATION_UP_METRIC, "Unit": "Count"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_stage", _STAGES)
async def test_one_stage_failure_emits_zero_and_continues_other_stages(
    failing_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """特定stageのStreamHealthErrorはそのstageだけobservation_up=0にし、他stageの観測を止めない。"""
    _patch_targets_and_redis(monkeypatch)
    results: list[StreamHealthSnapshot | StreamHealthError] = [
        _stream_health_error(target)
        if target.stage == failing_stage
        else _successful_observation_snapshot(target, 1_000.0)
        for target in _FOUR_TARGETS
    ]
    monkeypatch.setattr(module, "read_stream_health", AsyncMock(side_effect=results))

    await module.observe_pipeline_queue_health()
    emf_lines = _emf_lines(capsys.readouterr().out)
    observation_up_by_stage = {
        line["stage"]: line[_OBSERVATION_UP_METRIC] for line in emf_lines
    }

    assert observation_up_by_stage == {
        stage: 0 if stage == failing_stage else 1 for stage in _STAGES
    }


@pytest.mark.asyncio
async def test_all_stages_failure_emit_four_zero_lines(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """全stage失敗時、4行すべてobservation_up=0で出る。"""
    _patch_targets_and_redis(monkeypatch)
    errors = [_stream_health_error(target) for target in _FOUR_TARGETS]
    monkeypatch.setattr(module, "read_stream_health", AsyncMock(side_effect=errors))

    await module.observe_pipeline_queue_health()
    emf_lines = _emf_lines(capsys.readouterr().out)

    assert [line["stage"] for line in emf_lines] == list(_STAGES)
    assert [line[_OBSERVATION_UP_METRIC] for line in emf_lines] == [0, 0, 0, 0]
