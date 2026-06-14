"""backfill cron の Logfire core metric contract。

救済 cron の dashboard で毎回見る値だけを low-cardinality metric として pin する。
対象 ID や URL などの詳細は pipeline_events の責務で、metric attribute には
``stage`` / ``action`` だけを許す。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire

from app.queue.helpers.backlog import BackfillTarget
from app.queue.tasks import backfill as tasks


@dataclass(frozen=True, slots=True)
class _MetricCase:
    name: str
    task: Callable[[Any], Awaitable[None]]
    stage: str
    action: str
    enabled_attr: str
    hold_patch: str
    ageout_patch: str
    queue_task_patch: str
    count_method: str
    target_method: str


CASES = [
    _MetricCase(
        name="curation",
        task=tasks.backfill_curations,
        stage="curation",
        action="deleted",
        enabled_attr="backfill_curations_enabled",
        hold_patch="app.queue.tasks.backfill.is_curation_held",
        ageout_patch="app.queue.tasks.backfill._delete_aged_out_curations",
        queue_task_patch="app.queue.tasks.backfill.curate_content",
        count_method="count_articles_pending_curation",
        target_method="curation_targets_pending",
    ),
    _MetricCase(
        name="assessment",
        task=tasks.backfill_assessments,
        stage="assessment",
        action="excluded",
        enabled_attr="backfill_assessments_enabled",
        hold_patch="app.queue.tasks.backfill.is_assessment_held",
        ageout_patch="app.queue.tasks.backfill._exclude_aged_out_assessments",
        queue_task_patch="app.queue.tasks.backfill.assess_content",
        count_method="count_curations_pending_assessment",
        target_method="assessment_targets_pending",
    ),
    _MetricCase(
        name="embedding",
        task=tasks.backfill_embeddings,
        stage="embedding",
        action="excluded",
        enabled_attr="backfill_embeddings_enabled",
        hold_patch="app.queue.tasks.backfill.is_embedding_held",
        ageout_patch="app.queue.tasks.backfill._exclude_aged_out_embeddings",
        queue_task_patch="app.queue.tasks.backfill.generate_embedding",
        count_method="count_analyzed_articles_pending_embedding",
        target_method="embedding_targets_pending",
    ),
]


def _ctx() -> SimpleNamespace:
    """session_factory だけを持つ taskiq Context test double。"""
    return SimpleNamespace(state=SimpleNamespace(session_factory=MagicMock()))


def _target(target_id: int) -> BackfillTarget:
    """backfill enqueue 対象の test double を返す。"""
    return BackfillTarget(
        target_id=target_id,
        analyzable_article_id=target_id + 1000,
        source_name="VentureBeat",
    )


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
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_backlog_and_hold_false_are_recorded_for_each_stage(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """hold なし通常 tick は held=0 と stage 別 backlog 真値を記録する。"""
    backlog_count = 7
    backlog = MagicMock()
    setattr(backlog, case.count_method, AsyncMock(return_value=backlog_count))
    setattr(backlog, case.target_method, AsyncMock(return_value=[_target(101)]))

    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=False)),
        patch(case.ageout_patch, AsyncMock(return_value=0)),
        patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            AsyncMock(return_value=0),
        ),
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        await case.task(ctx=_ctx())

    metrics = _collected_metrics(capfire)
    held_metric = _find_metric(metrics, "vector.backfill.held")
    assert held_metric is not None
    assert _has_point(held_metric, value=0, attributes={"stage": case.stage})

    backlog_metric = _find_metric(metrics, "vector.backfill.backlog")
    assert backlog_metric is not None
    assert _has_point(
        backlog_metric,
        value=backlog_count,
        attributes={"stage": case.stage},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_empty_backlog_records_zero_for_each_stage(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """対象 0 件でも backlog=0 を set し、dashboard の stale 表示を避ける。"""
    backlog = MagicMock()
    setattr(backlog, case.count_method, AsyncMock(return_value=0))
    setattr(backlog, case.target_method, AsyncMock(return_value=[]))

    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=False)),
        patch(case.ageout_patch, AsyncMock(return_value=0)),
        patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        await case.task(ctx=_ctx())

    backlog_metric = _find_metric(
        _collected_metrics(capfire), "vector.backfill.backlog"
    )
    assert backlog_metric is not None
    assert _has_point(backlog_metric, value=0, attributes={"stage": case.stage})


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_hold_true_records_held_and_skips_backlog_budget_and_kiq(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """hold 中は held=1 だけを記録し、backlog / budget / kiq に進まない。"""
    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=True)),
        patch(case.ageout_patch, AsyncMock(return_value=0)) as ageout,
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch("app.queue.tasks.backfill.consume_daily_budget", AsyncMock()) as budget,
        patch(case.queue_task_patch) as queue_task,
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        await case.task(ctx=_ctx())

    ageout.assert_not_called()
    backlog_cls.assert_not_called()
    budget.assert_not_called()
    queue_task.kiq.assert_not_called()

    held_metric = _find_metric(_collected_metrics(capfire), "vector.backfill.held")
    assert held_metric is not None
    assert _has_point(held_metric, value=1, attributes={"stage": case.stage})


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_kill_switch_disabled_does_not_record_hold_or_backlog(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """kill switch off は hold check も metric set も行わない。"""
    with (
        patch.object(tasks.settings, case.enabled_attr, False),
        patch(case.hold_patch, AsyncMock()) as held,
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        await case.task(ctx=_ctx())

    held.assert_not_called()
    backlog_cls.assert_not_called()
    metrics = _collected_metrics(capfire)
    assert _find_metric(metrics, "vector.backfill.held") is None
    assert _find_metric(metrics, "vector.backfill.backlog") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_dispatched_counter_counts_only_successful_kiq(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """kiq 成功件数だけを vector.backfill.dispatched に加算する。"""
    targets = [_target(1), _target(2), _target(3)]
    backlog = MagicMock()
    setattr(backlog, case.count_method, AsyncMock(return_value=len(targets)))
    setattr(backlog, case.target_method, AsyncMock(return_value=targets))
    queue_task = SimpleNamespace(
        kiq=AsyncMock(side_effect=[None, RuntimeError("queue down"), None])
    )

    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=False)),
        patch(case.ageout_patch, AsyncMock(return_value=0)),
        patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            AsyncMock(return_value=len(targets)),
        ),
        patch(case.queue_task_patch, queue_task),
        patch("app.queue.tasks.backfill._append_backfill_item_event", AsyncMock()),
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        await case.task(ctx=_ctx())

    dispatched = _find_metric(_collected_metrics(capfire), "vector.backfill.dispatched")
    assert dispatched is not None
    assert _sum_values(dispatched) == 2
    assert _attributes_for(dispatched) == [{"stage": case.stage}]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_aged_out_counter_records_completed_cleanup_count(
    capfire: CaptureLogfire,
    case: _MetricCase,
) -> None:
    """age-out helper が返した commit 成功件数だけを stage/action 付きで記録する。"""
    backlog = MagicMock()
    setattr(backlog, case.count_method, AsyncMock(return_value=0))
    setattr(backlog, case.target_method, AsyncMock(return_value=[]))

    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=False)),
        patch(case.ageout_patch, AsyncMock(return_value=4)),
        patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        await case.task(ctx=_ctx())

    aged_out = _find_metric(_collected_metrics(capfire), "vector.backfill.aged_out")
    assert aged_out is not None
    assert _sum_values(aged_out) == 4
    assert _attributes_for(aged_out) == [{"stage": case.stage, "action": case.action}]


@pytest.mark.asyncio
async def test_metric_attributes_do_not_leak_dynamic_target_values(
    capfire: CaptureLogfire,
) -> None:
    """metric attribute / dump 全体に target_id や source_name を混ぜない。"""
    case = CASES[0]
    distinctive_ids = [987654321, 876543210]
    backlog = MagicMock()
    setattr(backlog, case.count_method, AsyncMock(return_value=42))
    setattr(
        backlog,
        case.target_method,
        AsyncMock(return_value=[_target(target_id) for target_id in distinctive_ids]),
    )
    queue_task = SimpleNamespace(kiq=AsyncMock())

    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=False)),
        patch(case.ageout_patch, AsyncMock(return_value=5)),
        patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            AsyncMock(return_value=len(distinctive_ids)),
        ),
        patch(case.queue_task_patch, queue_task),
        patch("app.queue.tasks.backfill._append_backfill_item_event", AsyncMock()),
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        await case.task(ctx=_ctx())

    metrics = _collected_metrics(capfire)
    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    for target_id in distinctive_ids:
        assert str(target_id) not in dumped
    assert "VentureBeat" not in dumped

    expected_keys = {
        "vector.backfill.backlog": {"stage"},
        "vector.backfill.held": {"stage"},
        "vector.backfill.dispatched": {"stage"},
        "vector.backfill.aged_out": {"stage", "action"},
    }
    for metric_name, keys in expected_keys.items():
        metric = _find_metric(metrics, metric_name)
        assert metric is not None
        for attrs in _attributes_for(metric):
            assert set(attrs.keys()) == keys
