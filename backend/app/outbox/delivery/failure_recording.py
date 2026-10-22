"""停止確定後に、診断ログと必要なメトリクスを書く。"""

import structlog

from app.analysis.assessment.events import AssessedEventInvalidError
from app.analysis.curation.events import CuratedEventInvalidError
from app.audit.error_fields import exception_fqn
from app.aws.sqs.errors import (
    MessageSendConfigurationError,
    MessageSendConfigurationReason,
    MessageSendCredentialsError,
    MessageSendTransportError,
    MessageSendUnexpectedError,
    SqsServiceError,
    SqsServiceErrorReason,
)
from app.aws.sqs.outgoing_message import MessageBodyMismatchError
from app.cloudwatch.emf import emit_metric
from app.collection.article_acquisition.events import IncompleteArticleEventInvalidError
from app.collection.events import AnalyzableEventInvalidError
from app.outbox.delivery.repository import LeasedOutboxEvent
from app.outbox.delivery.retry_policy import NonRetryableReason
from app.outbox.publishing.contracts import PublishFailure

logger = structlog.get_logger(__name__)
CONFIGURATION_FAILURE_METRIC = "outbox_publish_configuration_failure"
_BODY_CHECKSUM_MISMATCH_MESSAGE = (
    "送信した本文と、送信先が受け取った本文のチェックサムが一致しません。"
    "送信先には受付済みの可能性があるため、このイベントの自動再試行を停止しました。"
)


def requires_publish_configuration_fix(error: PublishFailure) -> bool:
    """設定修復が必要な停止だけ、メトリクスも書く。"""
    if isinstance(error, MessageSendConfigurationError):
        return error.reason in (
            MessageSendConfigurationReason.MISSING_CREDENTIALS,
            MessageSendConfigurationReason.INCOMPLETE_CREDENTIALS,
            MessageSendConfigurationReason.MISSING_REGION,
        )
    if isinstance(error, SqsServiceError):
        return error.reason in (
            SqsServiceErrorReason.AUTHENTICATION_FAILED,
            SqsServiceErrorReason.ACCESS_DENIED,
            SqsServiceErrorReason.DESTINATION_NOT_FOUND,
        )
    return False


def _record_output_failure(exc: Exception) -> None:
    """出力障害は型だけを記録し、その記録失敗を再試行しない。"""
    try:
        logger.warning(
            "outbox_failure_recording_failed", error_class=exception_fqn(exc)
        )
    except Exception:  # noqa: S110 — 出力失敗の記録を再帰させない。
        pass


def record_publish_failure(
    *,
    leased_event: LeasedOutboxEvent,
    error: PublishFailure,
    stop_reason: NonRetryableReason,
) -> None:
    """停止確定後に呼び、記録の通常失敗を配信処理へ戻さない。"""
    requires_fix = requires_publish_configuration_fix(error)
    try:
        fields: dict[str, object] = {
            "event_id": str(leased_event.envelope.event_id),
            "attempt_count": leased_event.attempt_count,
            "stop_reason": stop_reason.value,
            "error_class": exception_fqn(error),
            "requires_configuration_fix": requires_fix,
        }
        if isinstance(error, MessageSendConfigurationError | SqsServiceError):
            fields["error_reason"] = error.reason.value
        elif isinstance(
            error,
            AnalyzableEventInvalidError
            | IncompleteArticleEventInvalidError
            | AssessedEventInvalidError
            | CuratedEventInvalidError,
        ):
            fields["error_reason"] = error.invalid.reason.value
        elif isinstance(error, MessageBodyMismatchError):
            fields["error_message"] = _BODY_CHECKSUM_MISMATCH_MESSAGE
        elif isinstance(error, MessageSendTransportError):
            fields.update(
                transport_stage=error.failure.stage.value,
                transport_reason=error.failure.reason.value,
            )
        elif isinstance(
            error, MessageSendCredentialsError | MessageSendUnexpectedError
        ):
            fields["exception_type"] = error.exception_type
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
