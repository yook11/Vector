"""毎分pipeline queue health samplerのtask/metric契約。"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

from app.queue.brokers import broker_maintenance
from app.queue.stream_health import (
    StreamHealthError,
    StreamHealthFailureReason,
    StreamHealthSnapshot,
    StreamHealthStage,
    StreamHealthTarget,
)

_METRIC_NAMES = (
    "vector.pipeline.queue.retained_entries",
    "vector.pipeline.queue.lag",
    "vector.pipeline.queue.pending",
    "vector.pipeline.queue.oldest_undelivered_enqueue_age",
    "vector.pipeline.queue.oldest_pending_enqueue_age",
    "vector.pipeline.queue.oldest_outstanding_enqueue_age",
    "vector.pipeline.queue.observation_up",
    "vector.pipeline.queue.observation_timestamp",
)
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


def _queue_health_module() -> ModuleType:
    """未実装moduleをcollection errorではなく契約failureとして報告する。"""
    try:
        return importlib.import_module("app.queue.tasks.queue_health")
    except ModuleNotFoundError as exc:
        if exc.name == "app.queue.tasks.queue_health":
            pytest.fail("app.queue.tasks.queue_health is not implemented")
        raise


def _snapshot(
    target: StreamHealthTarget,
    *,
    timestamp: float,
    retained: int,
    lag: int,
    pending: int,
    undelivered_age: float | None,
    pending_age: float | None,
    outstanding_age: float | None,
) -> StreamHealthSnapshot:
    return StreamHealthSnapshot(
        stage=target.stage,
        stream=target.stream,
        group=target.group,
        observation_timestamp=timestamp,
        retained_entries=retained,
        lag=lag,
        pending=pending,
        oldest_undelivered_enqueue_age=undelivered_age,
        oldest_pending_enqueue_age=pending_age,
        oldest_outstanding_enqueue_age=outstanding_age,
    )


def _empty_snapshot(
    target: StreamHealthTarget,
    timestamp: float,
) -> StreamHealthSnapshot:
    return _snapshot(
        target,
        timestamp=timestamp,
        retained=0,
        lag=0,
        pending=0,
        undelivered_age=None,
        pending_age=None,
        outstanding_age=None,
    )


def _queue_metric_values(
    capfire: CaptureLogfire,
) -> tuple[dict[str, dict[str, int | float]], dict[str, set[frozenset[str]]]]:
    try:
        metrics = capfire.get_collected_metrics()
    except AttributeError:
        metrics = []
    values: dict[str, dict[str, int | float]] = {}
    attribute_keys: dict[str, set[frozenset[str]]] = {}
    for metric in metrics:
        name = metric["name"]
        if name not in _METRIC_NAMES:
            continue
        values[name] = {}
        attribute_keys[name] = set()
        for point in metric["data"]["data_points"]:
            attributes = point.get("attributes", {})
            stage = attributes.get("stage")
            if isinstance(stage, str):
                values[name][stage] = point["value"]
            attribute_keys[name].add(frozenset(attributes))
    return values, attribute_keys


def _all_stage_only_attributes() -> dict[str, set[frozenset[str]]]:
    return {name: {frozenset({"stage"})} for name in _METRIC_NAMES}


def test_queue_health_task_uses_dedicated_cron_and_maintenance_broker() -> None:
    module = _queue_health_module()
    schedule = importlib.import_module("app.queue.schedule")
    cron = getattr(schedule, "CRON_PIPELINE_QUEUE_HEALTH", None)
    task = module.observe_pipeline_queue_health

    assert (
        cron,
        task.broker,
        task.task_name,
        task.labels,
    ) == (
        "* * * * *",
        broker_maintenance,
        "observe_pipeline_queue_health",
        {
            "timeout": 15,
            "max_retries": 0,
            "retry_on_error": False,
            "schedule": [{"cron": "* * * * *"}],
        },
    )


@pytest.mark.asyncio
async def test_empty_snapshots_record_zero_ages_up_and_redis_timestamp(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _queue_health_module()
    redis = object()
    get_redis = MagicMock(return_value=redis)
    snapshots = [
        _empty_snapshot(target, timestamp)
        for target, timestamp in zip(
            _FOUR_TARGETS,
            (1_000.25, 2_000.5, 3_000.75, 4_000.125),
            strict=True,
        )
    ]
    read_health = AsyncMock(side_effect=snapshots)
    monkeypatch.setattr(module, "get_redis", get_redis)
    monkeypatch.setattr(module, "read_stream_health", read_health)
    monkeypatch.setattr(module, "PIPELINE_QUEUE_TARGETS", _FOUR_TARGETS)

    await module.observe_pipeline_queue_health()
    values, attribute_keys = _queue_metric_values(capfire)

    assert (
        get_redis.call_count,
        read_health.await_args_list,
        values,
        attribute_keys,
    ) == (
        1,
        [call(redis, target) for target in _FOUR_TARGETS],
        {
            "vector.pipeline.queue.retained_entries": dict.fromkeys(_STAGES, 0),
            "vector.pipeline.queue.lag": dict.fromkeys(_STAGES, 0),
            "vector.pipeline.queue.pending": dict.fromkeys(_STAGES, 0),
            "vector.pipeline.queue.oldest_undelivered_enqueue_age": dict.fromkeys(
                _STAGES, 0
            ),
            "vector.pipeline.queue.oldest_pending_enqueue_age": dict.fromkeys(
                _STAGES, 0
            ),
            "vector.pipeline.queue.oldest_outstanding_enqueue_age": dict.fromkeys(
                _STAGES, 0
            ),
            "vector.pipeline.queue.observation_up": dict.fromkeys(_STAGES, 1),
            "vector.pipeline.queue.observation_timestamp": {
                "acquisition": 1_000.25,
                "completion": 2_000.5,
                "curation": 3_000.75,
                "assessment": 4_000.125,
            },
        },
        _all_stage_only_attributes(),
    )


@pytest.mark.asyncio
async def test_nonempty_snapshots_record_exact_counts_and_enqueue_ages(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _queue_health_module()
    redis = object()
    snapshots = [
        _snapshot(
            _FOUR_TARGETS[0],
            timestamp=3_000.75,
            retained=11,
            lag=7,
            pending=3,
            undelivered_age=12.5,
            pending_age=35.25,
            outstanding_age=35.25,
        ),
        _snapshot(
            _FOUR_TARGETS[1],
            timestamp=4_000.125,
            retained=5,
            lag=0,
            pending=2,
            undelivered_age=None,
            pending_age=9.5,
            outstanding_age=9.5,
        ),
        _snapshot(
            _FOUR_TARGETS[2],
            timestamp=5_000.5,
            retained=17,
            lag=4,
            pending=0,
            undelivered_age=22.0,
            pending_age=None,
            outstanding_age=22.0,
        ),
        _snapshot(
            _FOUR_TARGETS[3],
            timestamp=6_000.25,
            retained=9,
            lag=1,
            pending=1,
            undelivered_age=3.0,
            pending_age=6.0,
            outstanding_age=6.0,
        ),
    ]
    monkeypatch.setattr(module, "get_redis", MagicMock(return_value=redis))
    monkeypatch.setattr(
        module,
        "read_stream_health",
        AsyncMock(side_effect=snapshots),
    )
    monkeypatch.setattr(module, "PIPELINE_QUEUE_TARGETS", _FOUR_TARGETS)

    await module.observe_pipeline_queue_health()
    values, attribute_keys = _queue_metric_values(capfire)

    assert (values, attribute_keys) == (
        {
            "vector.pipeline.queue.retained_entries": {
                "acquisition": 11,
                "completion": 5,
                "curation": 17,
                "assessment": 9,
            },
            "vector.pipeline.queue.lag": {
                "acquisition": 7,
                "completion": 0,
                "curation": 4,
                "assessment": 1,
            },
            "vector.pipeline.queue.pending": {
                "acquisition": 3,
                "completion": 2,
                "curation": 0,
                "assessment": 1,
            },
            "vector.pipeline.queue.oldest_undelivered_enqueue_age": {
                "acquisition": 12.5,
                "completion": 0,
                "curation": 22.0,
                "assessment": 3.0,
            },
            "vector.pipeline.queue.oldest_pending_enqueue_age": {
                "acquisition": 35.25,
                "completion": 9.5,
                "curation": 0,
                "assessment": 6.0,
            },
            "vector.pipeline.queue.oldest_outstanding_enqueue_age": {
                "acquisition": 35.25,
                "completion": 9.5,
                "curation": 22.0,
                "assessment": 6.0,
            },
            "vector.pipeline.queue.observation_up": {
                "acquisition": 1,
                "completion": 1,
                "curation": 1,
                "assessment": 1,
            },
            "vector.pipeline.queue.observation_timestamp": {
                "acquisition": 3_000.75,
                "completion": 4_000.125,
                "curation": 5_000.5,
                "assessment": 6_000.25,
            },
        },
        _all_stage_only_attributes(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_stage", _STAGES)
async def test_one_stage_failure_records_only_up_zero_and_continues_other_stage(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
    failing_stage: str,
) -> None:
    module = _queue_health_module()
    redis = object()
    results: list[StreamHealthSnapshot | StreamHealthError] = [
        StreamHealthError(
            stage=target.stage,
            reason=cast(
                StreamHealthFailureReason,
                _FAILURE_REASON_BY_STAGE[target.stage],
            ),
        )
        if target.stage == failing_stage
        else _snapshot(
            target,
            timestamp=5_000.5,
            retained=8,
            lag=4,
            pending=2,
            undelivered_age=20.0,
            pending_age=30.0,
            outstanding_age=30.0,
        )
        for target in _FOUR_TARGETS
    ]
    read_health = AsyncMock(side_effect=results)
    monkeypatch.setattr(module, "get_redis", MagicMock(return_value=redis))
    monkeypatch.setattr(module, "read_stream_health", read_health)
    monkeypatch.setattr(module, "PIPELINE_QUEUE_TARGETS", _FOUR_TARGETS)

    with capture_logs() as logs:
        await module.observe_pipeline_queue_health()
    values, attribute_keys = _queue_metric_values(capfire)
    failure_logs = [
        log
        for log in logs
        if log.get("event") == "pipeline_queue_health_observation_failed"
    ]
    successful_stages = tuple(stage for stage in _STAGES if stage != failing_stage)
    expected_reason = _FAILURE_REASON_BY_STAGE[failing_stage]

    assert (
        read_health.await_args_list,
        values,
        attribute_keys,
        failure_logs,
    ) == (
        [call(redis, target) for target in _FOUR_TARGETS],
        {
            "vector.pipeline.queue.retained_entries": dict.fromkeys(
                successful_stages, 8
            ),
            "vector.pipeline.queue.lag": dict.fromkeys(successful_stages, 4),
            "vector.pipeline.queue.pending": dict.fromkeys(successful_stages, 2),
            "vector.pipeline.queue.oldest_undelivered_enqueue_age": dict.fromkeys(
                successful_stages, 20.0
            ),
            "vector.pipeline.queue.oldest_pending_enqueue_age": dict.fromkeys(
                successful_stages, 30.0
            ),
            "vector.pipeline.queue.oldest_outstanding_enqueue_age": dict.fromkeys(
                successful_stages, 30.0
            ),
            "vector.pipeline.queue.observation_up": {
                stage: 0 if stage == failing_stage else 1 for stage in _STAGES
            },
            "vector.pipeline.queue.observation_timestamp": dict.fromkeys(
                successful_stages, 5_000.5
            ),
        },
        _all_stage_only_attributes(),
        [
            {
                "event": "pipeline_queue_health_observation_failed",
                "stage": failing_stage,
                "reason": expected_reason,
                "log_level": "warning",
            }
        ],
    )


@pytest.mark.asyncio
async def test_failure_tick_does_not_overwrite_last_successful_data_or_timestamp(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _queue_health_module()
    redis = object()
    successful = [
        _snapshot(
            target,
            timestamp=6_000.25 + index,
            retained=13 + index,
            lag=5 + index,
            pending=3 + index,
            undelivered_age=40.0 + 10 * index,
            pending_age=50.0 + 10 * index,
            outstanding_age=50.0 + 10 * index,
        )
        for index, target in enumerate(_FOUR_TARGETS)
    ]
    read_health = AsyncMock(
        side_effect=[
            *successful,
            *[
                StreamHealthError(
                    stage=target.stage,
                    reason=cast(
                        StreamHealthFailureReason,
                        _FAILURE_REASON_BY_STAGE[target.stage],
                    ),
                )
                for target in _FOUR_TARGETS
            ],
        ]
    )
    monkeypatch.setattr(module, "get_redis", MagicMock(return_value=redis))
    monkeypatch.setattr(module, "read_stream_health", read_health)
    monkeypatch.setattr(module, "PIPELINE_QUEUE_TARGETS", _FOUR_TARGETS)

    await module.observe_pipeline_queue_health()
    await module.observe_pipeline_queue_health()
    values, attribute_keys = _queue_metric_values(capfire)

    assert (values, attribute_keys) == (
        {
            "vector.pipeline.queue.retained_entries": {
                stage: 13 + index for index, stage in enumerate(_STAGES)
            },
            "vector.pipeline.queue.lag": {
                stage: 5 + index for index, stage in enumerate(_STAGES)
            },
            "vector.pipeline.queue.pending": {
                stage: 3 + index for index, stage in enumerate(_STAGES)
            },
            "vector.pipeline.queue.oldest_undelivered_enqueue_age": {
                stage: 40.0 + 10 * index for index, stage in enumerate(_STAGES)
            },
            "vector.pipeline.queue.oldest_pending_enqueue_age": {
                stage: 50.0 + 10 * index for index, stage in enumerate(_STAGES)
            },
            "vector.pipeline.queue.oldest_outstanding_enqueue_age": {
                stage: 50.0 + 10 * index for index, stage in enumerate(_STAGES)
            },
            "vector.pipeline.queue.observation_up": dict.fromkeys(_STAGES, 0),
            "vector.pipeline.queue.observation_timestamp": {
                stage: 6_000.25 + index for index, stage in enumerate(_STAGES)
            },
        },
        _all_stage_only_attributes(),
    )
