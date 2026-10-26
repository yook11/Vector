"""SQSの補完イベントを逐次処理し、再配信対象のメッセージを返す。"""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from time import perf_counter

import structlog
from structlog.contextvars import bound_contextvars
from structlog.typing import FilteringBoundLogger

from app.aws.lambda_.sqs_input import (
    SqsLambdaInputInvalidError,
    parse_sqs_lambda_input,
)
from app.aws.lambda_.sqs_response import (
    PartialBatchResponse,
    partial_batch_response,
)
from app.collection.article_acquisition.events import IncompleteArticleRecordedEvent
from app.collection.article_completion.consumer import (
    RetryCompletion,
)
from app.lambda_handlers.article_fetch_lifecycle import ArticleFetchLifecycleRecorder
from app.lambda_handlers.completion.composition import open_completion_resources
from app.lambda_handlers.completion.message_recorder import CompletionMessageRecorder
from app.lambda_handlers.completion.processing_start_deadline import (
    LambdaContext,
    ProcessingStartDeadline,
)
from app.lambda_handlers.completion.redelivery_wait import (
    RedeliveryMessage,
    apply_visibility_timeouts,
)
from app.lambda_handlers.completion.settings import CompletionConsumerSettings
from app.lambda_handlers.event_reader import EventReader, EventReadFailed
from app.log_policy.policies.external_content import EXTERNAL_CONTENT_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.shared.time import elapsed_ms_since


def handler(sqs_input: object, context: LambdaContext) -> PartialBatchResponse:
    """設定と資源を呼び出し単位で準備し、再配信させるメッセージだけを返す。"""
    with _open_invocation(context) as (settings, logger, failure_recorder):
        return asyncio.run(
            _run_completion(
                sqs_input,
                settings,
                context=context,
                now=lambda: datetime.now(UTC),
                logger=logger,
                failure_recorder=failure_recorder,
            )
        )


@contextmanager
def _open_invocation(
    context: LambdaContext,
) -> Iterator[
    tuple[
        CompletionConsumerSettings, FilteringBoundLogger, ArticleFetchLifecycleRecorder
    ]
]:
    """呼び出しの相関情報をcontextvarsに置き、呼び出し元の文脈は終了時に戻す。"""
    outer_context = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    try:
        structlog.contextvars.bind_contextvars(
            service="article_fetch", stage="completion"
        )
        request_id = getattr(context, "aws_request_id", None)
        if isinstance(request_id, str) and request_id.strip():
            structlog.contextvars.bind_contextvars(request_id=request_id)
        logger = create_policy_json_logger("article_fetch", EXTERNAL_CONTENT_LOG_RULES)
        failure_recorder = ArticleFetchLifecycleRecorder(logger, stage="completion")
        try:
            settings = CompletionConsumerSettings()  # type: ignore[call-arg]
        except Exception as exc:
            failure_recorder.record_initialization_failure("settings", exc)
            raise
        structlog.contextvars.bind_contextvars(environment=settings.env)
        yield settings, logger, failure_recorder
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**outer_context)


async def _run_completion(
    sqs_input: object,
    settings: CompletionConsumerSettings,
    *,
    context: LambdaContext,
    now: Callable[[], datetime],
    logger: FilteringBoundLogger,
    failure_recorder: ArticleFetchLifecycleRecorder,
) -> PartialBatchResponse:
    """全IDを検証してから本文・補完結果を個別の配送応答へ対応付ける。"""
    recorder = CompletionMessageRecorder(logger)
    async with open_completion_resources(
        settings, failure_recorder=failure_recorder
    ) as resources:
        try:
            messages = parse_sqs_lambda_input(sqs_input)
        except SqsLambdaInputInvalidError as exc:
            recorder.record_invalid_message_batch(exc)
            raise

        event_reader = EventReader(IncompleteArticleRecordedEvent.from_input)
        redelivery_messages: list[RedeliveryMessage] = []

        # 応答と後始末を間に合わせるため、
        # 記事の処理を始めてよいのはLambdaの制限時間の60秒前までとする。
        processing_start_deadline = ProcessingStartDeadline(
            context, before_lambda_limit=timedelta(seconds=60)
        )
        for message_position, message in enumerate(messages):
            if processing_start_deadline.has_passed():
                for unstarted in messages[message_position:]:
                    redelivery_messages.append(RedeliveryMessage(unstarted))
                    with bound_contextvars(message_id=unstarted.message_id):
                        recorder.record_unstarted()
                break

            with bound_contextvars(message_id=message.message_id):
                started_at_seconds = perf_counter()
                recorder.record_started()

                match event_reader.read(message):
                    case EventReadFailed() as read_failed:
                        recorder.record_read_failed(
                            read_failed,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                        )
                        redelivery_messages.append(RedeliveryMessage(message))

                    case IncompleteArticleRecordedEvent() as article_event:
                        with bound_contextvars(
                            event_id=str(article_event.event_id),
                            incomplete_article_id=article_event.payload.incomplete_article_id,
                            source_id=article_event.payload.source_id,
                        ):
                            try:
                                completion_result = await resources.consumer.consume(
                                    article_event.payload.incomplete_article_id
                                )
                            except Exception as exc:
                                recorder.record_consume_failed(
                                    exc,
                                    duration_ms=elapsed_ms_since(started_at_seconds),
                                )
                                redelivery_messages.append(RedeliveryMessage(message))
                                continue

                            if isinstance(completion_result, RetryCompletion):
                                redelivery_messages.append(
                                    RedeliveryMessage(
                                        message, completion_result.retry_at
                                    )
                                )
                            recorder.record_consumed(
                                completion_result,
                                duration_ms=elapsed_ms_since(started_at_seconds),
                            )
        await apply_visibility_timeouts(
            redelivery_messages,
            sqs_client=resources.sqs_client,
            queue_url=settings.sqs_article_completion_queue_url,
            context=context,
            now=now,
            recorder=recorder,
        )
        return partial_batch_response(
            [
                redelivery_message.message.message_id
                for redelivery_message in redelivery_messages
            ]
        )
