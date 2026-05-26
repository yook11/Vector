"""Article Completion (Stage 2) taskiq タスク群。

3 task をステージ単位で集約する:
  - ``dispatch_html_fetch_jobs`` (cron, 1 分間隔): ``ready_at <= NOW`` の open
    pending を claim し ``scrape_html_body`` に kiq
  - ``sweep_expired_leases`` (cron, 1 分間隔): worker crash で ``status='running'``
    のまま残った行を ``open`` に戻す
  - ``scrape_html_body`` (event-driven): HTML 取得 + 本文抽出 + Article 永続化を
    ``ArticleCompletionService`` に委譲、成功時は ``curate_content`` chain

再投入は DB の ``ready_at`` を SSoT とした cron poller に統一する。worker
crash で ``status='running'`` のまま残った行は ``sweep_expired_leases`` が
``status='open'`` に戻すため永続スタックしない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from taskiq import Context, TaskiqDepends

from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.article_completion.service import ArticleCompletionService
from app.queue.brokers import broker_content, broker_metadata
from app.queue.messages.curation import CurationTrigger
from app.queue.schedule import CRON_HTML_FETCH
from app.queue.tasks.curation import curate_content

logger = structlog.get_logger(__name__)

# scrape_html_body.timeout=60s × 5 倍。task timeout 変更時は要連動。
_LEASE_MINUTES = 5
_DISPATCH_BATCH_LIMIT = 100


@broker_metadata.task(
    task_name="dispatch_html_fetch_jobs",
    timeout=30,
    max_retries=1,
    retry_on_error=True,
    schedule=[{"cron": CRON_HTML_FETCH}],
)
async def dispatch_html_fetch_jobs(ctx: Context = TaskiqDepends()) -> dict:
    """``ready_at <= NOW`` の open pending を claim し ``scrape_html_body`` 投入。"""
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
        await scrape_html_body.kiq(pending_id)

    result = {"dispatched_count": len(pending_ids)}
    logger.info("dispatch_html_fetch_jobs_completed", **result)
    return result


@broker_metadata.task(
    task_name="sweep_expired_leases",
    timeout=30,
    max_retries=1,
    retry_on_error=True,
    schedule=[{"cron": CRON_HTML_FETCH}],
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


@broker_content.task(
    task_name="scrape_html_body",
    timeout=60,
    max_retries=0,
    retry_on_error=False,
)
async def scrape_html_body(
    pending_id: int,
    ctx: Context = TaskiqDepends(),
) -> dict | None:
    """HTML 取得 + 本文抽出 + Article 永続化を Service に委譲。

    taskiq retry を持たず cron poller (``dispatch_html_fetch_jobs``) のみで
    再投入する。task は ``ReadyForArticleCompletion.try_advance_from`` で Ready を
    構築し (構築不能なら skip log + ``None``)、Service に渡す。article_id が返れば
    ``curate_content`` に enqueue、``None`` は何もしない (DB 状態 + audit は
    Service / failure handler 内で完結済)。
    """
    session_factory = ctx.state.session_factory
    async with session_factory() as session:
        ready = await ReadyForArticleCompletion.try_advance_from(
            pending_id=pending_id,
            repo=ArticleCompletionRepository(session),
        )
    if ready is None:
        logger.info(
            "scrape_html_body_skipped",
            pending_id=pending_id,
            reason="precondition_not_met",
        )
        return None

    article_id = await ArticleCompletionService(session_factory).execute(ready)

    if article_id is None:
        return None
    await curate_content.kiq(CurationTrigger(article_id=article_id))
    return {
        "pending_id": pending_id,
        "article_id": article_id,
        "status": "success",
    }
