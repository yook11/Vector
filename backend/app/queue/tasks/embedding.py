"""Stage 5 embedding task。Ready 構築後に quota と Service 実行へ進む。"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildBlockedError,
    ReadyForEmbedding,
)
from app.analysis.embedding.failure_handling import EmbeddingFailureHandler
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import EmbeddingService
from app.analysis.rate_limit import record_rate_limit_gate_skipped
from app.audit.error_fields import exception_fqn
from app.audit.stages.embedding import EmbeddingAuditRepository
from app.logfire.article_stage import embedding_stage_span
from app.queue.brokers import broker_embedding
from app.queue.helpers.stage_hold import set_embedding_hold
from app.queue.messages.embedding import EmbeddingTrigger
from app.queue.retry import is_last_attempt
from app.redis import get_redis

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
                ready = await ReadyForEmbedding.try_advance_from(
                    analyzed_article_id=trigger.analyzed_article_id,
                    embedding_repo=EmbeddingRepository(session),
                )
            except EmbeddingReadyBuildBlockedError as exc:
                await EmbeddingAuditRepository(session).append_ready_build_blocked(
                    analyzed_article_id=trigger.analyzed_article_id,
                    exc=exc,
                )
                await session.commit()
                logger.info(
                    "generate_embedding_rejected",
                    analyzed_article_id=trigger.analyzed_article_id,
                    reason="ready_build_blocked",
                    code=exc.code.value,
                )
                stage.set_result("skipped")
                return
            except Exception as exc:
                await _append_ready_build_failed_audit(
                    session_factory,
                    analyzed_article_id=trigger.analyzed_article_id,
                    exc=exc,
                )
                raise

        # analyzable_article_id は trigger に無く ready で判明する (late-binding)。
        stage.set_article_id(ready.analyzable_article_id)

        # precondition 未充足の stale trigger で AI quota を消費しない。
        gate = ctx.state.provider_rate_limit_gate
        if not await gate.acquire(embedder.rate_limit_policy):
            record_rate_limit_gate_skipped(stage="embedding", model=embedder.model_name)
            logger.info(
                "embedding_ai_rate_limit_gate_skipped",
                analyzed_article_id=ready.analyzed_article_id,
                analyzable_article_id=ready.analyzable_article_id,
                embedding_model=embedder.model_name,
            )
            stage.set_result("rate_limited")
            return

        svc = EmbeddingService(session_factory)
        handler = EmbeddingFailureHandler(session_factory)

        try:
            await svc.execute(ready, embedder)
        except Exception as exc:
            decision = await handler.handle(
                ready=ready,
                exc=exc,
                last_attempt=is_last_attempt(ctx),
            )
            if decision.stage_hold_reason is not None:
                await set_embedding_hold(get_redis(), reason=decision.stage_hold_reason)
            stage.set_result("failed")
            if decision.reraise:
                raise
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
