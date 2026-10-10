"""取得依頼を処理し、再配信させるメッセージだけをSQSへ返す。"""

import asyncio
from time import monotonic

import structlog

from app.audit.error_fields import exception_fqn
from app.collection.article_acquisition.consumer import AcquisitionSucceeded
from app.collection.article_acquisition.consumer_failure_classification import (
    NoRetryAcquisition,
    RetryAcquisition,
)
from app.collection.article_acquisition.errors import AcquisitionSourceInvalidError
from app.collection.article_acquisition.source_resolution import (
    AcquisitionNotRequired,
)
from app.collection.sources.acquisition_request import (
    AcquisitionRequestInvalidError,
    acquisition_request_from_message,
)
from app.lambda_handlers.acquisition.composition import open_acquisition_consumer
from app.lambda_handlers.acquisition.settings import AcquisitionConsumerSettings
from app.lambda_handlers.article_fetch_lifecycle import ArticleFetchLifecycleRecorder
from app.lambda_handlers.logging import setup_lambda_logging
from app.lambda_handlers.sqs.received_message import (
    ReceivedMessageBatch,
    ReceivedMessageInvalidError,
)
from app.lambda_handlers.sqs.response import (
    RedeliveryResponse,
    redelivery_response,
)

logger = structlog.get_logger(__name__)


def _record(**fields: object) -> None:
    try:
        logger.info("acquisition_message_processed", **fields)
    except Exception:  # noqa: S110
        # 診断障害で配送結果を変えない。
        pass


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
        for message in message_batch.messages:
            started = monotonic()
            fields: dict[str, object] = {"message_id": message.message_id}
            disposition = "completed"
            try:
                parsed_body = message.parse_json()
                request = acquisition_request_from_message(parsed_body)
                fields.update(
                    request_id=request.request_id, source_id=request.source_id
                )
                result = await consumer.consume(request)
                match result:
                    case AcquisitionSucceeded(created_count=created_count):
                        fields.update(result="acquired", created_count=created_count)
                    case AcquisitionNotRequired(reason=reason):
                        fields.update(result=reason, created_count=0)
                    case (
                        RetryAcquisition(error=error) | NoRetryAcquisition(error=error)
                    ):
                        fields.update(
                            result="failed",
                            code="processing_failed",
                            error_class=exception_fqn(error),
                        )
                if isinstance(result, RetryAcquisition):
                    disposition = "batch_item_failure"
            except Exception as exc:
                code = "processing_failed"
                if isinstance(
                    exc,
                    (
                        AcquisitionRequestInvalidError,
                        ReceivedMessageInvalidError,
                    ),
                ):
                    code = "invalid_request"
                elif isinstance(exc, AcquisitionSourceInvalidError):
                    code = "source_not_registered"
                fields.update(
                    result="failed", code=code, error_class=exception_fqn(exc)
                )
                disposition = "batch_item_failure"
            finally:
                fields["message_disposition"] = disposition
                fields["duration_seconds"] = monotonic() - started
                _record(**fields)
            if disposition == "batch_item_failure":
                redelivery_message_ids.append(message.message_id)
    return redelivery_response(redelivery_message_ids)
