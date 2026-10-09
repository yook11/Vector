"""Embedding Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from time import perf_counter

from structlog.contextvars import bound_contextvars
from structlog.typing import FilteringBoundLogger

from app.ai_providers.errors import AIProviderError
from app.analysis.assessment.events import (
    ArticleAssessedInScopeEvent,
    AssessedEventInvalidError,
)
from app.analysis.embedding.consumer_failure_classification import RetryEmbedding
from app.analysis.embedding.domain.ready import EmbeddingReadyBuildRejected
from app.analysis.embedding.service import EmbeddingCompletion
from app.lambda_handlers.article_analysis_invocation import (
    open_article_analysis_invocation,
)
from app.lambda_handlers.article_analysis_lifecycle import (
    ArticleAnalysisLifecycleRecorder,
)
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
    """呼び出しの文脈と設定を用意し、資源解放まで同じロガーを渡す。"""
    with open_article_analysis_invocation(
        "embedding", context, EmbeddingConsumerSettings
    ) as invocation:
        failed_items = asyncio.run(
            _run_embedding(
                lambda_event,
                invocation.settings,
                logger=invocation.logger,
                failure_recorder=invocation.failure_recorder,
            )
        )
    return SqsBatchFailureResponse(batchItemFailures=failed_items)


async def _run_embedding(
    lambda_event: object,
    settings: EmbeddingConsumerSettings,
    *,
    logger: FilteringBoundLogger,
    failure_recorder: ArticleAnalysisLifecycleRecorder,
) -> list[SqsBatchItemIdentifier]:
    """資源を管理して各レコードの開始と結果を記録し、失敗した識別子を返す。"""
    async with open_embedding_consumer(
        settings, failure_recorder=failure_recorder
    ) as consumer:
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
            with bound_contextvars(message_id=record_input.message_id):
                started_at_seconds = perf_counter()
                logger.info("embedding_message_processing_started")
                try:
                    record = record_input.to_record()
                    parsed_body = record.parse_json()
                    assessed_event = ArticleAssessedInScopeEvent.from_input(parsed_body)
                except (SqsInputError, SqsMessageJsonInvalidError) as exc:
                    logger.warning(
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
                    logger.warning(
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
                    logger.error(
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

                with bound_contextvars(
                    event_id=str(assessed_event.event_id),
                    curation_id=assessed_event.payload.curation_id,
                    analyzed_article_id=assessed_event.payload.analyzed_article_id,
                ):
                    try:
                        completion = await consumer.consume(
                            assessed_event.payload, logger=logger
                        )
                    except Exception as exc:
                        completion = RetryEmbedding(exc)
                    if isinstance(completion, RetryEmbedding):
                        logger.error(
                            "embedding_message_processing_failed",
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="batch_item_failure",
                            exc_info=completion.error,
                        )
                        failed_items.append(
                            SqsBatchItemIdentifier(
                                itemIdentifier=record_input.message_id
                            )
                        )
                    elif isinstance(completion, EmbeddingCompletion):
                        logger.info(
                            "embedding_message_processing_completed",
                            outcome=completion.value,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="completed",
                        )
                    elif isinstance(completion.cause, EmbeddingReadyBuildRejected):
                        logger.warning(
                            "embedding_message_processing_failed",
                            operation="build_ready",
                            rejection_code=completion.cause.reason.value,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="completed",
                        )
                    elif isinstance(completion.cause, AIProviderError):
                        logger.warning(
                            "embedding_message_processing_failed",
                            code=completion.cause.CODE,
                            failure_reason=completion.cause.reason.value,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="completed",
                        )
                    else:
                        logger.warning(
                            "embedding_message_processing_failed",
                            code=completion.cause.code,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="completed",
                        )
        return failed_items
