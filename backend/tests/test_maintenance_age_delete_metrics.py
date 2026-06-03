"""``app/queue/tasks/backfill.py`` の年齢削除 metric 記録 oracle。

検証する性質:
- 削除発生時、``vector.curation.age_deleted`` counter が削除件数分 +N。
- 0 件 cycle でも ``vector.curation.age_delete_batch_size`` histogram に 0 が
  record される (平常 baseline を分布に残す契約)。
- attribute は ``{"stage": "curation"}`` 1 key 固定、article_id / URL に類する
  dynamic 値が attribute / dump 全体に混入しない (capfire 全文検索 oracle)。

unit 層で metric 経路のみ検証するため、repository 群は patch する。capfire fixture
が ``logfire.configure(...)`` を呼ぶため、本テスト内では ``setup_logfire`` を呼ばない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire

from app.queue.tasks.backfill import _delete_aged_out_curations

# ヘルパー


def _find_metric(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((m for m in metrics if m["name"] == name), None)


def _sum_value(metric: dict[str, Any]) -> int:
    return sum(int(dp["value"]) for dp in metric["data"]["data_points"])


def _attributes_for(metric: dict[str, Any]) -> list[dict[str, Any]]:
    return [dp.get("attributes", {}) for dp in metric["data"]["data_points"]]


# session_factory / repository stub


def _make_session_factory() -> Any:
    """session_factory(): async context manager を返す callable (AsyncSession stub)。"""

    class _AsyncSessionStub:
        def __init__(self) -> None:
            self.commit = AsyncMock()

        async def __aenter__(self) -> _AsyncSessionStub:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    return MagicMock(side_effect=_AsyncSessionStub)


async def _invoke_with_aged_ids(
    capfire: CaptureLogfire, aged_ids: list[int]
) -> list[dict[str, Any]]:
    """``_delete_aged_out_curations`` を ``article_ids`` 固定で呼んで metric を回収。"""
    backlog = MagicMock()
    backlog.article_ids_aged_out_curation = AsyncMock(return_value=aged_ids)
    audit_repo = MagicMock()
    audit_repo.append_backfill_curation_aged_out = AsyncMock()
    article_repo = MagicMock()
    article_repo.delete_by_id = AsyncMock()

    with (
        patch(
            "app.queue.tasks.backfill.PipelineBacklog",
            return_value=backlog,
        ),
        patch(
            "app.audit.stages.curation.CurationAuditRepository",
            return_value=audit_repo,
        ),
        patch(
            "app.repositories.articles.ArticleRepository",
            return_value=article_repo,
        ),
    ):
        await _delete_aged_out_curations(
            _make_session_factory(),
            created_before=datetime(2026, 1, 1, tzinfo=UTC),
        )

    return capfire.get_collected_metrics()


# age_deleted counter


@pytest.mark.asyncio
async def test_age_deleted_counter_increments_by_deleted_count(
    capfire: CaptureLogfire,
) -> None:
    """N 件削除なら ``vector.curation.age_deleted`` counter は +N。"""
    metrics = await _invoke_with_aged_ids(capfire, [101, 102, 103])

    age_deleted = _find_metric(metrics, "vector.curation.age_deleted")
    assert age_deleted is not None
    assert _sum_value(age_deleted) == 3


@pytest.mark.asyncio
async def test_age_deleted_attribute_is_stage_only(
    capfire: CaptureLogfire,
) -> None:
    """counter attribute は ``{"stage": "curation"}`` 1 key 固定。"""
    metrics = await _invoke_with_aged_ids(capfire, [201, 202])

    age_deleted = _find_metric(metrics, "vector.curation.age_deleted")
    assert age_deleted is not None
    attrs_list = _attributes_for(age_deleted)
    assert attrs_list == [{"stage": "curation"}]


# age_delete_batch_size histogram


@pytest.mark.asyncio
async def test_age_delete_batch_size_records_actual_count(
    capfire: CaptureLogfire,
) -> None:
    """histogram に削除件数 N が record される (削除発生 cycle)。"""
    metrics = await _invoke_with_aged_ids(capfire, [301, 302, 303, 304, 305])

    hist = _find_metric(metrics, "vector.curation.age_delete_batch_size")
    assert hist is not None
    # histogram の data_point は count / sum を持つ; 1 cycle = 1 record なので
    # count=1 / sum=5 (deleted 数)。
    data_points = hist["data"]["data_points"]
    assert len(data_points) >= 1
    total_sum = sum(int(dp["sum"]) for dp in data_points)
    assert total_sum == 5


@pytest.mark.asyncio
async def test_age_delete_batch_size_records_zero_baseline(
    capfire: CaptureLogfire,
) -> None:
    """0 件 cycle も baseline として histogram に 0 を record する。

    counter は increment しないが histogram には ``record(0)`` が出る契約。
    平常 cycle の分布形を p99 参照に活用するための spec 上の意図。
    """
    metrics = await _invoke_with_aged_ids(capfire, [])

    # counter は increment されない (0 件のため)
    age_deleted = _find_metric(metrics, "vector.curation.age_deleted")
    if age_deleted is not None:
        assert _sum_value(age_deleted) == 0

    # histogram は 0 を record (baseline)
    hist = _find_metric(metrics, "vector.curation.age_delete_batch_size")
    assert hist is not None, "0 件 cycle でも histogram baseline が必要"
    data_points = hist["data"]["data_points"]
    assert len(data_points) >= 1
    total_sum = sum(int(dp["sum"]) for dp in data_points)
    assert total_sum == 0
    # count = 1 (record 自体は呼ばれた)
    total_count = sum(int(dp["count"]) for dp in data_points)
    assert total_count == 1


# PII 非含有 oracle


@pytest.mark.asyncio
async def test_age_delete_metrics_do_not_leak_article_ids(
    capfire: CaptureLogfire,
) -> None:
    """metric attribute / dump 全体に article_id 値が混入しない (capfire oracle)。

    article_id を attribute に乗せると cardinality 爆発する上、削除対象記事の
    識別情報が SaaS dashboard に流出する。
    """
    # 検出しやすい目印 ID を使う (空虚回避)
    distinctive_ids = [987654321, 123456789]
    metrics = await _invoke_with_aged_ids(capfire, distinctive_ids)

    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    for article_id in distinctive_ids:
        assert str(article_id) not in dumped, (
            f"age-delete metric dump に article_id {article_id} が混入"
        )
    # attribute set は stage 1 key のみ
    age_deleted = _find_metric(metrics, "vector.curation.age_deleted")
    assert age_deleted is not None
    for attrs in _attributes_for(age_deleted):
        assert set(attrs.keys()) == {"stage"}, (
            f"age_deleted attribute に予期しない key: {attrs.keys()}"
        )
    hist = _find_metric(metrics, "vector.curation.age_delete_batch_size")
    assert hist is not None
    for attrs in _attributes_for(hist):
        assert set(attrs.keys()) == {"stage"}, (
            f"age_delete_batch_size attribute に予期しない key: {attrs.keys()}"
        )
