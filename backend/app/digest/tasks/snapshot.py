"""週次 snapshot 生成 cron タスク。

スケジュール:
- ``cron="5 15 * * 0"`` (UTC) = JST 月曜 00:05 — 直近完了週 (= 今いる週の前週)
  を JST 月曜起点で集計し、``weekly_trends_snapshots`` に 1 行 INSERT する

責務分離:
- 入口 task は cron 引数 (`force=False` 固定) から ``ReadyForDigest`` を構築し
  ``WeeklyTrendsSnapshotService.execute(ready)`` に委譲する gatekeeper
- ビジネスロジック (集計 / 保存) は Service 側
- precondition (既存 snapshot 判定) は ``ReadyForDigest.try_advance_from`` 側

エラー方針 (feedback_failure_visibility.md):
- 例外は捕まえずに伝播させる (taskiq 側の retry/log に委ねる)
- 既存 snapshot あり (Ready が None) は正常終了として扱う
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.brokers import broker_digest
from app.digest.application.snapshot import WeeklyTrendsSnapshotService
from app.digest.domain.ready import ReadyForDigest
from app.digest.domain.week import latest_completed_week_start, now_in_jst
from app.digest.repository.snapshots import SnapshotRepository

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
    week_start = latest_completed_week_start(now_in_jst())

    async with session_factory() as session:
        snapshot_repo = SnapshotRepository(session)
        ready = await ReadyForDigest.try_advance_from(
            week_start=week_start,
            force=False,
            snapshot_repo=snapshot_repo,
        )
    if ready is None:
        logger.info(
            "weekly_snapshot_task_skipped",
            week_start=week_start.isoformat(),
        )
        return

    service = WeeklyTrendsSnapshotService(session_factory)
    outcome = await service.execute(ready)
    logger.info(
        "weekly_snapshot_task_generated",
        week_start=outcome.week_start.isoformat(),
        source_analysis_count=outcome.source_analysis_count,
    )
