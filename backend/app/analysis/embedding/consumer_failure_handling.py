"""Consumerの分類済み失敗に対して監査・通知・計測を実行する。"""

from __future__ import annotations

from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_exhaustion import record_ai_provider_exhausted
from app.analysis.embedding.consumer_failure_classification import (
    EmbeddingFailureClassification,
)
from app.analysis.embedding.domain.ready import EmbeddingReadyBuildRejected
from app.analysis.embedding.metrics import record_embedding_processing_outcome
from app.audit.error_fields import exception_fqn
from app.audit.metrics import record_audit_dropped
from app.audit.stages.embedding import EmbeddingAuditRepository

logger = structlog.get_logger(__name__)


class EmbeddingConsumerFailureHandler:
    """各後処理を独立して試み、元の失敗の伝播は呼び出し元に委ねる。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        *,
        failure: EmbeddingFailureClassification,
        exc: Exception,
        analyzed_article_id: int,
        analyzable_article_id: int | None,
        provider: str,
    ) -> None:
        """通常の二次障害で元の処理例外を置き換えず、後処理だけを実行する。"""
        try:
            record_embedding_processing_outcome(failure.outcome)
        except Exception as metric_exc:
            self._record_secondary_failure(
                "processing_metric", analyzed_article_id, exc, metric_exc
            )

        try:
            async with self._session_factory() as session:
                await EmbeddingAuditRepository(session).append_classified_failure(
                    analyzed_article_id=analyzed_article_id,
                    article_id=analyzable_article_id,
                    exc=exc,
                    projection=failure.audit,
                )
                await session.commit()
        except Exception as audit_exc:
            try:
                logger.warning(
                    "embedding_consumer_failure_audit_dropped",
                    operation="audit",
                    analyzed_article_id=analyzed_article_id,
                    business_error_class=exception_fqn(exc),
                    secondary_error_class=exception_fqn(audit_exc),
                )
            except Exception:  # noqa: S110
                # ログの障害でdrop計測と通知を止めない。
                pass
            try:
                record_audit_dropped(EmbeddingAuditRepository.STAGE)
            except Exception as metric_exc:
                self._record_secondary_failure(
                    "audit_dropped_metric", analyzed_article_id, exc, metric_exc
                )

        if failure.provider_exhaustion is not None:
            try:
                record_ai_provider_exhausted(
                    failure.provider_exhaustion, provider=provider
                )
            except Exception as notification_exc:
                self._record_secondary_failure(
                    "notification", analyzed_article_id, exc, notification_exc
                )

    async def handle_ready_build_rejected(
        self, *, analyzed_article_id: int, rejected: EmbeddingReadyBuildRejected
    ) -> None:
        """理由記録の通常障害で、確定した受信完了を再配信へ戻さない。"""
        try:
            async with self._session_factory() as session:
                await EmbeddingAuditRepository(session).append_ready_build_rejected(
                    analyzed_article_id=analyzed_article_id, rejected=rejected
                )
                await session.commit()
        except Exception as audit_exc:
            try:
                logger.warning(
                    "embedding_ready_build_rejected_audit_dropped",
                    analyzed_article_id=analyzed_article_id,
                    reason=rejected.reason.value,
                    audit_error_class=exception_fqn(audit_exc),
                )
            except Exception:  # noqa: S110
                # 診断ログの障害でも受信完了を維持する。
                pass
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
    ) -> None:
        """二次障害は例外本文やトレースバックを出さずに記録する。"""
        try:
            logger.warning(
                "embedding_consumer_failure_handling_failed",
                operation=operation,
                analyzed_article_id=analyzed_article_id,
                business_error_class=exception_fqn(original),
                secondary_error_class=exception_fqn(secondary),
            )
        except Exception:  # noqa: S110
            # ログ出力自体の障害でも元の例外と残りの後処理を優先する。
            pass
