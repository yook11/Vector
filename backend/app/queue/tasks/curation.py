"""Stage 3 curation task。Ready 構築後に quota と Service 実行へ進む。"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.domain.ready import (
    CurationReadyBuildBlockedCode,
    CurationReadyBuildBlockedError,
    ReadyForCuration,
)
from app.analysis.curation.failure_handling import CurationFailureHandler
from app.analysis.curation.metrics import record_curation_processing_outcome
from app.analysis.curation.repository import CurationRepository
from app.analysis.curation.service import CurationService
from app.analysis.rate_limit import record_rate_limit_gate_skipped
from app.audit.domain.event import Stage
from app.audit.error_fields import exception_fqn
from app.audit.metrics import record_audit_dropped
from app.audit.ready_build import project_ready_build_failure
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

    with curation_stage_span(article_id=trigger.analyzable_article_id) as stage:
        async with session_factory() as session:
            try:
                ready = await ReadyForCuration.try_advance_from(
                    analyzable_article_id=trigger.analyzable_article_id,
                    repo=CurationRepository(session),
                )
            except CurationReadyBuildBlockedError as exc:
                # 冪等 skip (ALREADY_*) は勝者の行と冗長。監査に残さず log のみ。
                # 恒久的な突き返し/欠損 (CONTENT_TOO_LARGE / ARTICLE_MISSING) は残す。
                if not exc.code.is_idempotent_skip:
                    await CurationAuditRepository(session).append_ready_build_blocked(
                        target_article_id=trigger.analyzable_article_id,
                        exc=exc,
                    )
                    await session.commit()
                logger.info(
                    "curate_content_rejected",
                    analyzable_article_id=trigger.analyzable_article_id,
                    reason="ready_build_blocked",
                    code=exc.code.value,
                )
                # 内容を読んで拒否した CONTENT_TOO_LARGE だけ処理結果に数える
                # (ALREADY_* / ARTICLE_MISSING は冪等 skip / stale で分母外)。
                if exc.code is CurationReadyBuildBlockedCode.CONTENT_TOO_LARGE:
                    record_curation_processing_outcome("rejected")
                stage.set_result("skipped")
                return
            except Exception as exc:
                await _append_ready_build_failed_audit(
                    session_factory,
                    analyzable_article_id=trigger.analyzable_article_id,
                    exc=exc,
                )
                # audit は best-effort。drop されても分類 emit は止めない。DB 障害だけ
                # infra_error として分母外に逃がし、contract/想定外は failed に倒す。
                projection = project_ready_build_failure(
                    stage_prefix="curation", exc=exc
                )
                record_curation_processing_outcome(
                    "infra_error" if projection.failure_kind == "db_error" else "failed"
                )
                raise

        # precondition 未充足の stale trigger で AI quota を消費しない。
        if not await ctx.state.provider_rate_limit_gate.acquire(
            curator.rate_limit_policy
        ):
            record_rate_limit_gate_skipped(
                stage=Stage.CURATION, model=curator.model_name
            )
            logger.info(
                "curation_ai_rate_limit_gate_skipped",
                analyzable_article_id=ready.analyzable_article_id,
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
            # handler / hold が二次例外で落ちても元の業務例外を span に残す
            # (no-override で最初の業務例外を保持)。
            stage.record_failure(exc)
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
    analyzable_article_id: int,
    exc: Exception,
) -> None:
    """Ready 構築例外を best-effort で監査し、失敗時は構造ログへ退避する。"""
    try:
        async with session_factory() as audit_session:
            await CurationAuditRepository(audit_session).append_ready_build_failed(
                target_article_id=analyzable_article_id,
                exc=exc,
            )
            await audit_session.commit()
    except Exception as audit_exc:
        logger.exception(
            "curation_ready_build_failed_audit_dropped",
            analyzable_article_id=analyzable_article_id,
            business_error_class=exception_fqn(exc),
            audit_error_class=exception_fqn(audit_exc),
        )
        record_audit_dropped(CurationAuditRepository.STAGE)
