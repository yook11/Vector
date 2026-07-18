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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.audit.domain.event import Stage
from app.audit.error_fields import exception_fqn
from app.audit.metrics import record_audit_dropped
from app.audit.stages.completion import ArticleCompletionAuditRepository
from app.collection.article_completion.metrics import (
    record_completion_lease_swept,
    record_completion_processing_outcome,
)
from app.collection.article_completion.ready import (
    ArticleCompletionReadyBuildError,
    ReadyForArticleCompletion,
)
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.article_completion.service import ArticleCompletionService
from app.logfire.stage_span import pipeline_stage_span
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
        incomplete_article_ids = await ArticleCompletionRepository(
            session
        ).claim_ready_batch(
            limit=_DISPATCH_BATCH_LIMIT,
            now=now,
            leased_until=now + timedelta(minutes=_LEASE_MINUTES),
        )
        await session.commit()

    for incomplete_article_id in incomplete_article_ids:
        await scrape_html_body.kiq(incomplete_article_id)

    result = {"dispatched_count": len(incomplete_article_ids)}
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

    record_completion_lease_swept(swept_count)
    result = {"swept_count": swept_count}
    logger.info("sweep_expired_leases_completed", **result)
    return result


@broker_content.task(
    task_name="scrape_html_body",
    queue_name="pipeline:completion",
    timeout=60,
    max_retries=0,
    retry_on_error=False,
)
async def scrape_html_body(
    incomplete_article_id: int,
    ctx: Context = TaskiqDepends(),
) -> dict | None:
    """HTML 取得 + 本文抽出 + Article 永続化を Service に委譲。

    taskiq retry を持たず cron poller (``dispatch_html_fetch_jobs``) のみで
    再投入する。task は ``ReadyForArticleCompletion.try_advance_from`` で Ready を
    構築し (対象消滅 / 別 worker 完了済み等の benign な skip は log のみで ``None``)、
    Service に渡す。
    analyzable_article_id が返れば ``curate_content`` に enqueue、``None`` は何もしない
    (DB 状態 + audit は
    Service / failure handler 内で完結済)。
    """
    with pipeline_stage_span(
        Stage.COMPLETION, op="scrape_html_body", article_id=incomplete_article_id
    ):
        session_factory = ctx.state.session_factory
        async with session_factory() as session:
            try:
                ready = await ReadyForArticleCompletion.try_advance_from(
                    incomplete_article_id=incomplete_article_id,
                    repo=ArticleCompletionRepository(session),
                )
            except ArticleCompletionReadyBuildError as exc:
                # 対象消滅 / 別 worker 完了済み等の benign な冪等 skip。監査には焼かず
                # log で観測し、processing_outcome counter も汚さない。VO 構築失敗は
                # この型を継承せず except Exception 側で failed として焼かれる。
                logger.info(
                    "scrape_html_body_skipped",
                    incomplete_article_id=incomplete_article_id,
                    reason="ready_build_error",
                    outcome_code=exc.CODE,
                )
                return None
            except Exception as exc:
                await _append_ready_build_error_audit(
                    session_factory,
                    incomplete_article_id=incomplete_article_id,
                    exc=exc,
                )
                # ready-build の DB 障害は infra、VO error 等は failed。
                record_completion_processing_outcome(
                    "infra_error" if isinstance(exc, SQLAlchemyError) else "failed"
                )
                raise

        analyzable_article_id = await ArticleCompletionService(session_factory).execute(
            ready
        )

        if analyzable_article_id is None:
            return None
        await curate_content.kiq(
            CurationTrigger(analyzable_article_id=analyzable_article_id)
        )
        return {
            "incomplete_article_id": incomplete_article_id,
            "analyzable_article_id": analyzable_article_id,
            "status": "success",
        }


async def _append_ready_build_error_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    incomplete_article_id: int,
    exc: Exception,
) -> None:
    """Ready 構築例外を best-effort で監査し、失敗時は構造ログへ退避する。"""
    try:
        async with session_factory() as audit_session:
            facts = None
            try:
                facts = await ArticleCompletionRepository(
                    audit_session
                ).load_ready_build_facts(incomplete_article_id)
            except Exception as context_exc:
                await audit_session.rollback()
                logger.warning(
                    "completion_ready_build_context_load_failed",
                    incomplete_article_id=incomplete_article_id,
                    context_error_class=exception_fqn(context_exc),
                )

            await ArticleCompletionAuditRepository(
                audit_session
            ).append_ready_build_error(
                incomplete_article_id=incomplete_article_id,
                exc=exc,
                facts=facts,
            )
            await audit_session.commit()
    except Exception as audit_exc:
        logger.exception(
            "completion_ready_build_error_audit_dropped",
            incomplete_article_id=incomplete_article_id,
            business_error_class=exception_fqn(exc),
            audit_error_class=exception_fqn(audit_exc),
        )
        record_audit_dropped(ArticleCompletionAuditRepository.STAGE)
