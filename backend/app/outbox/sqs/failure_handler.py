from uuid import UUID

from app.aws.sqs.outgoing_message import SqsResponseInvalidError
from app.log_policy.policies.pipeline_control import PIPELINE_CONTROL_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger


class SqsPublishFailureHandler:
    """SQS送信の診断を記録し、通常の出力障害を送信結果へ戻さない。"""

    def handle_invalid_response(
        self, *, error: SqsResponseInvalidError, event_ids: tuple[UUID, ...]
    ) -> None:
        create_policy_json_logger(__name__, PIPELINE_CONTROL_LOG_RULES).warning(
            "outbox_sqs_response_invalid",
            event_ids=[str(event_id) for event_id in event_ids],
            exc_info=error,
        )

    def handle_cleanup_failure(self, error: Exception) -> None:
        create_policy_json_logger(__name__, PIPELINE_CONTROL_LOG_RULES).warning(
            "outbox_publish_cleanup_failed", exc_info=error
        )
