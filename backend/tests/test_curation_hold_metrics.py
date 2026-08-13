"""``app/queue/helpers/stage_hold.py`` の curation Logfire metric 記録 oracle。

検証する性質:
- ``set_stage_hold`` 成功時に ``vector.curation.hold_set`` counter が +1
  され、attribute は ``{"reason": <CODE 由来>}`` のみ。
- ``set_stage_hold`` の Redis SET 失敗時は ``vector.curation.hold_set_failed``
  counter が +1 され、成功 counter は increment されない。
- attribute 値域が provider error CODE の閉じた語彙に収まる構造的契約を
  ホワイトリスト (許可値域) oracle で検証する。

capfire fixture が ``logfire.configure(send_to_logfire=False, ...)`` を呼ぶため
本テスト内では ``setup_logfire`` を呼ばない。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from logfire.testing import CaptureLogfire
from redis.exceptions import ConnectionError as RedisConnectionError

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInsufficientBalanceError,
    AIProviderRequestInvalidError,
    AIProviderUsageLimitExhaustedError,
)
from app.audit.domain.event import Stage
from app.queue.helpers.stage_hold import set_stage_hold
from tests.logfire._metric_helpers import assert_attribute_contract

# set_stage_hold(reason: str) のシグネチャ自体は closed vocabulary を持たない
# (curation/assessment/embedding 共有の Redis hold setter で reason は素通し文字列)。
# 実運用では CurationFailureHandler._hold_reason が provider error の CODE を渡す
# (app/analysis/ai_provider_errors.py)。stage hold を要する回復クラス
# (OPERATOR_ACTION_REQUIRED / CONDITION_BASED_RECOVERY) の leaf CODE のみを
# 許可値域として列挙する (SSoT を持たないための test 側由来コメント付き定数)。
_ALLOWED_HOLD_REASONS = {
    AIProviderConfigurationError.CODE,
    AIProviderRequestInvalidError.CODE,
    AIProviderInsufficientBalanceError.CODE,
    AIProviderUsageLimitExhaustedError.CODE,
}


def _find_metric(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """metric dump から指定 name の metric を取り出す。"""
    return next((m for m in metrics if m["name"] == name), None)


def _sum_value(metric: dict[str, Any]) -> int:
    """sum 系 metric の合計値を取り出す (data_points が複数 attribute set もあり)。"""
    return sum(int(dp["value"]) for dp in metric["data"]["data_points"])


def _attributes_for(metric: dict[str, Any]) -> list[dict[str, Any]]:
    """metric の全 data_points の attribute dict を集める。"""
    return [dp.get("attributes", {}) for dp in metric["data"]["data_points"]]


@pytest.mark.asyncio
async def test_curation_hold_increments_hold_set_counter(
    capfire: CaptureLogfire,
) -> None:
    """Redis SET 成功時、``vector.curation.hold_set`` counter が +1。"""
    fake_redis = AsyncMock()
    fake_redis.set.return_value = True

    await set_stage_hold(fake_redis, Stage.CURATION, reason="ai_error_configuration")

    metrics = capfire.get_collected_metrics()
    hold_set = _find_metric(metrics, "vector.curation.hold_set")
    assert hold_set is not None, "vector.curation.hold_set が exporter に届かない"
    assert _sum_value(hold_set) == 1


@pytest.mark.asyncio
async def test_curation_hold_records_reason_attribute(
    capfire: CaptureLogfire,
) -> None:
    """成功時 counter の attribute は ``{"reason": "<CODE>"}`` のみ。"""
    fake_redis = AsyncMock()
    await set_stage_hold(
        fake_redis, Stage.CURATION, reason="ai_error_insufficient_balance"
    )

    metrics = capfire.get_collected_metrics()
    hold_set = _find_metric(metrics, "vector.curation.hold_set")
    assert hold_set is not None
    attrs_list = _attributes_for(hold_set)
    assert attrs_list == [{"reason": "ai_error_insufficient_balance"}]


@pytest.mark.asyncio
async def test_curation_hold_does_not_record_failed_counter_on_success(
    capfire: CaptureLogfire,
) -> None:
    """成功経路では ``vector.curation.hold_set_failed`` を一切 increment しない。"""
    fake_redis = AsyncMock()
    await set_stage_hold(fake_redis, Stage.CURATION, reason="ai_error_configuration")

    metrics = capfire.get_collected_metrics()
    failed = _find_metric(metrics, "vector.curation.hold_set_failed")
    # 未 record の場合 metric 自体が dump に出ないか、出ても 0
    if failed is not None:
        assert _sum_value(failed) == 0


@pytest.mark.asyncio
async def test_curation_hold_failure_increments_failed_counter(
    capfire: CaptureLogfire,
) -> None:
    """Redis SET 例外時、``vector.curation.hold_set_failed`` counter が +1。"""
    fake_redis = AsyncMock()
    fake_redis.set.side_effect = RedisConnectionError("connection refused")

    # set_stage_hold は best-effort で例外を呑む。
    await set_stage_hold(fake_redis, Stage.CURATION, reason="ai_error_configuration")

    metrics = capfire.get_collected_metrics()
    failed = _find_metric(metrics, "vector.curation.hold_set_failed")
    assert failed is not None, "hold_set_failed counter が exporter に届かない"
    assert _sum_value(failed) == 1


@pytest.mark.asyncio
async def test_curation_hold_failure_does_not_record_success_counter(
    capfire: CaptureLogfire,
) -> None:
    """失敗経路では ``vector.curation.hold_set`` 成功 counter は increment されない。"""
    fake_redis = AsyncMock()
    fake_redis.set.side_effect = RedisConnectionError("connection refused")
    await set_stage_hold(fake_redis, Stage.CURATION, reason="ai_error_configuration")

    metrics = capfire.get_collected_metrics()
    success = _find_metric(metrics, "vector.curation.hold_set")
    if success is not None:
        assert _sum_value(success) == 0


# attribute 値域契約 (ホワイトリスト oracle)


@pytest.mark.asyncio
async def test_hold_metrics_attributes_conform_to_declared_vocabulary(
    capfire: CaptureLogfire,
) -> None:
    """attribute が {"reason"} key のみで、値は provider error CODE の許可語彙内。

    将来 ``set_stage_hold`` に article_id / URL 等の dynamic 値が流入する
    regression を、既知文字列の blocklist ではなく許可値域で構造的に検知する。
    """
    fake_redis = AsyncMock()
    await set_stage_hold(fake_redis, Stage.CURATION, reason="ai_error_configuration")

    metrics = capfire.get_collected_metrics()
    assert_attribute_contract(
        metrics, "vector.curation.hold_set", allowed={"reason": _ALLOWED_HOLD_REASONS}
    )
