"""Embedding Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from time import perf_counter

from structlog.contextvars import bound_contextvars
from structlog.typing import FilteringBoundLogger

from app.analysis.assessment.events import ArticleAssessedInScopeEvent
from app.analysis.embedding.consumer_failure_classification import RetryEmbedding
from app.lambda_handlers.article_analysis_invocation import (
    open_article_analysis_invocation,
)
from app.lambda_handlers.article_analysis_lifecycle import (
    ArticleAnalysisLifecycleRecorder,
)
from app.lambda_handlers.embedding.composition import open_embedding_consumer
from app.lambda_handlers.embedding.message_recorder import EmbeddingMessageRecorder
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings
from app.lambda_handlers.event_reader import EventReader, EventReadFailed
from app.lambda_handlers.sqs.received_message import (
    ReceivedMessageBatch,
    ReceivedMessageBatchInvalidError,
)
from app.lambda_handlers.sqs.response import (
    RedeliveryResponse,
    redelivery_response,
)
from app.shared.time import elapsed_ms_since


def handler(sqs_event: object, context: object) -> RedeliveryResponse:
    """呼び出しの文脈と設定を用意し、資源解放まで同じロガーを渡す。"""
    with open_article_analysis_invocation(
        "embedding", context, EmbeddingConsumerSettings
    ) as invocation:
        return asyncio.run(
            _run_embedding(
                sqs_event,
                invocation.settings,
                logger=invocation.logger,
                failure_recorder=invocation.failure_recorder,
            )
        )


async def _run_embedding(
    sqs_event: object,
    settings: EmbeddingConsumerSettings,
    *,
    logger: FilteringBoundLogger,
    failure_recorder: ArticleAnalysisLifecycleRecorder,
) -> RedeliveryResponse:
    """バッチの各メッセージを順に処理し、再配信させるメッセージのIDだけを返す。"""
    async with open_embedding_consumer(
        settings, failure_recorder=failure_recorder
    ) as consumer:
        # 失敗はメッセージIDで1件ずつ返すため、
        # IDを特定できないレコードがあれば全件を処理せず再配信させる。
        try:
            message_batch = ReceivedMessageBatch.from_sqs_event(sqs_event)
        except ReceivedMessageBatchInvalidError as exc:
            logger.warning(
                "embedding_sqs_input_invalid",
                exc_info=exc,
            )
            raise

        event_reader = EventReader(ArticleAssessedInScopeEvent.from_input)
        message_recorder = EmbeddingMessageRecorder(logger)
        redelivery_message_ids: list[str] = []

        for message in message_batch.messages:
            with bound_contextvars(message_id=message.message_id):
                started_at_seconds = perf_counter()
                message_recorder.record_started()

                match event_reader.read(message):
                    case EventReadFailed() as read_failed:
                        message_recorder.record_read_failed(
                            read_failed,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                        )
                        redelivery_message_ids.append(message.message_id)

                    case ArticleAssessedInScopeEvent() as assessed_event:
                        with bound_contextvars(
                            event_id=str(assessed_event.event_id),
                            curation_id=assessed_event.payload.curation_id,
                            analyzed_article_id=assessed_event.payload.analyzed_article_id,
                        ):
                            try:
                                consume_result = await consumer.consume(
                                    assessed_event.payload, logger=logger
                                )
                            except Exception as exc:
                                consume_result = RetryEmbedding(exc)
                            message_recorder.record_consumed(
                                consume_result,
                                duration_ms=elapsed_ms_since(started_at_seconds),
                            )
                            if isinstance(consume_result, RetryEmbedding):
                                redelivery_message_ids.append(message.message_id)
        return redelivery_response(redelivery_message_ids)
