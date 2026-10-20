"""Consumerの分類済み失敗に対して通知・計測を実行する。"""

from __future__ import annotations

from typing import Literal

from structlog.typing import FilteringBoundLogger

from app.analysis.ai_provider_exhaustion import record_ai_provider_exhausted
from app.analysis.assessment.metrics import record_assessment_processing_outcome
from app.audit.error_fields import exception_fqn


class AssessmentConsumerFailureHandler:
    """各後処理を独立して試み、Consumerが決めた失敗の扱いは変えない。"""

    async def handle(
        self,
        *,
        exc: Exception,
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
            record_ai_provider_exhausted(exc, provider=provider)
        except Exception as notification_exc:
            self._record_secondary_failure(
                "notification", exc, notification_exc, logger=logger
            )

    @staticmethod
    def _record_secondary_failure(
        operation: Literal["processing_metric", "notification"],
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
