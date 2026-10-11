"""取得依頼を処理し、再配信させるメッセージだけをSQSへ返す。"""

import asyncio
from time import perf_counter

import structlog

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
from app.lambda_handlers.logging import setup_lambda_logging
from app.lambda_handlers.sqs.received_message import (
    ReceivedMessageBatch,
)
from app.lambda_handlers.sqs.response import (
    RedeliveryResponse,
    redelivery_response,
)
from app.shared.time import elapsed_ms_since

logger = structlog.get_logger(__name__)


def handler(sqs_event: object, context: object) -> RedeliveryResponse:
    setup_lambda_logging()
    try:
        message_batch = ReceivedMessageBatch.from_sqs_event(sqs_event)
        settings = AcquisitionConsumerSettings()  # type: ignore[call-arg]
    except Exception as exc:
        ArticleFetchLifecycleRecorder(
            logger, operation="acquisition"
        ).record_initialization_failure("input_or_settings", exc)
        raise RuntimeError("acquisition_initialization_failed") from None
    try:
        return asyncio.run(_run(message_batch, settings))
    except Exception as exc:
        ArticleFetchLifecycleRecorder(
            logger, operation="acquisition"
        ).record_initialization_failure("resources", exc)
        raise RuntimeError("acquisition_resources_failed") from None


async def _run(
    message_batch: ReceivedMessageBatch, settings: AcquisitionConsumerSettings
) -> RedeliveryResponse:
    redelivery_message_ids: list[str] = []
    async with open_acquisition_consumer(settings) as consumer:
        event_reader = EventReader(acquisition_request_from_message)
        message_recorder = AcquisitionMessageRecorder(logger)
        for message in message_batch.messages:
            started_at_seconds = perf_counter()
            match event_reader.read(message):
                case EventReadFailed() as read_failed:
                    message_recorder.record_read_failed(
                        read_failed,
                        message_id=message.message_id,
                        duration_ms=elapsed_ms_since(started_at_seconds),
                    )
                    redelivery_message_ids.append(message.message_id)

                case SourceAcquisitionRequest() as request:
                    try:
                        result = await consumer.consume(request)
                    except Exception as exc:
                        # Consumerから結果を受け取れなかった場合は再配信する。
                        message_recorder.record_consume_failed(
                            exc,
                            message_id=message.message_id,
                            request=request,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                        )
                        redelivery_message_ids.append(message.message_id)
                        continue
                    if isinstance(result, RetryAcquisition):
                        redelivery_message_ids.append(message.message_id)
                    message_recorder.record_consumed(
                        result,
                        message_id=message.message_id,
                        request=request,
                        duration_ms=elapsed_ms_since(started_at_seconds),
                    )
    return redelivery_response(redelivery_message_ids)
