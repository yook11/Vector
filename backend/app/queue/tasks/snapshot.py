"""rolling 7d daily snapshot 生成 cron タスク。

スケジュール:
- ``CRON_WEEKLY_SNAPSHOT`` (UTC) = JST 毎日 00:05 — 直近完了 7 日窓
  (``[今日0:00 - 7d, 今日0:00)`` JST) を集計し、
  ``weekly_trends_snapshots`` に 1 行 INSERT する

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

from app.insights.snapshot.application.snapshot import WeeklyTrendsSnapshotService
from app.insights.snapshot.domain.ready import ReadyForDigest
from app.insights.snapshot.domain.week import latest_window_end, now_in_jst
from app.insights.snapshot.repository.snapshots import SnapshotRepository
from app.queue.brokers import broker_digest
from app.queue.schedule import CRON_WEEKLY_SNAPSHOT

logger = structlog.get_logger(__name__)


@broker_digest.task(
    task_name="generate_weekly_snapshot",
    timeout=600,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_WEEKLY_SNAPSHOT}],
)
async def generate_weekly_snapshot(ctx: Context = TaskiqDepends()) -> None:
    """rolling 7d window (JST 当日 0:00 を上限) の snapshot を生成する。"""
    session_factory = ctx.state.session_factory
    window_end = latest_window_end(now_in_jst())

    async with session_factory() as session:
        snapshot_repo = SnapshotRepository(session)
        ready = await ReadyForDigest.try_advance_from(
            window_end=window_end,
            force=False,
            snapshot_repo=snapshot_repo,
        )
    if ready is None:
        logger.info(
            "weekly_snapshot_task_skipped",
            window_end=window_end.isoformat(),
        )
        return

    service = WeeklyTrendsSnapshotService(session_factory)
    outcome = await service.execute(ready)
    logger.info(
        "weekly_snapshot_task_generated",
        window_end=outcome.window_end.isoformat(),
        source_analysis_count=outcome.source_analysis_count,
    )
