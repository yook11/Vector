from uuid import UUID

import structlog

from app.outbox.publishing.errors import PublishCleanupError
from app.outbox.sqs.response_errors import InvalidSqsBatchResponse

logger = structlog.get_logger(__name__)


class SqsPublishFailureHandler:
    """SQS送信の診断を記録し、通常の出力障害を送信結果へ戻さない。"""

    def handle_invalid_response(
        self, *, error: InvalidSqsBatchResponse, event_ids: tuple[UUID, ...]
    ) -> None:
        try:
            logger.warning(
                "outbox_sqs_response_invalid",
                error_reason=error.reason.value,
                response_field=error.field.value,
                event_ids=[str(event_id) for event_id in event_ids],
            )
        except Exception:  # noqa: S110 — 診断出力を再帰させず送信結果を維持する。
            pass

    def handle_cleanup_failure(self, error: PublishCleanupError) -> None:
        try:
            logger.warning(
                "outbox_publish_cleanup_failed",
                error_code=error.CODE,
                original_exception_type=error.original_exception_type,
            )
        except Exception:  # noqa: S110 — 診断出力を再帰させず元の結果を維持する。
            pass
