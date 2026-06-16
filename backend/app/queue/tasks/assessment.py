"""Stage 4 assessment task。Ready 構築後に quota と Service 実行へ進む。"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildBlockedError,
    ReadyForAssessment,
)
from app.analysis.assessment.failure_handling import AssessmentFailureHandler
from app.analysis.assessment.metrics import record_assessment_processing_outcome
from app.analysis.assessment.repository import AssessmentRepository
from app.analysis.assessment.service import AssessmentService
from app.analysis.rate_limit import record_rate_limit_gate_skipped
from app.audit.error_fields import exception_fqn
from app.audit.ready_build import project_ready_build_failure
from app.audit.stages.assessment import AssessmentAuditRepository
from app.logfire.article_stage import assessment_stage_span
from app.queue.brokers import broker_analysis
from app.queue.helpers.stage_hold import set_assessment_hold
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.messages.embedding import EmbeddingTrigger
from app.queue.retry import is_last_attempt
from app.queue.tasks.embedding import generate_embedding
from app.redis import get_redis

logger = structlog.get_logger(__name__)


@broker_analysis.task(
    task_name="assess_content",
    timeout=180,
    max_retries=2,
    retry_on_error=True,
)
async def assess_content(
    trigger: AssessmentTrigger,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一 curation を assessment し、in-scope 成功時だけ embedding に chain する。"""
    session_factory = ctx.state.session_factory
    assessor: BaseAssessor = ctx.state.assessor

    with assessment_stage_span(curation_id=trigger.curation_id) as stage:
        async with session_factory() as session:
            try:
                ready = await ReadyForAssessment.try_advance_from(
                    curation_id=trigger.curation_id,
                    repo=AssessmentRepository(session),
                )
            except AssessmentReadyBuildBlockedError as exc:
                await AssessmentAuditRepository(session).append_ready_build_blocked(
                    curation_id=trigger.curation_id,
                    exc=exc,
                )
                await session.commit()
                logger.info(
                    "assess_content_rejected",
                    curation_id=trigger.curation_id,
                    reason="ready_build_blocked",
                    code=exc.code.value,
                )
                stage.set_result("skipped")
                return
            except Exception as exc:
                await _append_ready_build_failed_audit(
                    session_factory,
                    curation_id=trigger.curation_id,
                    exc=exc,
                )
                # audit は best-effort。drop されても分類 emit は止めない。DB 障害だけ
                # infra_error として分母外に逃がし、contract/想定外は failed に倒す。
                projection = project_ready_build_failure(
                    stage_prefix="assessment", exc=exc
                )
                record_assessment_processing_outcome(
                    "infra_error" if projection.failure_kind == "db_error" else "failed"
                )
                raise

        # analyzable_article_id は trigger に無く ready で判明する (late-binding)。
        stage.set_article_id(ready.analyzable_article_id)

        # precondition 未充足の stale trigger で AI quota を消費しない。
        if not await ctx.state.provider_rate_limit_gate.acquire(
            assessor.rate_limit_policy
        ):
            record_rate_limit_gate_skipped(
                stage="assessment", model=assessor.model_name
            )
            logger.info(
                "assessment_ai_rate_limit_gate_skipped",
                curation_id=ready.curation_id,
                analyzable_article_id=ready.analyzable_article_id,
                ai_model=assessor.model_name,
                prompt_version=assessor.prompt_version,
            )
            stage.set_result("rate_limited")
            return

        svc = AssessmentService(session_factory)
        handler = AssessmentFailureHandler(session_factory)

        try:
            result = await svc.execute(ready, assessor)
        except Exception as exc:
            decision = await handler.handle(
                ready=ready,
                exc=exc,
                last_attempt=is_last_attempt(ctx),
            )
            if decision.stage_hold_reason is not None:
                await set_assessment_hold(
                    get_redis(), reason=decision.stage_hold_reason
                )
            stage.set_result("failed")
            if decision.reraise:
                raise
            return

        if result is not None:
            await generate_embedding.kiq(EmbeddingTrigger(analyzed_article_id=result))
            stage.mark_next_task_enqueued()


async def _append_ready_build_failed_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    curation_id: int,
    exc: Exception,
) -> None:
    """Ready 構築例外を best-effort で監査し、失敗時は構造ログへ退避する。"""
    try:
        async with session_factory() as audit_session:
            await AssessmentAuditRepository(audit_session).append_ready_build_failed(
                curation_id=curation_id,
                exc=exc,
            )
            await audit_session.commit()
    except Exception as audit_exc:
        logger.exception(
            "assessment_ready_build_failed_audit_dropped",
            curation_id=curation_id,
            business_error_class=exception_fqn(exc),
            audit_error_class=exception_fqn(audit_exc),
        )
