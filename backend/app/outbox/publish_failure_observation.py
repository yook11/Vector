"""配信停止の診断情報と、設定修復が必要な失敗のメトリクスを記録する。"""

import structlog

from app.audit.error_fields import exception_fqn
from app.cloudwatch.emf import emit_metric
from app.outbox.publish_errors import (
    PublishConfigurationError,
    PublishConfigurationReason,
    PublishError,
    PublishEventInvalidError,
    PublishServiceError,
    PublishServiceReason,
    PublishTransportError,
    PublishUnexpectedError,
)
from app.outbox.publish_failure_handler import (
    DeliveryStopped,
    DeliveryUpdateSkipped,
    RetryScheduled,
)
from app.outbox.repository import ClaimedOutboxEvent

logger = structlog.get_logger(__name__)
CONFIGURATION_FAILURE_METRIC = "outbox_publish_configuration_failure"


def requires_publish_configuration_fix(error: PublishError) -> bool:
    """修復が必要と合意した共通reasonだけを通知対象にする。"""
    if isinstance(error, PublishConfigurationError):
        return error.reason in (
            PublishConfigurationReason.MISSING_CREDENTIALS,
            PublishConfigurationReason.INCOMPLETE_CREDENTIALS,
            PublishConfigurationReason.MISSING_REGION,
        )
    if isinstance(error, PublishServiceError):
        return error.reason in (
            PublishServiceReason.AUTHENTICATION_FAILED,
            PublishServiceReason.ACCESS_DENIED,
            PublishServiceReason.DESTINATION_NOT_FOUND,
        )
    return False


def _record_output_failure(exc: Exception) -> None:
    """出力障害は型だけを記録し、その記録失敗を再試行しない。"""
    try:
        logger.warning(
            "outbox_failure_observation_failed", error_class=exception_fqn(exc)
        )
    except Exception:  # noqa: S110 — 出力失敗の記録を再帰させない。
        pass


def record_publish_failure(
    *,
    event: ClaimedOutboxEvent,
    error: PublishError,
    result: RetryScheduled | DeliveryStopped | DeliveryUpdateSkipped,
) -> None:
    """停止確定後に呼び、観測の通常失敗を配信処理へ戻さない。"""
    if not isinstance(result, DeliveryStopped):
        return
    requires_fix = requires_publish_configuration_fix(error)
    try:
        fields: dict[str, object] = {
            "event_id": str(event.event_id),
            "attempt_count": event.attempt_count,
            "stop_reason": result.reason.value,
            "error_code": error.CODE,
            "requires_configuration_fix": requires_fix,
        }
        if isinstance(
            error,
            PublishConfigurationError | PublishServiceError | PublishEventInvalidError,
        ):
            fields["error_reason"] = error.reason.value
        elif isinstance(error, PublishTransportError):
            fields["transport_kind"] = error.failure.kind.value
        elif isinstance(error, PublishUnexpectedError):
            fields.update(
                error_reason=error.reason,
                phase=error.phase.value,
                original_exception_type=error.original_exception_type,
                classification_exception_type=error.classification_exception_type,
            )
        logger.info("outbox_delivery_stopped", **fields)
    except Exception as exc:
        _record_output_failure(exc)
    if requires_fix:
        try:
            emit_metric(
                CONFIGURATION_FAILURE_METRIC,
                dimensions={},
                value=1,
                unit="Count",
            )
        except Exception as exc:
            _record_output_failure(exc)
