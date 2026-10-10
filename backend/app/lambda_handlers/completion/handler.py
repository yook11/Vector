"""SQSの補完イベントを逐次処理し、再配信対象のメッセージを返す。"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, assert_never

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
from app.lambda_handlers.completion.redelivery_wait import (
    RedeliveryWait,
    apply_redelivery_waits,
)
from app.lambda_handlers.completion.settings import CompletionConsumerSettings
from app.lambda_handlers.logging import setup_lambda_logging
from app.lambda_handlers.sqs.errors import SqsInputError
from app.lambda_handlers.sqs.received_message import ReceivedMessageBatch
from app.lambda_handlers.sqs.response import (
    RedeliveryResponse,
    redelivery_response,
)

logger = structlog.get_logger(__name__)

MIN_ARTICLE_REMAINING_MILLIS = 60_000


def handler(sqs_event: object, context: Any) -> RedeliveryResponse:
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
    context: Any,
    now: Callable[[], datetime],
) -> RedeliveryResponse:
    """全IDを検証してから本文・補完結果を個別の配送応答へ対応付ける。"""
    recorder = CompletionLambdaFailureRecorder(logger)
    async with open_completion_resources(settings) as resources:
        try:
            message_batch = ReceivedMessageBatch.from_sqs_event(sqs_event)
        except SqsInputError as exc:
            recorder.record_invalid_sqs_input(exc)
            raise

        redelivery_message_ids: list[str] = []
        waits: list[RedeliveryWait] = []
        for index, message in enumerate(message_batch.messages):
            if context.get_remaining_time_in_millis() < MIN_ARTICLE_REMAINING_MILLIS:
                for unstarted in message_batch.messages[index:]:
                    redelivery_message_ids.append(unstarted.message_id)
                    recorder.record_unstarted(message_id=unstarted.message_id)
                break
            article_event = None
            try:
                parsed_body = message.parse_json()
                article_event = IncompleteArticleRecordedEvent.from_input(parsed_body)
                completion = await resources.consumer.consume(
                    article_event.payload.incomplete_article_id
                )
                match completion:
                    case CompletionSucceeded() | CompletionNotRequired():
                        pass
                    case CompletionFailed(decision=CloseArticleCompletion()):
                        pass
                    case CompletionFailed(
                        decision=RetryArticleCompletion(retry_at=retry_at)
                    ):
                        if retry_at is not None:
                            waits.append(RedeliveryWait(message, retry_at))
                        redelivery_message_ids.append(message.message_id)
                    case _:
                        assert_never(completion)
            except Exception as exc:
                recorder.record_processing_error(
                    exc, message_id=message.message_id, article_event=article_event
                )
                redelivery_message_ids.append(message.message_id)
                continue

            recorder.record_completion(
                completion,
                message_id=message.message_id,
                article_event=article_event,
            )
        await apply_redelivery_waits(
            waits,
            sqs_client=resources.sqs_client,
            queue_url=settings.sqs_article_completion_queue_url,
            context=context,
            now=now,
            recorder=recorder,
        )
        return redelivery_response(redelivery_message_ids)
