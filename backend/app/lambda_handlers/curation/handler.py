"""Curation Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from time import perf_counter

import structlog
from structlog.typing import FilteringBoundLogger

from app.analysis.curation.consumer_failure_classification import RetryCuration
from app.analysis.curation.domain.ready import CurationReadyBuildRejected
from app.analysis.curation.service import CurationCompletion
from app.analysis.logging import create_article_analysis_logger
from app.collection.events import (
    AnalyzableArticleCreatedEvent,
    AnalyzableEventInvalidError,
)
from app.lambda_handlers.curation.composition import open_curation_consumer
from app.lambda_handlers.curation.settings import CurationConsumerSettings
from app.lambda_handlers.sqs.errors import SqsInputError, SqsMessageJsonInvalidError
from app.lambda_handlers.sqs.records import SqsRecordBatch
from app.lambda_handlers.sqs.response import (
    SqsBatchFailureResponse,
    SqsBatchItemIdentifier,
)
from app.shared.time import elapsed_ms_since


def handler(lambda_event: object, context: object) -> SqsBatchFailureResponse:
    """呼び出し文脈を分離し、設定取得から資源解放まで同じロガーを渡す。"""
    outer_context = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    try:
        logger = create_article_analysis_logger().bind(stage="curation")
        structlog.contextvars.bind_contextvars(
            service="article_analysis", stage="curation"
        )
        request_id = getattr(context, "aws_request_id", None)
        if isinstance(request_id, str) and request_id.strip():
            logger = logger.bind(request_id=request_id)
            structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            settings = CurationConsumerSettings()  # type: ignore[call-arg]
            logger = logger.bind(environment=settings.env)
            structlog.contextvars.bind_contextvars(environment=settings.env)
        except Exception as exc:
            logger.error(
                "curation_initialization_failed",
                operation="settings",
                exc_info=exc,
            )
            raise
        failed_items = asyncio.run(_run_curation(lambda_event, settings, logger=logger))
        return SqsBatchFailureResponse(batchItemFailures=failed_items)
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**outer_context)


async def _run_curation(
    lambda_event: object,
    settings: CurationConsumerSettings,
    *,
    logger: FilteringBoundLogger,
) -> list[SqsBatchItemIdentifier]:
    """資源を管理して各レコードの開始と結果を記録し、失敗した識別子を返す。"""
    async with open_curation_consumer(settings, logger=logger) as consumer:
        try:
            record_batch = SqsRecordBatch.from_lambda_event(lambda_event)
        except SqsInputError as exc:
            logger.warning(
                "curation_sqs_input_invalid",
                exc_info=exc,
            )
            raise

        failed_items: list[SqsBatchItemIdentifier] = []
        for record_input in record_batch.records:
            message_logger = logger.bind(message_id=record_input.message_id)
            started_at_seconds = perf_counter()
            message_logger.info("curation_message_processing_started")
            try:
                record = record_input.to_record()
                parsed_body = record.parse_json()
                article_event = AnalyzableArticleCreatedEvent.from_input(parsed_body)
            except (SqsInputError, SqsMessageJsonInvalidError) as exc:
                message_logger.warning(
                    "curation_message_processing_failed",
                    operation="parse_message",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=exc,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                )
                continue
            except AnalyzableEventInvalidError as exc:
                message_logger.warning(
                    "curation_message_processing_failed",
                    operation="validate_event",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=exc,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                )
                continue
            except Exception as exc:
                message_logger.error(
                    "curation_message_processing_failed",
                    operation="parse_message",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=exc,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                )
                continue

            message_logger = message_logger.bind(
                event_id=str(article_event.event_id),
                analyzable_article_id=article_event.payload.analyzable_article_id,
            )
            try:
                completion = await consumer.consume(
                    article_event.payload, logger=message_logger
                )
            except Exception as exc:
                completion = RetryCuration(exc)
            if isinstance(completion, RetryCuration):
                message_logger.error(
                    "curation_message_processing_failed",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=completion.error,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                )
            elif isinstance(completion, CurationCompletion):
                if completion.curation_id is not None:
                    message_logger = message_logger.bind(
                        curation_id=completion.curation_id
                    )
                message_logger.info(
                    "curation_message_processing_completed",
                    outcome=completion.kind.value,
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="completed",
                )
            elif isinstance(completion.cause, CurationReadyBuildRejected):
                message_logger.warning(
                    "curation_message_processing_failed",
                    operation="build_ready",
                    rejection_code=completion.cause.reason.value,
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="completed",
                )
            else:
                message_logger.warning(
                    "curation_message_processing_failed",
                    code=completion.cause.CODE,
                    failure_reason=completion.cause.reason.value,
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="completed",
                )
        return failed_items
