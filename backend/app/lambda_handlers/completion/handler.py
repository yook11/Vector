"""SQSの補完イベントを逐次処理し、再配信対象のメッセージを返す。"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import assert_never

import structlog

from app.collection.article_acquisition.events import IncompleteArticleRecordedEvent
from app.collection.article_completion.consumer import (
    CompletionFailed,
    CompletionNotRequired,
    CompletionSucceeded,
)
from app.collection.article_completion.consumer_failure_classification import (
    CloseArticleCompletion,
    RetryArticleCompletion,
)
from app.lambda_handlers.article_fetch_lifecycle import ArticleFetchLifecycleRecorder
from app.lambda_handlers.completion.composition import open_completion_resources
from app.lambda_handlers.completion.failure_recorder import (
    CompletionLambdaFailureRecorder,
)
from app.lambda_handlers.completion.processing_start_deadline import (
    LambdaContext,
    ProcessingStartDeadline,
)
from app.lambda_handlers.completion.redelivery_wait import (
    RedeliveryMessage,
    apply_redelivery_waits,
)
from app.lambda_handlers.completion.settings import CompletionConsumerSettings
from app.lambda_handlers.event_reader import EventReader, EventReadFailed
from app.lambda_handlers.logging import setup_lambda_logging
from app.lambda_handlers.sqs.received_message import (
    ReceivedMessageBatch,
    ReceivedMessageBatchInvalidError,
)
from app.lambda_handlers.sqs.response import (
    RedeliveryResponse,
    redelivery_response,
)

logger = structlog.get_logger(__name__)


def handler(sqs_event: object, context: LambdaContext) -> RedeliveryResponse:
    """設定と資源を呼び出し単位で準備し、再配信させるメッセージだけを返す。"""
    setup_lambda_logging()
    try:
        settings = CompletionConsumerSettings()  # type: ignore[call-arg]
    except Exception as exc:
        ArticleFetchLifecycleRecorder(
            logger, operation="completion"
        ).record_initialization_failure("settings", exc)
        raise
    return asyncio.run(
        _run_completion(
            sqs_event,
            settings,
            context=context,
            now=lambda: datetime.now(UTC),
        )
    )


async def _run_completion(
    sqs_event: object,
    settings: CompletionConsumerSettings,
    *,
    context: LambdaContext,
    now: Callable[[], datetime],
) -> RedeliveryResponse:
    """全IDを検証してから本文・補完結果を個別の配送応答へ対応付ける。"""
    recorder = CompletionLambdaFailureRecorder(logger)
    async with open_completion_resources(settings) as resources:
        try:
            message_batch = ReceivedMessageBatch.from_sqs_event(sqs_event)
        except ReceivedMessageBatchInvalidError as exc:
            recorder.record_invalid_message_batch(exc)
            raise

        event_reader = EventReader(IncompleteArticleRecordedEvent.from_input)
        redelivery_messages: list[RedeliveryMessage] = []

        # 応答と後始末を間に合わせるため、
        # 記事の処理を始めてよいのはLambdaの制限時間の60秒前までとする。
        processing_start_deadline = ProcessingStartDeadline(
            context, before_lambda_limit=timedelta(seconds=60)
        )
        for message_position, message in enumerate(message_batch.messages):
            if processing_start_deadline.has_passed():
                for unstarted in message_batch.messages[message_position:]:
                    redelivery_messages.append(RedeliveryMessage(unstarted))
                    recorder.record_unstarted(message_id=unstarted.message_id)
                break

            match event_reader.read(message):
                case EventReadFailed() as read_failed:
                    recorder.record_read_failed(
                        read_failed, message_id=message.message_id
                    )
                    redelivery_messages.append(RedeliveryMessage(message))

                case IncompleteArticleRecordedEvent() as article_event:
                    try:
                        completion_result = await resources.consumer.consume(
                            article_event.payload.incomplete_article_id
                        )
                    except Exception as exc:
                        recorder.record_message_failure(
                            exc,
                            message_id=message.message_id,
                            article_event=article_event,
                        )
                        redelivery_messages.append(RedeliveryMessage(message))
                        continue

                    match completion_result:
                        case CompletionSucceeded() | CompletionNotRequired():
                            pass
                        case CompletionFailed(decision=CloseArticleCompletion()):
                            pass
                        case CompletionFailed(
                            decision=RetryArticleCompletion(retry_at=retry_at)
                        ):
                            redelivery_messages.append(
                                RedeliveryMessage(message, retry_at)
                            )
                        case _:
                            assert_never(completion_result)
                    recorder.record_completion(
                        completion_result,
                        message_id=message.message_id,
                        article_event=article_event,
                    )
        await apply_redelivery_waits(
            redelivery_messages,
            sqs_client=resources.sqs_client,
            queue_url=settings.sqs_article_completion_queue_url,
            context=context,
            now=now,
            recorder=recorder,
        )
        return redelivery_response(
            [
                redelivery_message.message.message_id
                for redelivery_message in redelivery_messages
            ]
        )
