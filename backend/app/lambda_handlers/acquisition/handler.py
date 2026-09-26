"""取得依頼を処理し、失敗したメッセージだけをSQSへ返す。"""

import asyncio
from time import monotonic

import structlog

from app.audit.error_fields import exception_fqn
from app.collection.article_acquisition.consumer import AcquisitionFailed
from app.collection.article_acquisition.consumer_failure_classification import (
    AcquisitionFailureDecision,
)
from app.collection.article_acquisition.errors import AcquisitionSourceInvalidError
from app.collection.sources.acquisition_request import (
    AcquisitionRequestInvalidError,
    acquisition_request_from_message,
)
from app.lambda_handlers.acquisition.composition import open_acquisition_consumer
from app.lambda_handlers.acquisition.settings import AcquisitionConsumerSettings
from app.lambda_handlers.article_fetch_lifecycle import ArticleFetchLifecycleRecorder
from app.lambda_handlers.logging import setup_lambda_logging
from app.lambda_handlers.sqs.errors import SqsInputError, SqsMessageJsonInvalidError
from app.lambda_handlers.sqs.records import SqsRecordBatch
from app.lambda_handlers.sqs.response import (
    SqsBatchFailureResponse,
    SqsBatchItemIdentifier,
)

logger = structlog.get_logger(__name__)


def _record(**fields: object) -> None:
    try:
        logger.info("acquisition_message_processed", **fields)
    except Exception:  # noqa: S110
        # 診断障害で配送結果を変えない。
        pass


def handler(lambda_event: object, context: object) -> SqsBatchFailureResponse:
    setup_lambda_logging()
    try:
        batch = SqsRecordBatch.from_lambda_event(lambda_event)
        settings = AcquisitionConsumerSettings()  # type: ignore[call-arg]
    except Exception as exc:
        ArticleFetchLifecycleRecorder(
            logger, operation="acquisition"
        ).record_initialization_failure("input_or_settings", exc)
        raise RuntimeError("acquisition_initialization_failed") from None
    try:
        return asyncio.run(_run(batch, settings))
    except Exception as exc:
        ArticleFetchLifecycleRecorder(
            logger, operation="acquisition"
        ).record_initialization_failure("resources", exc)
        raise RuntimeError("acquisition_resources_failed") from None


async def _run(
    batch: SqsRecordBatch, settings: AcquisitionConsumerSettings
) -> SqsBatchFailureResponse:
    failures: list[SqsBatchItemIdentifier] = []
    async with open_acquisition_consumer(settings) as consumer:
        for record_input in batch.records:
            started = monotonic()
            fields: dict[str, object] = {"message_id": record_input.message_id}
            disposition = "completed"
            try:
                record = record_input.to_record()
                parsed_body = record.parse_json()
                request = acquisition_request_from_message(parsed_body)
                fields.update(
                    request_id=request.request_id, source_id=request.source_id
                )
                result = await consumer.consume(request)
                if isinstance(result, AcquisitionFailed):
                    fields.update(
                        result="failed",
                        code="processing_failed",
                        error_class=exception_fqn(result.error),
                    )
                    if result.decision is AcquisitionFailureDecision.RETRY:
                        disposition = "batch_item_failure"
                else:
                    fields.update(
                        result=result.result, created_count=result.created_count
                    )
            except Exception as exc:
                code = "processing_failed"
                if isinstance(
                    exc,
                    (
                        AcquisitionRequestInvalidError,
                        SqsMessageJsonInvalidError,
                        SqsInputError,
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
                failures.append(
                    SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                )
    return SqsBatchFailureResponse(batchItemFailures=failures)
