"""Embeddingのメッセージ処理の開始と終わり方を、AI分析のログ方針の形で記録する。"""

from structlog.typing import FilteringBoundLogger

from app.ai_providers.errors import AIProviderError
from app.analysis.assessment.events import AssessedEventInvalidError
from app.analysis.embedding.consumer_failure_classification import (
    NoRetryEmbedding,
    RetryEmbedding,
)
from app.analysis.embedding.domain.ready import EmbeddingReadyBuildRejected
from app.analysis.embedding.service import EmbeddingCompletion
from app.lambda_handlers.event_reader import EventReadFailed
from app.lambda_handlers.sqs.errors import SqsInputError, SqsMessageJsonInvalidError


class EmbeddingMessageRecorder:
    """メッセージの終わり方ごとに、終端ログのイベント名・レベル・項目を決める。"""

    def __init__(self, logger: FilteringBoundLogger) -> None:
        self._logger = logger

    def record_started(self) -> None:
        self._logger.info("embedding_message_processing_started")

    def record_read_failed(
        self, read_failed: EventReadFailed, *, duration_ms: float
    ) -> None:
        logger = self._logger.bind(
            duration_ms=duration_ms, message_disposition="batch_item_failure"
        )
        match read_failed.error:
            case SqsInputError() | SqsMessageJsonInvalidError() as error:
                logger.warning(
                    "embedding_message_processing_failed",
                    operation="parse_message",
                    exc_info=error,
                )
            case AssessedEventInvalidError() as error:
                logger.warning(
                    "embedding_message_processing_failed",
                    operation="validate_event",
                    exc_info=error,
                )
            case error:
                logger.error(
                    "embedding_message_processing_failed",
                    operation="parse_message",
                    exc_info=error,
                )

    def record_consumed(
        self,
        consume_result: EmbeddingCompletion | NoRetryEmbedding | RetryEmbedding,
        *,
        duration_ms: float,
    ) -> None:
        logger = self._logger.bind(duration_ms=duration_ms)
        match consume_result:
            case EmbeddingCompletion() as completion:
                logger.info(
                    "embedding_message_processing_completed",
                    outcome=completion.value,
                    message_disposition="completed",
                )
            case RetryEmbedding(error=error):
                logger.error(
                    "embedding_message_processing_failed",
                    message_disposition="batch_item_failure",
                    exc_info=error,
                )
            case NoRetryEmbedding(cause=EmbeddingReadyBuildRejected(reason=reason)):
                logger.warning(
                    "embedding_message_processing_failed",
                    operation="build_ready",
                    rejection_code=reason.value,
                    message_disposition="completed",
                )
            case NoRetryEmbedding(cause=AIProviderError() as cause):
                logger.warning(
                    "embedding_message_processing_failed",
                    code=cause.CODE,
                    failure_reason=cause.reason.value,
                    message_disposition="completed",
                )
            case NoRetryEmbedding(cause=cause):
                logger.warning(
                    "embedding_message_processing_failed",
                    code=cause.code,
                    message_disposition="completed",
                )
