"""rolling 7d Trend Discovery cron タスク。

スケジュール:
- ``CRON_TREND_DISCOVERY`` (UTC) = JST 毎日 00:05 — 直近完了 7 日窓
  (``[今日0:00 - 7d, 今日0:00)`` JST) を集計し、
  集計対象 analysis がある場合のみ ``trends_snapshots`` に 1 行 INSERT する

責務分離:
- 入口 task は cron 引数 (`force=False` 固定) から ``ReadyForTrendDiscovery`` を構築し
  ``TrendDiscoveryService.execute(ready)`` に委譲する gatekeeper
- ビジネスロジック (集計 / 保存) は Service 側
- precondition (既存 snapshot 判定) は ``ReadyForTrendDiscovery.try_advance_from`` 側

エラー方針 (feedback_failure_visibility.md):
- 例外は捕まえずに伝播させる (taskiq 側の retry/log に委ねる)
- 既存 snapshot あり (Ready が None) は正常終了として扱う
- 集計対象記事 0 件は Service の正常 skip として扱う
"""

from __future__ import annotations

from datetime import date, timedelta

import structlog
from taskiq import Context, TaskiqDepends

from app.audit.domain.event import EventType, Stage
from app.audit.stages.trend_discovery import (
    TrendDiscoveryOutcomeCode,
    append_trend_discovery_run_event_best_effort,
)
from app.config import settings
from app.insights.trend_discovery.domain.ready import ReadyForTrendDiscovery
from app.insights.trend_discovery.domain.window import latest_window_end, now_in_jst
from app.insights.trend_discovery.repository import SnapshotRepository
from app.insights.trend_discovery.service import (
    TRENDS_REVALIDATE_TAGS,
    SkippedNoTargetArticles,
    TrendDiscoveryCompleted,
    TrendDiscoveryConflict,
    TrendDiscoveryService,
)
from app.logfire.stage_span import pipeline_stage_span
from app.queue.brokers import broker_trend_discovery
from app.queue.schedule import CRON_TREND_DISCOVERY
from app.shared.revalidate import FrontendRevalidateNotifier

logger = structlog.get_logger(__name__)

_WINDOW = timedelta(days=7)


@broker_trend_discovery.task(
    task_name="run_trend_discovery",
    timeout=600,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_TREND_DISCOVERY}],
)
async def run_trend_discovery(ctx: Context = TaskiqDepends()) -> None:
    """rolling 7d window (JST 当日 0:00 を上限) の trend discovery を実行する。"""
    with pipeline_stage_span(Stage.TREND_DISCOVERY, op="run_trend_discovery"):
        session_factory = ctx.state.session_factory
        window_end = latest_window_end(now_in_jst())
        window_start = _window_start(window_end)

        try:
            async with session_factory() as session:
                snapshot_repo = SnapshotRepository(session)
                ready = await ReadyForTrendDiscovery.try_advance_from(
                    window_end=window_end,
                    force=False,
                    snapshot_repo=snapshot_repo,
                )
        except Exception as exc:
            await append_trend_discovery_run_event_best_effort(
                session_factory,
                event_type=EventType.FAILED,
                outcome_code=TrendDiscoveryOutcomeCode.RUN_FAILED,
                window_start=window_start,
                window_end=window_end,
                trigger="cron",
                requested_update=False,
                exc=exc,
            )
            raise

        if ready is None:
            # 既存 snapshot あり = benign な冪等 skip。監査に焼かず log で観測する。
            logger.info(
                "trend_discovery_task_skipped_already_exists",
                window_end=window_end.isoformat(),
            )
            return

        service = TrendDiscoveryService(session_factory)
        try:
            outcome = await service.execute(ready)
        except Exception as exc:
            await append_trend_discovery_run_event_best_effort(
                session_factory,
                event_type=EventType.FAILED,
                outcome_code=TrendDiscoveryOutcomeCode.RUN_FAILED,
                window_start=window_start,
                window_end=window_end,
                trigger="cron",
                requested_update=False,
                exc=exc,
            )
            raise

        if isinstance(outcome, SkippedNoTargetArticles):
            # 集計対象 0 件 = benign な skip。監査に焼かず log で観測する。
            logger.info(
                "trend_discovery_task_skipped_no_target_articles",
                window_end=outcome.window_end.isoformat(),
            )
            return
        if isinstance(outcome, TrendDiscoveryConflict):
            # race 敗北 = benign な skip。監査に焼かず log で観測する。
            logger.info(
                "trend_discovery_task_conflict",
                window_end=outcome.window_end.isoformat(),
                source_analysis_count=outcome.source_analysis_count,
                category_count=outcome.completed_category_count,
            )
            return

        outcome_code = (
            TrendDiscoveryOutcomeCode.RUN_UPDATED
            if isinstance(outcome, TrendDiscoveryCompleted) and outcome.updated
            else TrendDiscoveryOutcomeCode.RUN_COMPLETED
        )
        await append_trend_discovery_run_event_best_effort(
            session_factory,
            event_type=EventType.SUCCEEDED,
            outcome_code=outcome_code,
            window_start=window_start,
            window_end=outcome.window_end,
            trigger="cron",
            requested_update=False,
            source_analysis_count=outcome.source_analysis_count,
            completed_category_count=outcome.completed_category_count,
        )

        logger.info(
            "trend_discovery_task_completed",
            window_end=outcome.window_end.isoformat(),
            source_analysis_count=outcome.source_analysis_count,
            category_count=outcome.completed_category_count,
            updated=outcome.updated,
        )

        # 生成成功 (INSERT / force 上書き) で frontend キャッシュを無効化する。
        # notifier 内部で warn 降格するため例外は伝播しない。
        notifier = FrontendRevalidateNotifier.from_settings(settings)
        await notifier.notify(tags=TRENDS_REVALIDATE_TAGS)


def _window_start(window_end: date) -> date:
    return window_end - _WINDOW
