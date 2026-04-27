"""週次 snapshot 生成 cron タスク。

スケジュール:
- ``cron="5 15 * * 0"`` (UTC) = JST 月曜 00:05 — 直近完了週 (= 今いる週の前週)
  を JST 月曜起点で集計し、``weekly_trends_snapshots`` に 1 行 INSERT する

責務分離:
- ビジネスロジックは ``WeeklyTrendsSnapshotService`` 側 (集計 / 既存判定 /
  トランザクション境界 / Outcome 判断)
- ここはキュー機構 (cron 起動 + ``ctx.state.session_factory`` 注入) のみ

エラー方針 (feedback_failure_visibility.md):
- 例外は捕まえずに伝播させる (taskiq 側の retry/log に委ねる)
- ``Skipped`` (既存 snapshot と並行レース敗北) は正常終了として扱う
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.brokers import broker_digest
from app.digest.application.snapshot import (
    Generated,
    Skipped,
    WeeklyTrendsSnapshotService,
)

logger = structlog.get_logger(__name__)


@broker_digest.task(
    task_name="generate_weekly_snapshot",
    timeout=600,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": "5 15 * * 0"}],
)
async def generate_weekly_snapshot(ctx: Context = TaskiqDepends()) -> None:
    """直近完了週 (JST 月曜起点) の weekly trends snapshot を生成する。"""
    session_factory = ctx.state.session_factory
    service = WeeklyTrendsSnapshotService(session_factory)
    outcome = await service.generate_for_latest_completed_week()
    match outcome:
        case Generated(week_start=ws, source_analysis_count=n):
            logger.info(
                "weekly_snapshot_task_generated",
                week_start=ws.isoformat(),
                source_analysis_count=n,
            )
        case Skipped(week_start=ws):
            logger.info(
                "weekly_snapshot_task_skipped",
                week_start=ws.isoformat(),
            )
