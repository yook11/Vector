"""``pipeline_events`` の retention TTL purge (red-team chain γ-4)。

90 日経過した監査行を **毎時 :25** に小バッチで削除する。`queue/tasks/backfill.py`
の back-fill タスクが「詰まり救済 (gatekeeper)」を担うのに対し、本タスクは
「データ寿命管理」という独立した責務を持つ。schedule literal は
``app.queue.schedule`` の SSoT に集約済 (時刻表 docstring で overlap 検証)。

スケジューリング設計:
- :25 は既存 cron (`*/15`, `5,20,35,50`, `*/10`) と最少 overlap な minute。
  html_dispatch (`* * * * *`) のみが常時走るが軽量タスク (< 1 秒)。
- 1 時間最大 5k 行削除 (BATCH_SIZE=1000 × MAX_BATCHES=5) で insert rate
  (steady state ~1k/hour) を上回る capacity を確保。spike を作らない。
- batch 間 sleep 0.1s で autovacuum / replication lag と co-exist。
- kill switch (`pipeline_events_retention_enabled`) と
  `pipeline_events_retention_max_batches` (settings) で運用調整可能。
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import text
from taskiq import Context, TaskiqDepends

from app.config import settings
from app.queue.brokers import broker_metadata
from app.queue.schedule import CRON_PIPELINE_EVENTS_PURGE

logger = structlog.get_logger(__name__)

RETENTION_DAYS = 90
BATCH_SIZE = 1_000
INTER_BATCH_SLEEP_SECONDS = 0.1


@broker_metadata.task(
    task_name="purge_pipeline_events",
    timeout=60,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_PIPELINE_EVENTS_PURGE}],
)
async def purge_pipeline_events(ctx: Context = TaskiqDepends()) -> None:
    """90 日経過した pipeline_events 行を batch 削除する。

    削除対象が空なら早期離脱。MAX_BATCHES に達したら次回起動に持ち越す。
    """
    if not settings.pipeline_events_retention_enabled:
        logger.info("pipeline_events_retention_disabled")
        return

    session_factory = ctx.state.session_factory
    max_batches = settings.pipeline_events_retention_max_batches
    total_deleted = 0
    batches_run = 0

    async with session_factory() as session:
        for _ in range(max_batches):
            # delete-by-id sub-select で long lock を回避する。
            # `:days` は PG INTERVAL に integer 算術で乗算する (asyncpg の型を
            # 損ねず CLAUDE.md NEVER §5 の SQL injection 経路も閉鎖)。
            result = await session.execute(
                text(
                    """
                    DELETE FROM pipeline_events
                    WHERE id IN (
                        SELECT id FROM pipeline_events
                        WHERE occurred_at
                              < NOW() - (INTERVAL '1 day' * :days)
                        ORDER BY id ASC
                        LIMIT :batch_size
                    )
                    """
                ),
                {"days": RETENTION_DAYS, "batch_size": BATCH_SIZE},
            )
            deleted = result.rowcount or 0
            await session.commit()
            if deleted == 0:
                break
            total_deleted += deleted
            batches_run += 1
            await asyncio.sleep(INTER_BATCH_SLEEP_SECONDS)

    logger.info(
        "pipeline_events_retention_purged",
        deleted=total_deleted,
        retention_days=RETENTION_DAYS,
        batches=batches_run,
        max_batches=max_batches,
    )
