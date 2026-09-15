"""取得依頼を処理し、失敗したメッセージだけをSQSへ返す。"""

import asyncio
from time import monotonic

import structlog

from app.audit.error_fields import exception_fqn
from app.collection.article_acquisition.consumer import AcquisitionSourceInvalidError
from app.collection.sources.acquisition_request import AcquisitionRequestInvalidError
from app.lambda_handlers.acquisition.composition import open_acquisition_consumer
from app.lambda_handlers.acquisition.message import (
    AcquisitionMessageJsonInvalidError,
    parse_acquisition_request,
)
from app.lambda_handlers.acquisition.settings import AcquisitionConsumerSettings
from app.lambda_handlers.article_fetch_lifecycle import ArticleFetchLifecycleRecorder
from app.lambda_handlers.logging import setup_lambda_logging
from app.lambda_handlers.sqs.errors import SqsInputError
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
        for record in batch.records:
            started = monotonic()
            fields: dict[str, object] = {"message_id": record.message_id}
            try:
                request = parse_acquisition_request(record.body_text())
                fields.update(
                    request_id=request.request_id, source_id=request.source_id
                )
                result = await consumer.consume(request)
                fields.update(result=result.result, created_count=result.created_count)
            except Exception as exc:
                code = "processing_failed"
                if isinstance(
                    exc,
                    (
                        AcquisitionRequestInvalidError,
                        AcquisitionMessageJsonInvalidError,
                        SqsInputError,
                    ),
                ):
                    code = "invalid_request"
                elif isinstance(exc, AcquisitionSourceInvalidError):
                    code = "source_not_registered"
                fields.update(
                    result="failed", code=code, error_class=exception_fqn(exc)
                )
                failures.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
            finally:
                fields["duration_seconds"] = monotonic() - started
                _record(**fields)
    return SqsBatchFailureResponse(batchItemFailures=failures)
