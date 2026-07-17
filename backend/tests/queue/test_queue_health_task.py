"""毎分pipeline queue health samplerのtask/metric契約。"""

from __future__ import annotations

import importlib
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

from app.queue.brokers import broker_maintenance
from app.queue.stream_health import (
    PIPELINE_QUEUE_TARGETS,
    StreamHealthError,
    StreamHealthSnapshot,
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
        _empty_snapshot(PIPELINE_QUEUE_TARGETS[0], 1_000.25),
        _empty_snapshot(PIPELINE_QUEUE_TARGETS[1], 2_000.5),
    ]
    read_health = AsyncMock(side_effect=snapshots)
    monkeypatch.setattr(module, "get_redis", get_redis)
    monkeypatch.setattr(module, "read_stream_health", read_health)

    await module.observe_pipeline_queue_health()
    values, attribute_keys = _queue_metric_values(capfire)

    assert (
        get_redis.call_count,
        read_health.await_args_list,
        values,
        attribute_keys,
    ) == (
        1,
        [
            call(redis, PIPELINE_QUEUE_TARGETS[0]),
            call(redis, PIPELINE_QUEUE_TARGETS[1]),
        ],
        {
            "vector.pipeline.queue.retained_entries": {
                "curation": 0,
                "assessment": 0,
            },
            "vector.pipeline.queue.lag": {"curation": 0, "assessment": 0},
            "vector.pipeline.queue.pending": {"curation": 0, "assessment": 0},
            "vector.pipeline.queue.oldest_undelivered_enqueue_age": {
                "curation": 0,
                "assessment": 0,
            },
            "vector.pipeline.queue.oldest_pending_enqueue_age": {
                "curation": 0,
                "assessment": 0,
            },
            "vector.pipeline.queue.oldest_outstanding_enqueue_age": {
                "curation": 0,
                "assessment": 0,
            },
            "vector.pipeline.queue.observation_up": {
                "curation": 1,
                "assessment": 1,
            },
            "vector.pipeline.queue.observation_timestamp": {
                "curation": 1_000.25,
                "assessment": 2_000.5,
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
            PIPELINE_QUEUE_TARGETS[0],
            timestamp=3_000.75,
            retained=11,
            lag=7,
            pending=3,
            undelivered_age=12.5,
            pending_age=35.25,
            outstanding_age=35.25,
        ),
        _snapshot(
            PIPELINE_QUEUE_TARGETS[1],
            timestamp=4_000.125,
            retained=5,
            lag=0,
            pending=2,
            undelivered_age=None,
            pending_age=9.5,
            outstanding_age=9.5,
        ),
    ]
    monkeypatch.setattr(module, "get_redis", MagicMock(return_value=redis))
    monkeypatch.setattr(
        module,
        "read_stream_health",
        AsyncMock(side_effect=snapshots),
    )

    await module.observe_pipeline_queue_health()
    values, attribute_keys = _queue_metric_values(capfire)

    assert (values, attribute_keys) == (
        {
            "vector.pipeline.queue.retained_entries": {
                "curation": 11,
                "assessment": 5,
            },
            "vector.pipeline.queue.lag": {"curation": 7, "assessment": 0},
            "vector.pipeline.queue.pending": {"curation": 3, "assessment": 2},
            "vector.pipeline.queue.oldest_undelivered_enqueue_age": {
                "curation": 12.5,
                "assessment": 0,
            },
            "vector.pipeline.queue.oldest_pending_enqueue_age": {
                "curation": 35.25,
                "assessment": 9.5,
            },
            "vector.pipeline.queue.oldest_outstanding_enqueue_age": {
                "curation": 35.25,
                "assessment": 9.5,
            },
            "vector.pipeline.queue.observation_up": {
                "curation": 1,
                "assessment": 1,
            },
            "vector.pipeline.queue.observation_timestamp": {
                "curation": 3_000.75,
                "assessment": 4_000.125,
            },
        },
        _all_stage_only_attributes(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_stage", ["curation", "assessment"])
async def test_one_stage_failure_records_only_up_zero_and_continues_other_stage(
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
    failing_stage: str,
) -> None:
    module = _queue_health_module()
    redis = object()
    successful_target = next(
        target for target in PIPELINE_QUEUE_TARGETS if target.stage != failing_stage
    )
    successful_snapshot = _snapshot(
        successful_target,
        timestamp=5_000.5,
        retained=8,
        lag=4,
        pending=2,
        undelivered_age=20.0,
        pending_age=30.0,
        outstanding_age=30.0,
    )
    results: list[StreamHealthSnapshot | StreamHealthError] = [
        StreamHealthError(stage="curation", reason="stream_missing")
        if failing_stage == "curation"
        else successful_snapshot,
        StreamHealthError(stage="assessment", reason="group_missing")
        if failing_stage == "assessment"
        else successful_snapshot,
    ]
    read_health = AsyncMock(side_effect=results)
    monkeypatch.setattr(module, "get_redis", MagicMock(return_value=redis))
    monkeypatch.setattr(module, "read_stream_health", read_health)

    with capture_logs() as logs:
        await module.observe_pipeline_queue_health()
    values, attribute_keys = _queue_metric_values(capfire)
    failure_logs = [
        log
        for log in logs
        if log.get("event") == "pipeline_queue_health_observation_failed"
    ]
    successful_stage = successful_target.stage
    expected_reason = (
        "stream_missing" if failing_stage == "curation" else "group_missing"
    )

    assert (
        read_health.await_args_list,
        values,
        attribute_keys,
        failure_logs,
    ) == (
        [
            call(redis, PIPELINE_QUEUE_TARGETS[0]),
            call(redis, PIPELINE_QUEUE_TARGETS[1]),
        ],
        {
            "vector.pipeline.queue.retained_entries": {successful_stage: 8},
            "vector.pipeline.queue.lag": {successful_stage: 4},
            "vector.pipeline.queue.pending": {successful_stage: 2},
            "vector.pipeline.queue.oldest_undelivered_enqueue_age": {
                successful_stage: 20.0
            },
            "vector.pipeline.queue.oldest_pending_enqueue_age": {
                successful_stage: 30.0
            },
            "vector.pipeline.queue.oldest_outstanding_enqueue_age": {
                successful_stage: 30.0
            },
            "vector.pipeline.queue.observation_up": {
                failing_stage: 0,
                successful_stage: 1,
            },
            "vector.pipeline.queue.observation_timestamp": {successful_stage: 5_000.5},
        },
        {
            **{
                name: {frozenset({"stage"})}
                for name in _METRIC_NAMES
                if name != "vector.pipeline.queue.observation_timestamp"
            },
            "vector.pipeline.queue.observation_timestamp": {frozenset({"stage"})},
        },
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
            PIPELINE_QUEUE_TARGETS[0],
            timestamp=6_000.25,
            retained=13,
            lag=5,
            pending=3,
            undelivered_age=40.0,
            pending_age=50.0,
            outstanding_age=50.0,
        ),
        _snapshot(
            PIPELINE_QUEUE_TARGETS[1],
            timestamp=7_000.5,
            retained=17,
            lag=6,
            pending=4,
            undelivered_age=60.0,
            pending_age=70.0,
            outstanding_age=70.0,
        ),
    ]
    read_health = AsyncMock(
        side_effect=[
            *successful,
            StreamHealthError(stage="curation", reason="redis_unavailable"),
            StreamHealthError(stage="assessment", reason="inconsistent_snapshot"),
        ]
    )
    monkeypatch.setattr(module, "get_redis", MagicMock(return_value=redis))
    monkeypatch.setattr(module, "read_stream_health", read_health)

    await module.observe_pipeline_queue_health()
    await module.observe_pipeline_queue_health()
    values, attribute_keys = _queue_metric_values(capfire)

    assert (values, attribute_keys) == (
        {
            "vector.pipeline.queue.retained_entries": {
                "curation": 13,
                "assessment": 17,
            },
            "vector.pipeline.queue.lag": {"curation": 5, "assessment": 6},
            "vector.pipeline.queue.pending": {"curation": 3, "assessment": 4},
            "vector.pipeline.queue.oldest_undelivered_enqueue_age": {
                "curation": 40.0,
                "assessment": 60.0,
            },
            "vector.pipeline.queue.oldest_pending_enqueue_age": {
                "curation": 50.0,
                "assessment": 70.0,
            },
            "vector.pipeline.queue.oldest_outstanding_enqueue_age": {
                "curation": 50.0,
                "assessment": 70.0,
            },
            "vector.pipeline.queue.observation_up": {
                "curation": 0,
                "assessment": 0,
            },
            "vector.pipeline.queue.observation_timestamp": {
                "curation": 6_000.25,
                "assessment": 7_000.5,
            },
        },
        _all_stage_only_attributes(),
    )
