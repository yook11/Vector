"""補完のメッセージ処理の開始と終わり方、再配信の待機を、外部取得のログ方針の形で記録する。"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from structlog.typing import FilteringBoundLogger

from app.collection.article_acquisition.events import IncompleteArticleEventInvalidError
from app.collection.article_completion.consumer import (
    CompletionSucceeded,
    NoRetryCompletion,
    RetryCompletion,
)

if TYPE_CHECKING:
    from app.aws.lambda_.sqs_input import SqsLambdaInputInvalidError
    from app.aws.sqs.message import SqsMessageInvalidError
    from app.collection.retry_at import RetryAt
    from app.lambda_handlers.completion.redelivery_wait import VisibilityTimeout
    from app.lambda_handlers.event_reader import EventReadFailed


class CompletionMessageRecorder:
    """メッセージの終わり方ごとに、終端ログのイベント名・レベル・項目を決める。"""

    def __init__(self, logger: FilteringBoundLogger) -> None:
        self._logger = logger

    def record_started(self) -> None:
        self._logger.info("completion_message_processing_started")

    def record_read_failed(
        self, read_failed: EventReadFailed, *, duration_ms: float
    ) -> None:
        """本文・JSONを読めなかったか、イベントの契約に合わなかったかを区別して記録する。"""
        operation = (
            "validate_event"
            if isinstance(read_failed.error, IncompleteArticleEventInvalidError)
            else "parse_message"
        )
        self._logger.error(
            "completion_message_processing_failed",
            operation=operation,
            duration_ms=duration_ms,
            message_disposition="batch_item_failure",
            exc_info=read_failed.error,
        )

    def record_consume_failed(self, error: Exception, *, duration_ms: float) -> None:
        self._logger.error(
            "completion_message_processing_failed",
            operation="consume",
            duration_ms=duration_ms,
            message_disposition="batch_item_failure",
            exc_info=error,
        )

    def record_consumed(
        self,
        completion: CompletionSucceeded | RetryCompletion | NoRetryCompletion,
        *,
        duration_ms: float,
    ) -> None:
        """Consumerの結果から終わり方を決め、配送判断は変更しない。"""
        logger = self._logger.bind(duration_ms=duration_ms)
        match completion:
            case CompletionSucceeded(analyzable_article_id=article_id):
                logger.info(
                    "completion_message_processing_completed",
                    outcome="succeeded",
                    analyzable_article_id=article_id,
                    message_disposition="completed",
                )
            case RetryCompletion(
                error=error,
                code=code,
                retry_at=retry_at,
                requires_investigation=investigate,
            ):
                retry_fields = (
                    {} if retry_at is None else {"retry_at": retry_at.value.isoformat()}
                )
                # 人の調査が要る失敗だけをerrorにし、想定内の失敗はwarningにする。
                (logger.error if investigate else logger.warning)(
                    "completion_message_processing_failed",
                    code=code,
                    requires_investigation=investigate,
                    **retry_fields,
                    message_disposition="batch_item_failure",
                    exc_info=error,
                )
            case NoRetryCompletion(
                cause=Exception() as error,
                code=code,
                requires_investigation=investigate,
            ):
                (logger.error if investigate else logger.warning)(
                    "completion_message_processing_failed",
                    code=code,
                    requires_investigation=investigate,
                    message_disposition="completed",
                    exc_info=error,
                )
            case NoRetryCompletion(cause=reason):
                logger.info(
                    "completion_message_processing_completed",
                    outcome=reason,
                    message_disposition="completed",
                )
            case _:
                assert_never(completion)

    def record_unstarted(self) -> None:
        self._logger.warning(
            "completion_message_unstarted",
            reason="insufficient_time",
            message_disposition="batch_item_failure",
        )

    def record_invalid_message_batch(self, error: SqsLambdaInputInvalidError) -> None:
        self._logger.error("completion_sqs_input_invalid", exc_info=error)

    def record_retry_at_passed(self, retry_at: RetryAt, *, message_id: str) -> None:
        self._redelivery_wait_logger(retry_at, message_id=message_id).warning(
            "completion_redelivery_wait", result="expired"
        )

    def record_visibility_change_unstarted(
        self, retry_at: RetryAt, *, message_id: str
    ) -> None:
        self._redelivery_wait_logger(retry_at, message_id=message_id).warning(
            "completion_redelivery_wait", result="insufficient_time"
        )

    def record_receipt_handle_invalid(
        self,
        retry_at: RetryAt,
        error: SqsMessageInvalidError,
        *,
        message_id: str,
    ) -> None:
        self._redelivery_wait_logger(retry_at, message_id=message_id).error(
            "completion_redelivery_wait",
            result="invalid_receipt_handle",
            exc_info=error,
        )

    def record_visibility_change_failed(
        self,
        retry_at: RetryAt,
        visibility_timeout: VisibilityTimeout,
        error: Exception,
        *,
        message_id: str,
    ) -> None:
        # 可視性を変えられなくても、キューの通常の可視性で再配信される。
        self._redelivery_wait_logger(retry_at, message_id=message_id).warning(
            "completion_redelivery_wait",
            result="failed",
            visibility_timeout_seconds=visibility_timeout.seconds,
            capped=visibility_timeout.capped,
            exc_info=error,
        )

    def record_visibility_changed(
        self,
        retry_at: RetryAt,
        visibility_timeout: VisibilityTimeout,
        *,
        message_id: str,
    ) -> None:
        self._redelivery_wait_logger(retry_at, message_id=message_id).info(
            "completion_redelivery_wait",
            result="applied",
            visibility_timeout_seconds=visibility_timeout.seconds,
            capped=visibility_timeout.capped,
        )

    def _redelivery_wait_logger(
        self, retry_at: RetryAt, *, message_id: str
    ) -> FilteringBoundLogger:
        # 待機の設定は記事処理の後に動くため、メッセージの相関情報を明示して渡す。
        return self._logger.bind(
            message_id=message_id, retry_at=retry_at.value.isoformat()
        )
