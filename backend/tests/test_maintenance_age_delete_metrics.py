"""``app.backfill.cleanup`` の年齢削除 metric 記録 oracle。

検証する性質:
- 削除発生時、``vector.curation.age_deleted`` counter が削除件数分 +N。
- 0 件 cycle でも ``vector.curation.age_delete_batch_size`` histogram に 0 が
  record される (平常 baseline を分布に残す契約)。
- attribute は ``{"stage": "curation"}`` 1 key 固定、article_id / URL に類する
  dynamic 値が attribute / dump 全体に混入しない (capfire 全文検索 oracle)。

実DBの削除結果とmetricを照合する。capfire fixture
が ``logfire.configure(...)`` を呼ぶため、本テスト内では ``setup_logfire`` を呼ばない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from app.backfill.cleanup import delete_aged_out_curations
from app.models.analyzable_article_record import AnalyzableArticleRecord


def _find_metric(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((m for m in metrics if m["name"] == name), None)


def _sum_value(metric: dict[str, Any]) -> int:
    return sum(int(dp["value"]) for dp in metric["data"]["data_points"])


def _attributes_for(metric: dict[str, Any]) -> list[dict[str, Any]]:
    return [dp.get("attributes", {}) for dp in metric["data"]["data_points"]]


@pytest.fixture
def invoke_cleanup(capfire, db_session, session_factory, sample_source):
    """指定IDの期限切れ記事を実DBから削除してmetricを回収する。"""

    async def invoke(aged_ids):
        old = datetime(2025, 12, 1, tzinfo=UTC)
        for article_id in aged_ids:
            db_session.add(
                AnalyzableArticleRecord(
                    id=article_id,
                    source_id=sample_source.id,
                    source_url=f"https://example.com/aged/{article_id}",
                    original_title="title",
                    original_content="content",
                    published_at=old,
                    created_at=old,
                )
            )
        await db_session.commit()
        await delete_aged_out_curations(
            session_factory,
            created_before=datetime(2026, 1, 1, tzinfo=UTC),
        )
        return capfire.get_collected_metrics()

    return invoke


@pytest.mark.asyncio
async def test_age_deleted_counter_increments_by_deleted_count(
    capfire: CaptureLogfire,
    invoke_cleanup,
) -> None:
    """N 件削除なら ``vector.curation.age_deleted`` counter は +N。"""
    metrics = await invoke_cleanup([101, 102, 103])

    age_deleted = _find_metric(metrics, "vector.curation.age_deleted")
    assert age_deleted is not None
    assert _sum_value(age_deleted) == 3


@pytest.mark.asyncio
async def test_age_deleted_attribute_is_stage_only(
    capfire: CaptureLogfire,
    invoke_cleanup,
) -> None:
    """counter attribute は ``{"stage": "curation"}`` 1 key 固定。"""
    metrics = await invoke_cleanup([201, 202])

    age_deleted = _find_metric(metrics, "vector.curation.age_deleted")
    assert age_deleted is not None
    attrs_list = _attributes_for(age_deleted)
    assert attrs_list == [{"stage": "curation"}]


@pytest.mark.asyncio
async def test_age_delete_batch_size_records_actual_count(
    capfire: CaptureLogfire,
    invoke_cleanup,
) -> None:
    """histogram に削除件数 N が record される (削除発生 cycle)。"""
    metrics = await invoke_cleanup([301, 302, 303, 304, 305])

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
    invoke_cleanup,
) -> None:
    """0 件 cycle も baseline として histogram に 0 を record する。

    counter は increment しないが histogram には ``record(0)`` が出る契約。
    平常 cycle の分布形を p99 参照に活用するための spec 上の意図。
    """
    metrics = await invoke_cleanup([])

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
    invoke_cleanup,
) -> None:
    """metric attribute / dump 全体に article_id 値が混入しない (capfire oracle)。

    article_id を attribute に乗せると cardinality 爆発する上、削除対象記事の
    識別情報が SaaS dashboard に流出する。
    """
    # 検出しやすい目印 ID を使う (空虚回避)
    distinctive_ids = [987654321, 123456789]
    metrics = await invoke_cleanup(distinctive_ids)

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
