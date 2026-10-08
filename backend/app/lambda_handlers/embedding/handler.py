"""Embedding Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from time import perf_counter

import structlog
from structlog.typing import FilteringBoundLogger

from app.ai_providers.errors import AIProviderError
from app.analysis.assessment.events import (
    ArticleAssessedInScopeEvent,
    AssessedEventInvalidError,
)
from app.analysis.embedding.consumer_failure_classification import RetryEmbedding
from app.analysis.embedding.domain.ready import EmbeddingReadyBuildRejected
from app.analysis.embedding.service import EmbeddingCompletion
from app.analysis.logging import create_article_analysis_logger
from app.lambda_handlers.embedding.composition import open_embedding_consumer
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings
from app.lambda_handlers.sqs.errors import SqsInputError, SqsMessageJsonInvalidError
from app.lambda_handlers.sqs.records import SqsRecordBatch
from app.lambda_handlers.sqs.response import (
    SqsBatchFailureResponse,
    SqsBatchItemIdentifier,
)
from app.shared.time import elapsed_ms_since


def handler(lambda_event: object, context: object) -> SqsBatchFailureResponse:
    """呼び出し文脈を分離し、設定取得から資源解放まで同じロガーを渡す。"""
    outer_context = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    try:
        logger = create_article_analysis_logger().bind(stage="embedding")
        structlog.contextvars.bind_contextvars(
            service="article_analysis", stage="embedding"
        )
        request_id = getattr(context, "aws_request_id", None)
        if isinstance(request_id, str) and request_id.strip():
            logger = logger.bind(request_id=request_id)
            structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            settings = EmbeddingConsumerSettings()  # type: ignore[call-arg]
            logger = logger.bind(environment=settings.env)
            structlog.contextvars.bind_contextvars(environment=settings.env)
        except Exception as exc:
            logger.error(
                "embedding_initialization_failed",
                operation="settings",
                exc_info=exc,
            )
            raise
        failed_items = asyncio.run(
            _run_embedding(lambda_event, settings, logger=logger)
        )
        return SqsBatchFailureResponse(batchItemFailures=failed_items)
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**outer_context)


async def _run_embedding(
    lambda_event: object,
    settings: EmbeddingConsumerSettings,
    *,
    logger: FilteringBoundLogger,
) -> list[SqsBatchItemIdentifier]:
    """資源を管理して各レコードの開始と結果を記録し、失敗した識別子を返す。"""
    async with open_embedding_consumer(settings, logger=logger) as consumer:
        try:
            record_batch = SqsRecordBatch.from_lambda_event(lambda_event)
        except SqsInputError as exc:
            logger.warning(
                "embedding_sqs_input_invalid",
                exc_info=exc,
            )
            raise

        failed_items: list[SqsBatchItemIdentifier] = []
        for record_input in record_batch.records:
            message_logger = logger.bind(message_id=record_input.message_id)
            started_at_seconds = perf_counter()
            message_logger.info("embedding_message_processing_started")
            try:
                record = record_input.to_record()
                parsed_body = record.parse_json()
                assessed_event = ArticleAssessedInScopeEvent.from_input(parsed_body)
            except (SqsInputError, SqsMessageJsonInvalidError) as exc:
                message_logger.warning(
                    "embedding_message_processing_failed",
                    operation="parse_message",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=exc,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                )
                continue
            except AssessedEventInvalidError as exc:
                message_logger.warning(
                    "embedding_message_processing_failed",
                    operation="validate_event",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=exc,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                )
                continue
            except Exception as exc:
                message_logger.error(
                    "embedding_message_processing_failed",
                    operation="parse_message",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=exc,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                )
                continue

            message_logger = message_logger.bind(
                event_id=str(assessed_event.event_id),
                curation_id=assessed_event.payload.curation_id,
                analyzed_article_id=assessed_event.payload.analyzed_article_id,
            )
            try:
                completion = await consumer.consume(
                    assessed_event.payload, logger=message_logger
                )
            except Exception as exc:
                completion = RetryEmbedding(exc)
            if isinstance(completion, RetryEmbedding):
                message_logger.error(
                    "embedding_message_processing_failed",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=completion.error,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                )
            elif isinstance(completion, EmbeddingCompletion):
                message_logger.info(
                    "embedding_message_processing_completed",
                    outcome=completion.value,
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="completed",
                )
            elif isinstance(completion.cause, EmbeddingReadyBuildRejected):
                message_logger.warning(
                    "embedding_message_processing_failed",
                    operation="build_ready",
                    rejection_code=completion.cause.reason.value,
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="completed",
                )
            elif isinstance(completion.cause, AIProviderError):
                message_logger.warning(
                    "embedding_message_processing_failed",
                    code=completion.cause.CODE,
                    failure_reason=completion.cause.reason.value,
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="completed",
                )
            else:
                message_logger.warning(
                    "embedding_message_processing_failed",
                    code=completion.cause.code,
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="completed",
                )
        return failed_items
