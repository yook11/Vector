"""補完 task の cron 駆動 — open pending の claim 投入 + lease 切れの救出。

再投入は DB の ``ready_at`` を SSoT とした cron poller に統一する。worker
crash で ``status='running'`` のまま残った行は ``sweep_expired_leases`` が
``status='open'`` に戻すため永続スタックしない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from taskiq import Context, TaskiqDepends

from app.brokers import broker_metadata
from app.collection.article_completion.repository import ArticleCompletionRepository

logger = structlog.get_logger(__name__)

# extract_html_body.timeout=60s × 5 倍。task timeout 変更時は要連動。
_LEASE_MINUTES = 5
_DISPATCH_BATCH_LIMIT = 100
_HTML_FETCH_CRON = "* * * * *"  # 1 分間隔


@broker_metadata.task(
    task_name="dispatch_html_fetch_jobs",
    timeout=30,
    max_retries=1,
    retry_on_error=True,
    schedule=[{"cron": _HTML_FETCH_CRON}],
)
async def dispatch_html_fetch_jobs(ctx: Context = TaskiqDepends()) -> dict:
    """``ready_at <= NOW`` の open pending を claim し ``extract_html_body`` 投入。"""
    # tasks.py との循環 import 回避のため関数内 import
    from app.collection.tasks import extract_html_body

    session_factory = ctx.state.session_factory
    now = datetime.now(UTC)
    async with session_factory() as session:
        pending_ids = await ArticleCompletionRepository(session).claim_ready_batch(
            limit=_DISPATCH_BATCH_LIMIT,
            now=now,
            leased_until=now + timedelta(minutes=_LEASE_MINUTES),
        )
        await session.commit()

    for pending_id in pending_ids:
        await extract_html_body.kiq(pending_id)

    result = {"dispatched_count": len(pending_ids)}
    logger.info("dispatch_html_fetch_jobs_completed", **result)
    return result


@broker_metadata.task(
    task_name="sweep_expired_leases",
    timeout=30,
    max_retries=1,
    retry_on_error=True,
    schedule=[{"cron": _HTML_FETCH_CRON}],
)
async def sweep_expired_leases(ctx: Context = TaskiqDepends()) -> dict:
    """``status='running' AND leased_until <= NOW`` を ``open`` に戻す。"""
    session_factory = ctx.state.session_factory
    now = datetime.now(UTC)
    async with session_factory() as session:
        swept_count = await ArticleCompletionRepository(session).sweep_expired_leases(
            now=now
        )
        await session.commit()

    result = {"swept_count": swept_count}
    logger.info("sweep_expired_leases_completed", **result)
    return result
