"""停止確定後に、診断ログと必要なメトリクスを書く。"""

from app.cloudwatch.emf import emit_metric
from app.log_policy.policies.pipeline_control import PIPELINE_CONTROL_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.outbox.relay.failures import PublishFailure
from app.outbox.relay.repository import LeasedOutboxEvent
from app.outbox.relay.retry_policy import NonRetryableReason
from app.outbox.relay.sender import (
    MessageSendConfigurationError,
    MessageSendConfigurationReason,
    MessageSendError,
    MessageServiceError,
    MessageServiceErrorReason,
)

logger = create_policy_json_logger(__name__, PIPELINE_CONTROL_LOG_RULES)

CONFIGURATION_FAILURE_METRIC = "outbox_publish_configuration_failure"


def _requires_configuration_fix(error: MessageSendError) -> bool:
    """資格情報・region・権限・送信先のキューを直すまで解消しない送信の失敗かを返す。"""
    match error:
        case MessageSendConfigurationError():
            # キューのURLが空なのは、設定層が起動時に弾いた後の呼び出し側の誤り。
            return error.reason in (
                MessageSendConfigurationReason.MISSING_CREDENTIALS,
                MessageSendConfigurationReason.INCOMPLETE_CREDENTIALS,
                MessageSendConfigurationReason.MISSING_REGION,
            )
        case MessageServiceError():
            return error.reason in (
                MessageServiceErrorReason.AUTHENTICATION_FAILED,
                MessageServiceErrorReason.ACCESS_DENIED,
                MessageServiceErrorReason.DESTINATION_NOT_FOUND,
            )
        # 資格情報の取得の失敗は、一時的か恒久的かを型から区別できない。
        case _:
            return False


def record_publish_failure(
    *,
    leased_event: LeasedOutboxEvent,
    error: PublishFailure,
    stop_reason: NonRetryableReason,
) -> None:
    """停止確定後に呼び、記録の通常失敗を配信処理へ戻さない。"""
    requires_fix = isinstance(error, MessageSendError) and _requires_configuration_fix(
        error
    )
    # 送り直せる失敗で回数を使い切った記事は、backfillがDBの状態から再投入する。
    log = (
        logger.warning
        if stop_reason is NonRetryableReason.RETRY_EXHAUSTED
        else logger.error
    )
    log(
        "outbox_delivery_stopped",
        event_id=str(leased_event.envelope.event_id),
        attempt_count=leased_event.attempt_count,
        stop_reason=stop_reason.value,
        requires_configuration_fix=requires_fix,
        exc_info=error,
    )
    if requires_fix:
        try:
            emit_metric(
                CONFIGURATION_FAILURE_METRIC,
                dimensions={},
                value=1,
                unit="Count",
            )
        except Exception as exc:
            logger.warning("outbox_failure_recording_failed", exc_info=exc)
