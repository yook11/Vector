"""取得依頼を処理し、再配信させるメッセージだけをSQSへ返す。"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

import structlog
from structlog.contextvars import bound_contextvars
from structlog.typing import FilteringBoundLogger

from app.aws.lambda_.sqs_input import (
    parse_sqs_lambda_input,
)
from app.aws.lambda_.sqs_response import (
    RedeliveryResponse,
    redelivery_response,
)
from app.aws.sqs.message import SqsMessage
from app.collection.article_acquisition.consumer_result import (
    RetryAcquisition,
)
from app.collection.sources.acquisition_request import (
    SourceAcquisitionRequest,
    acquisition_request_from_message,
)
from app.lambda_handlers.acquisition.composition import open_acquisition_consumer
from app.lambda_handlers.acquisition.message_recorder import AcquisitionMessageRecorder
from app.lambda_handlers.acquisition.settings import AcquisitionConsumerSettings
from app.lambda_handlers.article_fetch_lifecycle import ArticleFetchLifecycleRecorder
from app.lambda_handlers.event_reader import EventReader, EventReadFailed
from app.log_policy.policies.external_content import EXTERNAL_CONTENT_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.shared.time import elapsed_ms_since


def handler(sqs_input: object, context: object) -> RedeliveryResponse:
    with _open_invocation(context) as (logger, failure_recorder):
        try:
            messages = parse_sqs_lambda_input(sqs_input)
            settings = AcquisitionConsumerSettings()  # type: ignore[call-arg]
        except Exception as exc:
            failure_recorder.record_initialization_failure("input_or_settings", exc)
            raise RuntimeError("acquisition_initialization_failed") from None
        structlog.contextvars.bind_contextvars(environment=settings.env)
        try:
            return asyncio.run(
                _run(
                    messages,
                    settings,
                    logger=logger,
                    failure_recorder=failure_recorder,
                )
            )
        except Exception as exc:
            failure_recorder.record_initialization_failure("resources", exc)
            raise RuntimeError("acquisition_resources_failed") from None


@contextmanager
def _open_invocation(
    context: object,
) -> Iterator[tuple[FilteringBoundLogger, ArticleFetchLifecycleRecorder]]:
    """呼び出しの相関情報をcontextvarsに置き、呼び出し元の文脈は終了時に戻す。"""
    outer_context = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    try:
        structlog.contextvars.bind_contextvars(
            service="article_fetch", stage="acquisition"
        )
        request_id = getattr(context, "aws_request_id", None)
        if isinstance(request_id, str) and request_id.strip():
            structlog.contextvars.bind_contextvars(request_id=request_id)
        logger = create_policy_json_logger("article_fetch", EXTERNAL_CONTENT_LOG_RULES)
        yield logger, ArticleFetchLifecycleRecorder(logger, stage="acquisition")
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**outer_context)


async def _run(
    messages: tuple[SqsMessage, ...],
    settings: AcquisitionConsumerSettings,
    *,
    logger: FilteringBoundLogger,
    failure_recorder: ArticleFetchLifecycleRecorder,
) -> RedeliveryResponse:
    redelivery_message_ids: list[str] = []
    async with open_acquisition_consumer(
        settings, failure_recorder=failure_recorder
    ) as consumer:
        event_reader = EventReader(acquisition_request_from_message)
        message_recorder = AcquisitionMessageRecorder(logger)
        for message in messages:
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

                    case SourceAcquisitionRequest() as request:
                        with bound_contextvars(
                            acquisition_request_id=request.request_id,
                            source_id=request.source_id,
                        ):
                            try:
                                result = await consumer.consume(request)
                            except Exception as exc:
                                # Consumerから結果を受け取れなかった場合は再配信する。
                                message_recorder.record_consume_failed(
                                    exc,
                                    duration_ms=elapsed_ms_since(started_at_seconds),
                                )
                                redelivery_message_ids.append(message.message_id)
                                continue
                            if isinstance(result, RetryAcquisition):
                                redelivery_message_ids.append(message.message_id)
                            message_recorder.record_consumed(
                                result,
                                duration_ms=elapsed_ms_since(started_at_seconds),
                            )
    return redelivery_response(redelivery_message_ids)
