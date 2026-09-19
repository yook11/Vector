"""backfill の Logfire core metric contract。

救済の dashboard で毎回見る値だけを low-cardinality metric として pin する。
対象 ID や URL などの詳細は pipeline_events の責務で、metric attribute には
``stage`` / ``action`` だけを許す。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire

from app.backfill import service
from app.backfill.targets import BackfillEventTarget, BackfillTarget
from app.collection.events import AnalyzableArticleCreated
from app.collection.sources.source_name import SourceName
from app.outbox.publishing.errors import PublishError
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    PublishFailed,
    PublishSucceeded,
)

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _MetricCase:
    stage: str
    entry: Callable[..., Awaitable[None]]
    action: str
    ageout_patch: str
    count_method: str
    target_method: str


CASES = [
    _MetricCase(
        stage="curation",
        entry=service.backfill_curations,
        action="deleted",
        ageout_patch="app.backfill.service.delete_aged_out_curations",
        count_method="count_articles_pending_curation",
        target_method="curation_events_pending",
    ),
    _MetricCase(
        stage="assessment",
        entry=service.backfill_assessments,
        action="excluded",
        ageout_patch="app.backfill.service.exclude_aged_out_assessments",
        count_method="count_curations_pending_assessment",
        target_method="assessment_events_pending",
    ),
    _MetricCase(
        stage="embedding",
        entry=service.backfill_embeddings,
        action="excluded",
        ageout_patch="app.backfill.service.exclude_aged_out_embeddings",
        count_method="count_analyzed_articles_pending_embedding",
        target_method="embedding_events_pending",
    ),
]
_IDS = [case.stage for case in CASES]


def _session_factory() -> MagicMock:
    """``async with session_factory()`` だけを満たす test double。"""

    @asynccontextmanager
    async def _session():
        yield MagicMock()

    return MagicMock(side_effect=_session)


def _target(target_id: int) -> BackfillEventTarget:
    """metric は payload の種類に依存しないため、全工程で同じ形の対象を使う。"""
    return BackfillEventTarget(
        target=BackfillTarget(
            target_id=target_id,
            analyzable_article_id=target_id + 1000,
            source_name=SourceName("VentureBeat"),
        ),
        occurred_at=NOW,
        payload=AnalyzableArticleCreated(analyzable_article_id=target_id),
    )


def _backlog(case: _MetricCase, *, count: int, targets: list[BackfillEventTarget]):
    backlog = MagicMock()
    setattr(backlog, case.count_method, AsyncMock(return_value=count))
    setattr(backlog, case.target_method, AsyncMock(return_value=targets))
    return backlog


def _publisher(*, failed_positions: frozenset[int] = frozenset()) -> MagicMock:
    """入力順の指定位置だけ送信失敗にする publisher。"""
    publisher = MagicMock()
    publisher.publish_batch.side_effect = lambda items: BatchPublishResult(
        tuple(
            PublishFailed(item.event_id, PublishError())
            if position in failed_positions
            else PublishSucceeded(item.event_id)
            for position, item in enumerate(items)
        )
    )
    return publisher


async def _run(
    case: _MetricCase,
    *,
    enabled: bool = True,
    cleaned: int = 0,
    count: int = 0,
    targets: list[BackfillEventTarget] | None = None,
    publisher: MagicMock | None = None,
) -> MagicMock:
    backlog = _backlog(case, count=count, targets=targets or [])
    with (
        patch(case.ageout_patch, AsyncMock(return_value=cleaned)),
        patch(
            "app.backfill.service.PipelineBacklog", return_value=backlog
        ) as backlog_cls,
        patch("app.backfill.service.append_backfill_item_event", AsyncMock()),
        patch("app.backfill.service.append_backfill_run_event", AsyncMock()),
    ):
        await case.entry(
            _session_factory(),
            publisher or _publisher(),
            enabled=enabled,
            now=NOW,
        )
    return backlog_cls


def _find_metric(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((m for m in metrics if m["name"] == name), None)


def _sum_values(metric: dict[str, Any]) -> int:
    return sum(int(dp["value"]) for dp in metric["data"]["data_points"])


def _attributes_for(metric: dict[str, Any]) -> list[dict[str, Any]]:
    return [dp.get("attributes", {}) for dp in metric["data"]["data_points"]]


def _has_point(
    metric: dict[str, Any],
    *,
    value: int,
    attributes: dict[str, str],
) -> bool:
    return any(
        dp["value"] == value and dp.get("attributes") == attributes
        for dp in metric["data"]["data_points"]
    )


def _collected_metrics(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    """metric 未記録時の capfire 例外を空 list として扱う。"""
    try:
        return capfire.get_collected_metrics()
    except AttributeError:
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_backlog_true_count_is_recorded_for_each_stage(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """送信件数ではなく、上限に縛られない backlog 真値を stage 別に記録する。"""
    backlog_count = 7
    await _run(case, count=backlog_count, targets=[_target(101)])

    backlog_metric = _find_metric(
        _collected_metrics(capfire), "vector.backfill.backlog"
    )
    assert backlog_metric is not None
    assert _has_point(
        backlog_metric,
        value=backlog_count,
        attributes={"stage": case.stage},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_empty_backlog_records_zero_for_each_stage(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """対象 0 件でも backlog=0 を set し、dashboard の stale 表示を避ける。"""
    await _run(case, count=0, targets=[])

    backlog_metric = _find_metric(
        _collected_metrics(capfire), "vector.backfill.backlog"
    )
    assert backlog_metric is not None
    assert _has_point(backlog_metric, value=0, attributes={"stage": case.stage})


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_disabled_entry_does_not_record_metrics(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """無効化された工程は DB を読まず、metric も記録しない。"""
    backlog_cls = await _run(case, enabled=False, cleaned=3, count=5)

    backlog_cls.assert_not_called()
    metrics = _collected_metrics(capfire)
    assert _find_metric(metrics, "vector.backfill.backlog") is None
    assert _find_metric(metrics, "vector.backfill.aged_out") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_dispatched_counter_counts_only_accepted_events(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """送信先が受け付けた件数だけを vector.backfill.dispatched に加算する。"""
    targets = [_target(1), _target(2), _target(3)]
    failed_positions = frozenset({1})
    await _run(
        case,
        count=len(targets),
        targets=targets,
        publisher=_publisher(failed_positions=failed_positions),
    )

    dispatched = _find_metric(_collected_metrics(capfire), "vector.backfill.dispatched")
    assert dispatched is not None
    assert _sum_values(dispatched) == len(targets) - len(failed_positions)
    assert _attributes_for(dispatched) == [{"stage": case.stage}]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_aged_out_counter_records_completed_cleanup_count(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """期限切れ整理が返した commit 成功件数だけを stage/action 付きで記録する。"""
    cleaned = 4
    await _run(case, cleaned=cleaned)

    aged_out = _find_metric(_collected_metrics(capfire), "vector.backfill.aged_out")
    assert aged_out is not None
    assert _sum_values(aged_out) == cleaned
    assert _attributes_for(aged_out) == [{"stage": case.stage, "action": case.action}]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_IDS)
async def test_metric_attributes_do_not_leak_dynamic_target_values(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """metric attribute / dump 全体に target_id や source_name を混ぜない。"""
    distinctive_ids = [987654321, 876543210]
    await _run(
        case,
        cleaned=5,
        count=42,
        targets=[_target(target_id) for target_id in distinctive_ids],
    )

    metrics = _collected_metrics(capfire)
    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    for target_id in distinctive_ids:
        assert str(target_id) not in dumped
    assert "VentureBeat" not in dumped

    expected_keys = {
        "vector.backfill.backlog": {"stage"},
        "vector.backfill.dispatched": {"stage"},
        "vector.backfill.aged_out": {"stage", "action"},
    }
    for metric_name, keys in expected_keys.items():
        metric = _find_metric(metrics, metric_name)
        assert metric is not None
        for attrs in _attributes_for(metric):
            assert set(attrs.keys()) == keys
