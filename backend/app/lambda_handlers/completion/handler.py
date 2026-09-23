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
from app.lambda_handlers.sqs.records import SqsRecord, SqsRecordBatch
from app.lambda_handlers.sqs.response import (
    SqsBatchFailureResponse,
    SqsBatchItemIdentifier,
)

logger = structlog.get_logger(__name__)

MIN_ARTICLE_REMAINING_MILLIS = 60_000


def handler(lambda_event: object, context: Any) -> SqsBatchFailureResponse:
    """設定と資源を呼び出し単位で準備し、失敗した項目だけを返す。"""
    setup_lambda_logging()
    try:
        settings = CompletionConsumerSettings()  # type: ignore[call-arg]
    except Exception as exc:
        ArticleFetchLifecycleRecorder(
            logger, operation="completion"
        ).record_initialization_failure("settings", exc)
        raise
    failed_items = asyncio.run(
        _run_completion(
            lambda_event,
            settings,
            context=context,
            now=lambda: datetime.now(UTC),
        )
    )
    return SqsBatchFailureResponse(batchItemFailures=failed_items)


async def _run_completion(
    lambda_event: object,
    settings: CompletionConsumerSettings,
    *,
    context: Any,
    now: Callable[[], datetime],
) -> list[SqsBatchItemIdentifier]:
    """全IDを検証してから本文・補完結果を個別の配送応答へ対応付ける。"""
    recorder = CompletionLambdaFailureRecorder(logger)
    async with open_completion_resources(settings) as resources:
        try:
            batch = SqsRecordBatch.from_lambda_event(lambda_event)
        except SqsInputError as exc:
            recorder.record_invalid_sqs_input(exc)
            raise

        failed_items: list[SqsBatchItemIdentifier] = []
        waits: list[RedeliveryWait] = []
        validated_records: list[SqsRecord] = []
        for index, record_input in enumerate(batch.records):
            if context.get_remaining_time_in_millis() < MIN_ARTICLE_REMAINING_MILLIS:
                for unstarted in batch.records[index:]:
                    failed_items.append(
                        SqsBatchItemIdentifier(itemIdentifier=unstarted.message_id)
                    )
                    recorder.record_unstarted(message_id=unstarted.message_id)
                break
            article_event = None
            try:
                record = record_input.to_record()
                validated_records.append(record)
                parsed_body = record.parse_json()
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
                            waits.append(
                                RedeliveryWait(record_input.message_id, retry_at)
                            )
                        failed_items.append(
                            SqsBatchItemIdentifier(
                                itemIdentifier=record_input.message_id
                            )
                        )
                    case _:
                        assert_never(completion)
            except Exception as exc:
                recorder.record_processing_error(
                    exc, message_id=record_input.message_id, article_event=article_event
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                )
                continue

            recorder.record_completion(
                completion,
                message_id=record_input.message_id,
                article_event=article_event,
            )
        await apply_redelivery_waits(
            waits,
            records=validated_records,
            sqs_client=resources.sqs_client,
            queue_url=settings.sqs_article_completion_queue_url,
            context=context,
            now=now,
            recorder=recorder,
        )
        return failed_items
