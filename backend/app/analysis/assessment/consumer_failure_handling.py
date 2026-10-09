"""Consumerの分類済み失敗に対して監査・通知・計測を実行する。"""

from __future__ import annotations

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.typing import FilteringBoundLogger

from app.analysis.ai_provider_exhaustion import record_ai_provider_exhausted
from app.analysis.assessment.consumer_failure_classification import (
    NoRetryAssessment,
    RetryAssessment,
)
from app.analysis.assessment.domain.ready import AssessmentReadyBuildRejected
from app.analysis.assessment.metrics import record_assessment_processing_outcome
from app.audit.error_fields import exception_fqn
from app.audit.metrics import record_audit_dropped
from app.audit.stages.assessment import AssessmentAuditRepository


class AssessmentConsumerFailureHandler:
    """各後処理を独立して試み、Consumerが決めた失敗の扱いは変えない。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        *,
        failure: RetryAssessment | NoRetryAssessment,
        exc: Exception,
        curation_id: int,
        analyzable_article_id: int | None,
        provider: str,
        logger: FilteringBoundLogger,
    ) -> None:
        """通常の二次障害で元の処理例外を置き換えず、後処理だけを実行する。"""
        try:
            record_assessment_processing_outcome("failed")
        except Exception as metric_exc:
            self._record_secondary_failure(
                "processing_metric", exc, metric_exc, logger=logger
            )

        try:
            async with self._session_factory() as session:
                await AssessmentAuditRepository(session).append_failure(
                    curation_id=curation_id,
                    article_id=analyzable_article_id,
                    exc=exc,
                    failure_action=(
                        "retry" if isinstance(failure, RetryAssessment) else "no_retry"
                    ),
                )
                await session.commit()
        except Exception as audit_exc:
            logger.warning(
                "assessment_consumer_failure_audit_dropped",
                operation="audit",
                business_error_class=exception_fqn(exc),
                exc_info=audit_exc,
            )
            try:
                record_audit_dropped(AssessmentAuditRepository.STAGE)
            except Exception as metric_exc:
                self._record_secondary_failure(
                    "audit_dropped_metric", exc, metric_exc, logger=logger
                )

        try:
            record_ai_provider_exhausted(exc, provider=provider)
        except Exception as notification_exc:
            self._record_secondary_failure(
                "notification", exc, notification_exc, logger=logger
            )

    async def handle_ready_build_rejected(
        self,
        *,
        curation_id: int,
        rejected: AssessmentReadyBuildRejected,
        logger: FilteringBoundLogger,
    ) -> None:
        """理由記録の通常障害で、確定した受信完了を再配信へ戻さない。"""
        try:
            async with self._session_factory() as session:
                await AssessmentAuditRepository(session).append_ready_build_rejected(
                    curation_id=curation_id, rejected=rejected
                )
                await session.commit()
        except Exception as audit_exc:
            logger.warning(
                "assessment_ready_build_rejected_audit_dropped",
                operation="audit",
                rejection_code=rejected.reason.value,
                exc_info=audit_exc,
            )
            try:
                record_audit_dropped(AssessmentAuditRepository.STAGE)
            except Exception:  # noqa: S110
                # 計測の障害でも受信完了を維持する。
                pass

    @staticmethod
    def _record_secondary_failure(
        operation: Literal["processing_metric", "audit_dropped_metric", "notification"],
        original: Exception,
        secondary: Exception,
        *,
        logger: FilteringBoundLogger,
    ) -> None:
        """二次例外の診断を共通変換へ渡し、元の業務例外型を添える。"""
        logger.warning(
            "assessment_consumer_failure_handling_failed",
            operation=operation,
            business_error_class=exception_fqn(original),
            exc_info=secondary,
        )
