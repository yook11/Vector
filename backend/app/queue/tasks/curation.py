"""Stage 3 curation task。Ready 構築後に quota と Service 実行へ進む。"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.domain.ready import (
    CurationReadyBuildBlockedError,
    ReadyForCuration,
)
from app.analysis.curation.failure_handling import CurationFailureHandler
from app.analysis.curation.repository import CurationRepository
from app.analysis.curation.service import CurationService
from app.analysis.rate_limit import record_rate_limit_gate_skipped
from app.audit.stages.curation import CurationAuditRepository
from app.logfire.article_stage import curation_stage_span
from app.queue.brokers import broker_analysis
from app.queue.helpers.stage_hold import set_curation_hold
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.messages.curation import CurationTrigger
from app.queue.retry import is_last_attempt
from app.queue.tasks.assessment import assess_content
from app.redis import get_redis

logger = structlog.get_logger(__name__)


@broker_analysis.task(
    task_name="curate_content",
    timeout=180,
    max_retries=1,
    retry_on_error=True,
)
async def curate_content(
    trigger: CurationTrigger,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一記事を curation し、signal 成功時だけ assessment に chain する。"""
    session_factory = ctx.state.session_factory
    curator: BaseCurator = ctx.state.curator

    with curation_stage_span(article_id=trigger.article_id) as stage:
        async with session_factory() as session:
            try:
                ready = await ReadyForCuration.try_advance_from(
                    article_id=trigger.article_id,
                    repo=CurationRepository(session),
                )
            except CurationReadyBuildBlockedError as exc:
                await CurationAuditRepository(session).append_ready_build_blocked(
                    target_article_id=trigger.article_id,
                    exc=exc,
                )
                await session.commit()
                logger.info(
                    "curate_content_rejected",
                    article_id=trigger.article_id,
                    reason="ready_build_blocked",
                    code=exc.code.value,
                )
                stage.set_result("skipped")
                return
            except Exception as exc:
                await _append_ready_build_failed_audit(
                    session_factory,
                    article_id=trigger.article_id,
                    exc=exc,
                )
                raise

        # precondition 未充足の stale trigger で AI quota を消費しない。
        if not await ctx.state.provider_rate_limit_gate.acquire(
            curator.rate_limit_policy
        ):
            record_rate_limit_gate_skipped(stage="curation", model=curator.model_name)
            logger.info(
                "curation_ai_rate_limit_gate_skipped",
                article_id=ready.article_id,
                ai_model=curator.model_name,
                prompt_version=curator.prompt_version,
            )
            stage.set_result("rate_limited")
            return

        svc = CurationService(session_factory)
        handler = CurationFailureHandler(session_factory)

        try:
            result = await svc.execute(ready, curator)
        except Exception as exc:
            decision = await handler.handle(
                ready=ready,
                exc=exc,
                curator=curator,
                last_attempt=is_last_attempt(ctx),
            )
            if decision.stage_hold_reason is not None:
                await set_curation_hold(get_redis(), reason=decision.stage_hold_reason)
            stage.set_result("failed")
            if decision.reraise:
                raise
            return

        if result is not None:
            await assess_content.kiq(AssessmentTrigger(curation_id=result))
            stage.mark_next_task_enqueued()


async def _append_ready_build_failed_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    article_id: int,
    exc: Exception,
) -> None:
    """Ready 構築例外を best-effort で監査し、失敗時は構造ログへ退避する。"""
    try:
        async with session_factory() as audit_session:
            await CurationAuditRepository(audit_session).append_ready_build_failed(
                target_article_id=article_id,
                exc=exc,
            )
            await audit_session.commit()
    except Exception as audit_exc:
        logger.exception(
            "curation_ready_build_failed_audit_dropped",
            article_id=article_id,
            business_error_class=_fqn(exc),
            audit_error_class=_fqn(audit_exc),
        )


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
