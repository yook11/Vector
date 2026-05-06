"""Pattern H の cron 駆動部 — open pending の claim 投入 + lease 切れの救出。

PR2.5-B cutover で taskiq の retry 機構は完全に殺し、再投入は **DB の
``ready_at`` SSoT + cron poller** に統一する。これにより:

- ``extract_html_body.kiq(pending_id)`` の caller は本ファイルの
  ``dispatch_html_fetch_jobs`` のみ (静的 grep で他 caller がいないことを保証)
- worker crash で ``status='running'`` のまま残った行は ``sweep_expired_leases``
  が ``status='open'`` に戻すため永続スタック不能

両 task は ``broker_metadata`` (= scheduler 側 broker) に登録、消費される
``extract_html_body`` は ``broker_content``。cross-broker 投入は既存の
``dispatch_sources → ingest_source`` と同パターン。
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.brokers import broker_metadata
from app.collection.ingestion.pending_repository import PendingHtmlArticleRepository

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
    async with session_factory() as session:
        pending_ids = await PendingHtmlArticleRepository(session).claim_batch(
            limit=_DISPATCH_BATCH_LIMIT,
            lease_minutes=_LEASE_MINUTES,
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
    async with session_factory() as session:
        swept_count = await PendingHtmlArticleRepository(session).sweep_expired()
        await session.commit()

    result = {"swept_count": swept_count}
    logger.info("sweep_expired_leases_completed", **result)
    return result
