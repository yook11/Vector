"""``app/queue/helpers/stage_hold.py`` の curation Logfire metric 記録 oracle。

検証する性質:
- ``set_curation_hold`` 成功時に ``vector.curation.hold_set`` counter が +1
  され、attribute は ``{"reason": <CODE 由来>}`` のみ。
- ``set_curation_hold`` の Redis SET 失敗時は ``vector.curation.hold_set_failed``
  counter が +1 され、成功 counter は increment されない。
- attribute 経路に PII (article_id / URL 様の dynamic 値) が混入しない構造的
  契約を capfire の metric dump 全文検索で oracle 化する。

capfire fixture が ``logfire.configure(send_to_logfire=False, ...)`` を呼ぶため
本テスト内では ``setup_logfire`` を呼ばない。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from logfire.testing import CaptureLogfire
from redis.exceptions import ConnectionError as RedisConnectionError

from app.queue.helpers.stage_hold import set_curation_hold


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
async def test_set_curation_hold_increments_hold_set_counter(
    capfire: CaptureLogfire,
) -> None:
    """Redis SET 成功時、``vector.curation.hold_set`` counter が +1。"""
    fake_redis = AsyncMock()
    fake_redis.set.return_value = True

    await set_curation_hold(fake_redis, reason="ai_error_configuration")

    metrics = capfire.get_collected_metrics()
    hold_set = _find_metric(metrics, "vector.curation.hold_set")
    assert hold_set is not None, "vector.curation.hold_set が exporter に届かない"
    assert _sum_value(hold_set) == 1


@pytest.mark.asyncio
async def test_set_curation_hold_records_reason_attribute(
    capfire: CaptureLogfire,
) -> None:
    """成功時 counter の attribute は ``{"reason": "<CODE>"}`` のみ。"""
    fake_redis = AsyncMock()
    await set_curation_hold(fake_redis, reason="ai_error_insufficient_balance")

    metrics = capfire.get_collected_metrics()
    hold_set = _find_metric(metrics, "vector.curation.hold_set")
    assert hold_set is not None
    attrs_list = _attributes_for(hold_set)
    assert attrs_list == [{"reason": "ai_error_insufficient_balance"}]


@pytest.mark.asyncio
async def test_set_curation_hold_does_not_record_failed_counter_on_success(
    capfire: CaptureLogfire,
) -> None:
    """成功経路では ``vector.curation.hold_set_failed`` を一切 increment しない。"""
    fake_redis = AsyncMock()
    await set_curation_hold(fake_redis, reason="ai_error_configuration")

    metrics = capfire.get_collected_metrics()
    failed = _find_metric(metrics, "vector.curation.hold_set_failed")
    # 未 record の場合 metric 自体が dump に出ないか、出ても 0
    if failed is not None:
        assert _sum_value(failed) == 0


@pytest.mark.asyncio
async def test_set_curation_hold_failure_increments_failed_counter(
    capfire: CaptureLogfire,
) -> None:
    """Redis SET 例外時、``vector.curation.hold_set_failed`` counter が +1。"""
    fake_redis = AsyncMock()
    fake_redis.set.side_effect = RedisConnectionError("connection refused")

    # set_curation_hold は best-effort で例外を呑む。
    await set_curation_hold(fake_redis, reason="ai_error_configuration")

    metrics = capfire.get_collected_metrics()
    failed = _find_metric(metrics, "vector.curation.hold_set_failed")
    assert failed is not None, "hold_set_failed counter が exporter に届かない"
    assert _sum_value(failed) == 1


@pytest.mark.asyncio
async def test_set_curation_hold_failure_does_not_record_success_counter(
    capfire: CaptureLogfire,
) -> None:
    """失敗経路では ``vector.curation.hold_set`` 成功 counter は increment されない。"""
    fake_redis = AsyncMock()
    fake_redis.set.side_effect = RedisConnectionError("connection refused")
    await set_curation_hold(fake_redis, reason="ai_error_configuration")

    metrics = capfire.get_collected_metrics()
    success = _find_metric(metrics, "vector.curation.hold_set")
    if success is not None:
        assert _sum_value(success) == 0


# PII 非含有 oracle


@pytest.mark.asyncio
async def test_hold_metrics_attribute_does_not_leak_dynamic_pii(
    capfire: CaptureLogfire,
) -> None:
    """attribute set に PII 様 dynamic 値が混入しない (capfire 全文検索 oracle)。

    将来 ``set_curation_hold`` に article_id / URL 等の引数が追加されて
    metric attribute に流入する regression を構造的に検知する。
    """
    # reason は SystemConfig 由来の enum-like 値に限定される。
    fake_redis = AsyncMock()
    await set_curation_hold(fake_redis, reason="ai_error_configuration")

    metrics = capfire.get_collected_metrics()
    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    # 現状の attribute 経路を pin: reason 1 key のみ
    hold_set = _find_metric(metrics, "vector.curation.hold_set")
    assert hold_set is not None
    for attrs in _attributes_for(hold_set):
        assert set(attrs.keys()) == {"reason"}, (
            f"hold_set attribute に予期しない key: {attrs.keys()}"
        )
    # 確実な全文検索: article_id / url / url-like 語が dump 全体に出ない
    forbidden_substrings = ("article_id", "http://", "https://", "raw_url")
    for needle in forbidden_substrings:
        assert needle not in dumped, (
            f"hold metric dump に PII 様文字列 {needle!r} が混入"
        )
