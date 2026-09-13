"""Stage 3 curation task。Ready 構築後に quota と Service 実行へ進む。"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.domain.ready import (
    CurationReadyBuildRejected,
    CurationReadyBuildRejectionReason,
    ReadyForCuration,
)
from app.analysis.curation.failure_handling import CurationFailureHandler
from app.analysis.curation.metrics import record_curation_processing_outcome
from app.analysis.curation.repository import CurationRepository
from app.analysis.curation.service import CurationCompletionKind, CurationService
from app.analysis.curation.task_errors import to_curation_task_error
from app.audit.domain.event import Stage
from app.audit.error_fields import exception_fqn
from app.audit.metrics import record_audit_dropped
from app.audit.ready_build import project_ready_build_failure
from app.audit.stages.curation import CurationAuditRepository
from app.logfire.article_stage import curation_stage_span
from app.queue.brokers import broker_analysis
from app.queue.helpers.stage_hold import set_stage_hold
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.messages.curation import CurationTrigger
from app.queue.retry import is_last_attempt
from app.queue.tasks.assessment import assess_content

logger = structlog.get_logger(__name__)


@broker_analysis.task(
    task_name="curate_content",
    queue_name="pipeline:curation",
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
                ready_build = await ReadyForCuration.try_advance_from(
                    analyzable_article_id=trigger.analyzable_article_id,
                    repo=CurationRepository(session),
                )
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

            if isinstance(ready_build, CurationReadyBuildRejected):
                # 処理済みの拒否は勝者の監査と重複するためログだけに残す。
                if not ready_build.reason.is_idempotent_skip:
                    await CurationAuditRepository(session).append_ready_build_rejected(
                        target_article_id=trigger.analyzable_article_id,
                        rejected=ready_build,
                    )
                    await session.commit()
                logger.info(
                    "curate_content_rejected",
                    analyzable_article_id=trigger.analyzable_article_id,
                    reason="ready_build_rejected",
                    code=ready_build.reason.value,
                )
                # 内容を読んで拒否した CONTENT_TOO_LARGE だけ処理結果に数える
                # (ALREADY_* / ARTICLE_MISSING は冪等 skip / stale で分母外)。
                if (
                    ready_build.reason
                    is CurationReadyBuildRejectionReason.CONTENT_TOO_LARGE
                ):
                    record_curation_processing_outcome("rejected")
                stage.set_result("skipped")
                return

            ready = ready_build

        svc = CurationService(session_factory)
        handler = CurationFailureHandler(session_factory)

        try:
            result = await svc.execute(ready, curator)
        except Exception as exc:
            # handler / hold が二次例外で落ちても元の業務例外を span に残す
            # (no-override で最初の業務例外を保持)。
            task_exc = to_curation_task_error(exc)
            stage.record_failure(task_exc)
            decision = await handler.handle(
                ready=ready,
                exc=task_exc,
                curator=curator,
                last_attempt=is_last_attempt(ctx),
            )
            if decision.stage_hold_reason is not None:
                await set_stage_hold(
                    ctx.state.pipeline_control_redis,
                    Stage.CURATION,
                    reason=decision.stage_hold_reason,
                )
            stage.set_result("failed")
            if decision.reraise:
                if task_exc is not exc:
                    raise task_exc from exc
                raise
            return

        if result.kind is CurationCompletionKind.SIGNAL:
            await assess_content.kiq(AssessmentTrigger(curation_id=result.curation_id))
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
