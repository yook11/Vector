"""Consumerの分類済み失敗に対して監査・通知・計測を実行する。"""

from __future__ import annotations

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.typing import FilteringBoundLogger

from app.analysis.ai_provider_exhaustion import record_ai_provider_exhausted
from app.analysis.embedding.consumer_failure_classification import (
    NoRetryEmbedding,
    RetryEmbedding,
)
from app.analysis.embedding.domain.ready import EmbeddingReadyBuildRejected
from app.analysis.embedding.metrics import record_embedding_processing_outcome
from app.audit.error_fields import exception_fqn
from app.audit.metrics import record_audit_dropped
from app.audit.stages.embedding import EmbeddingAuditRepository


class EmbeddingConsumerFailureHandler:
    """各後処理を独立して試み、Consumerが決めた失敗の扱いは変えない。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        *,
        failure: RetryEmbedding | NoRetryEmbedding,
        exc: Exception,
        analyzed_article_id: int,
        analyzable_article_id: int | None,
        provider: str,
        logger: FilteringBoundLogger,
    ) -> None:
        """通常の二次障害で元の処理例外を置き換えず、後処理だけを実行する。"""
        try:
            record_embedding_processing_outcome("failed")
        except Exception as metric_exc:
            self._record_secondary_failure(
                "processing_metric", analyzed_article_id, exc, metric_exc, logger=logger
            )

        try:
            async with self._session_factory() as session:
                await EmbeddingAuditRepository(session).append_failure(
                    analyzed_article_id=analyzed_article_id,
                    article_id=analyzable_article_id,
                    exc=exc,
                    failure_action=(
                        "retry" if isinstance(failure, RetryEmbedding) else "no_retry"
                    ),
                )
                await session.commit()
        except Exception as audit_exc:
            logger.warning(
                "embedding_consumer_failure_audit_dropped",
                operation="audit",
                analyzed_article_id=analyzed_article_id,
                business_error_class=exception_fqn(exc),
                exc_info=audit_exc,
            )
            try:
                record_audit_dropped(EmbeddingAuditRepository.STAGE)
            except Exception as metric_exc:
                self._record_secondary_failure(
                    "audit_dropped_metric",
                    analyzed_article_id,
                    exc,
                    metric_exc,
                    logger=logger,
                )

        try:
            record_ai_provider_exhausted(exc, provider=provider)
        except Exception as notification_exc:
            self._record_secondary_failure(
                "notification",
                analyzed_article_id,
                exc,
                notification_exc,
                logger=logger,
            )

    async def handle_ready_build_rejected(
        self,
        *,
        analyzed_article_id: int,
        rejected: EmbeddingReadyBuildRejected,
        logger: FilteringBoundLogger,
    ) -> None:
        """理由記録の通常障害で、確定した受信完了を再配信へ戻さない。"""
        try:
            async with self._session_factory() as session:
                await EmbeddingAuditRepository(session).append_ready_build_rejected(
                    analyzed_article_id=analyzed_article_id, rejected=rejected
                )
                await session.commit()
        except Exception as audit_exc:
            logger.warning(
                "embedding_ready_build_rejected_audit_dropped",
                analyzed_article_id=analyzed_article_id,
                operation="audit",
                rejection_code=rejected.reason.value,
                exc_info=audit_exc,
            )
            try:
                record_audit_dropped(EmbeddingAuditRepository.STAGE)
            except Exception:  # noqa: S110
                # 計測の障害でも受信完了を維持する。
                pass

    @staticmethod
    def _record_secondary_failure(
        operation: Literal["processing_metric", "audit_dropped_metric", "notification"],
        analyzed_article_id: int,
        original: Exception,
        secondary: Exception,
        *,
        logger: FilteringBoundLogger,
    ) -> None:
        """二次例外の診断を共通変換へ渡し、元の業務例外型を添える。"""
        logger.warning(
            "embedding_consumer_failure_handling_failed",
            operation=operation,
            analyzed_article_id=analyzed_article_id,
            business_error_class=exception_fqn(original),
            exc_info=secondary,
        )
