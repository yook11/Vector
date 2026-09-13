"""Stage 5 embedding task。Ready 構築後に Service 実行へ進む。"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildRejected,
    ReadyForEmbedding,
)
from app.analysis.embedding.failure_handling import EmbeddingFailureHandler
from app.analysis.embedding.metrics import record_embedding_processing_outcome
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import EmbeddingService
from app.analysis.embedding.task_errors import to_embedding_task_error
from app.audit.domain.event import Stage
from app.audit.error_fields import exception_fqn
from app.audit.metrics import record_audit_dropped
from app.audit.ready_build import project_ready_build_failure
from app.audit.stages.embedding import EmbeddingAuditRepository
from app.logfire.article_stage import embedding_stage_span
from app.queue.brokers import broker_embedding
from app.queue.helpers.stage_hold import set_stage_hold
from app.queue.messages.embedding import EmbeddingTrigger
from app.queue.retry import is_last_attempt

logger = structlog.get_logger(__name__)


@broker_embedding.task(
    task_name="generate_embedding",
    timeout=60,
    max_retries=2,
    retry_on_error=True,
)
async def generate_embedding(
    trigger: EmbeddingTrigger,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一 analyzed article に対してベクトル埋め込みを生成する。"""
    session_factory = ctx.state.session_factory
    embedder: BaseEmbedder = ctx.state.embedder

    with embedding_stage_span(analyzed_article_id=trigger.analyzed_article_id) as stage:
        async with session_factory() as session:
            try:
                ready_build = await ReadyForEmbedding.try_advance_from(
                    analyzed_article_id=trigger.analyzed_article_id,
                    embedding_repo=EmbeddingRepository(session),
                    analyzable_hint=trigger.analyzable_article_id,
                )
            except Exception as exc:
                await _append_ready_build_failed_audit(
                    session_factory,
                    analyzed_article_id=trigger.analyzed_article_id,
                    exc=exc,
                )
                # audit は best-effort。drop されても分類 emit は止めない。DB 障害だけ
                # infra_error として分母外に逃がし、contract/想定外は failed に倒す。
                projection = project_ready_build_failure(
                    stage_prefix="embedding", exc=exc
                )
                record_embedding_processing_outcome(
                    "infra_error" if projection.failure_kind == "db_error" else "failed"
                )
                raise

            if isinstance(ready_build, EmbeddingReadyBuildRejected):
                if not ready_build.reason.is_idempotent_skip:
                    await EmbeddingAuditRepository(session).append_ready_build_rejected(
                        analyzed_article_id=trigger.analyzed_article_id,
                        rejected=ready_build,
                    )
                    await session.commit()
                logger.info(
                    "generate_embedding_rejected",
                    analyzed_article_id=trigger.analyzed_article_id,
                    reason="ready_build_rejected",
                    code=ready_build.reason.value,
                )
                stage.set_result("skipped")
                return

            ready, analyzable_article_id = ready_build

        stage.set_article_id(analyzable_article_id)

        svc = EmbeddingService(session_factory)
        handler = EmbeddingFailureHandler(session_factory)

        try:
            await svc.execute(
                ready, embedder, analyzable_article_id=analyzable_article_id
            )
        except Exception as exc:
            task_exc = to_embedding_task_error(exc)
            # handler / hold が二次例外で落ちても元の業務例外を span に残す
            # (no-override で最初の業務例外を保持)。
            stage.record_failure(task_exc)
            decision = await handler.handle(
                ready=ready,
                exc=task_exc,
                last_attempt=is_last_attempt(ctx),
                analyzable_article_id=analyzable_article_id,
                provider=embedder.provider,
            )
            if decision.stage_hold_reason is not None:
                await set_stage_hold(
                    ctx.state.pipeline_control_redis,
                    Stage.EMBEDDING,
                    reason=decision.stage_hold_reason,
                )
            stage.set_result("failed")
            if decision.reraise:
                if task_exc is exc:
                    raise
                raise task_exc from exc
            return

        # Stage 5 はパイプライン終端、chain firing なし。


async def _append_ready_build_failed_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    analyzed_article_id: int,
    exc: Exception,
) -> None:
    """Ready 構築例外を best-effort で監査し、失敗時は構造ログへ退避する。"""
    try:
        async with session_factory() as audit_session:
            await EmbeddingAuditRepository(audit_session).append_ready_build_failed(
                analyzed_article_id=analyzed_article_id,
                exc=exc,
            )
            await audit_session.commit()
    except Exception as audit_exc:
        logger.exception(
            "embedding_ready_build_failed_audit_dropped",
            analyzed_article_id=analyzed_article_id,
            business_error_class=exception_fqn(exc),
            audit_error_class=exception_fqn(audit_exc),
        )
        record_audit_dropped(EmbeddingAuditRepository.STAGE)
