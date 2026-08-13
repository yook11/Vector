"""assessment / embedding hold metric の Logfire oracle。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from logfire.testing import CaptureLogfire
from redis.exceptions import ConnectionError as RedisConnectionError

from app.audit.domain.event import Stage
from app.queue.helpers.stage_hold import HoldableStage, set_stage_hold


def _find_metric(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """metric dump から指定 name の metric を取り出す。"""
    return next((m for m in metrics if m["name"] == name), None)


def _sum_value(metric: dict[str, Any]) -> int:
    """sum 系 metric の合計値を取り出す。"""
    return sum(int(dp["value"]) for dp in metric["data"]["data_points"])


def _attributes_for(metric: dict[str, Any]) -> list[dict[str, Any]]:
    """metric の全 data_points の attribute dict を集める。"""
    return [dp.get("attributes", {}) for dp in metric["data"]["data_points"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "metric_name"),
    [
        (Stage.ASSESSMENT, "vector.assessment.hold_set"),
        (Stage.EMBEDDING, "vector.embedding.hold_set"),
    ],
)
async def test_set_hold_increments_success_counter_with_reason_only(
    capfire: CaptureLogfire,
    stage: HoldableStage,
    metric_name: str,
) -> None:
    """Redis SET 成功時、stage 別 success counter に reason だけ載る。"""
    fake_redis = AsyncMock()

    await set_stage_hold(fake_redis, stage, reason="ai_error_configuration")

    metrics = capfire.get_collected_metrics()
    hold_set = _find_metric(metrics, metric_name)
    assert hold_set is not None
    assert _sum_value(hold_set) == 1
    assert _attributes_for(hold_set) == [{"reason": "ai_error_configuration"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "metric_name"),
    [
        (Stage.ASSESSMENT, "vector.assessment.hold_set_failed"),
        (Stage.EMBEDDING, "vector.embedding.hold_set_failed"),
    ],
)
async def test_set_hold_failure_increments_failed_counter_with_reason_only(
    capfire: CaptureLogfire,
    stage: HoldableStage,
    metric_name: str,
) -> None:
    """Redis SET 失敗時、stage 別 failed counter に reason だけ載る。"""
    fake_redis = AsyncMock()
    fake_redis.set.side_effect = RedisConnectionError("connection refused")

    await set_stage_hold(fake_redis, stage, reason="ai_error_insufficient_balance")

    metrics = capfire.get_collected_metrics()
    hold_failed = _find_metric(metrics, metric_name)
    assert hold_failed is not None
    assert _sum_value(hold_failed) == 1
    assert _attributes_for(hold_failed) == [{"reason": "ai_error_insufficient_balance"}]
