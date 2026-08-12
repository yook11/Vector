"""A2 工程別滞留 (EMF ``oldest_outstanding_enqueue_age`` 二重 sink) の emit 契約テスト。

``observe_pipeline_queue_health`` は成功した stage で
``oldest_outstanding_enqueue_age{stage}`` の EMF 1 行を出す
(specs/observability/cloudwatch-alerting.md A2)。snapshot の age が None のときは
0.0 を emit する(仕事ゼロ = age 0 の契約。A2 alarm の `TreatMissingData=notBreaching`
はこの emit を前提にする)。``StreamHealthError`` を送出した stage では age 行を
出さない(``observation_up=0`` のみ、A3 の担当)。

Logfire gauge 記録内容自体の正本は ``test_queue_health_task.py``、
``PIPELINE_QUEUE_TARGETS`` への embedding stage 追加自体の正本は
``test_stream_health.py``。``emit_metric`` の EMF 構造(Namespace/Unit/float 値)の
正本は ``test_emf.py``。本ファイルは age emit の stage ごとの成否分岐だけを検証する。
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

_AGE_METRIC = "oldest_outstanding_enqueue_age"
# embedding は本タスクで PIPELINE_QUEUE_TARGETS に追加された新規観測対象。
_STAGE_SPECS = (
    ("acquisition", "pipeline:acquisition"),
    ("completion", "pipeline:completion"),
    ("embedding", "pipeline:embedding"),
)
_STAGES = tuple(stage for stage, _ in _STAGE_SPECS)
_TARGETS = tuple(
    StreamHealthTarget(
        stage=cast(StreamHealthStage, stage),
        stream=stream,
        group="taskiq",
    )
    for stage, stream in _STAGE_SPECS
)


def _metric_records(captured_stdout: str, metric_name: str) -> list[dict[str, Any]]:
    """stdout から metric_name を持つ EMF レコード (``_aws`` キー行) を抽出する。"""
    records: list[dict[str, Any]] = []
    for line in captured_stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and "_aws" in record and metric_name in record:
            records.append(record)
    return records


def _snapshot_with_age(
    target: StreamHealthTarget,
    *,
    timestamp: float,
    outstanding_age: float | None,
) -> StreamHealthSnapshot:
    """age 分岐の検証に必要な最小 snapshot (他の count 系は 0 固定)。"""
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
        oldest_outstanding_enqueue_age=outstanding_age,
    )


def _patch_targets_and_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "get_redis", MagicMock(return_value=object()))
    monkeypatch.setattr(module, "PIPELINE_QUEUE_TARGETS", _TARGETS)


@pytest.mark.asyncio
async def test_success_stages_emit_age_with_snapshot_value_or_zero_for_none(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """成功 stage は snapshot の age を emit し、None の stage は 0.0 になる。"""
    _patch_targets_and_redis(monkeypatch)
    snapshots = [
        _snapshot_with_age(_TARGETS[0], timestamp=1_000.0, outstanding_age=42.5),
        _snapshot_with_age(_TARGETS[1], timestamp=2_000.0, outstanding_age=None),
        _snapshot_with_age(_TARGETS[2], timestamp=3_000.0, outstanding_age=7.25),
    ]
    monkeypatch.setattr(module, "read_stream_health", AsyncMock(side_effect=snapshots))

    await module.observe_pipeline_queue_health()
    age_records = _metric_records(capsys.readouterr().out, _AGE_METRIC)

    assert {record["stage"]: record[_AGE_METRIC] for record in age_records} == {
        "acquisition": 42.5,
        "completion": 0.0,
        "embedding": 7.25,
    }


@pytest.mark.asyncio
async def test_success_age_line_uses_seconds_unit_and_pipeline_namespace(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """age EMF 行の Namespace/Dimensions/Unit を固定する (値の型は test_emf.py 側)。"""
    _patch_targets_and_redis(monkeypatch)
    snapshots = [
        _snapshot_with_age(target, timestamp=1_000.0, outstanding_age=5.0)
        for target in _TARGETS
    ]
    monkeypatch.setattr(module, "read_stream_health", AsyncMock(side_effect=snapshots))

    await module.observe_pipeline_queue_health()
    age_records = _metric_records(capsys.readouterr().out, _AGE_METRIC)
    metric_def = age_records[0]["_aws"]["CloudWatchMetrics"][0]

    assert len(age_records) == len(_TARGETS)
    assert metric_def["Namespace"] == "Vector/Pipeline"
    assert metric_def["Dimensions"] == [["stage"]]
    assert metric_def["Metrics"] == [{"Name": _AGE_METRIC, "Unit": "Seconds"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_stage", _STAGES)
async def test_failing_stage_emits_no_age_line_but_other_stages_still_emit(
    failing_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """StreamHealthError の stage は age 行を出さず、他 stage の emit は止めない。"""
    _patch_targets_and_redis(monkeypatch)
    results: list[StreamHealthSnapshot | StreamHealthError] = [
        StreamHealthError(
            stage=target.stage,
            reason=cast(StreamHealthFailureReason, "redis_unavailable"),
        )
        if target.stage == failing_stage
        else _snapshot_with_age(target, timestamp=1_000.0, outstanding_age=9.0)
        for target in _TARGETS
    ]
    monkeypatch.setattr(module, "read_stream_health", AsyncMock(side_effect=results))

    await module.observe_pipeline_queue_health()
    age_stages = {
        record["stage"]
        for record in _metric_records(capsys.readouterr().out, _AGE_METRIC)
    }

    assert age_stages == set(_STAGES) - {failing_stage}
