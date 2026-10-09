"""Curation Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from time import perf_counter

from structlog.contextvars import bound_contextvars
from structlog.typing import FilteringBoundLogger

from app.analysis.curation.consumer_failure_classification import RetryCuration
from app.analysis.curation.domain.ready import CurationReadyBuildRejected
from app.analysis.curation.service import CurationCompletion
from app.collection.events import (
    AnalyzableArticleCreatedEvent,
    AnalyzableEventInvalidError,
)
from app.lambda_handlers.article_analysis_invocation import (
    open_article_analysis_invocation,
)
from app.lambda_handlers.article_analysis_lifecycle import (
    ArticleAnalysisLifecycleRecorder,
)
from app.lambda_handlers.curation.composition import open_curation_consumer
from app.lambda_handlers.curation.settings import CurationConsumerSettings
from app.lambda_handlers.event_reader import EventReader, EventReadFailed
from app.lambda_handlers.sqs.errors import SqsInputError, SqsMessageJsonInvalidError
from app.lambda_handlers.sqs.records import SqsRecordBatch
from app.lambda_handlers.sqs.response import (
    SqsBatchFailureResponse,
    SqsBatchItemIdentifier,
)
from app.shared.time import elapsed_ms_since


def handler(lambda_event: object, context: object) -> SqsBatchFailureResponse:
    """呼び出しの文脈と設定を用意し、資源解放まで同じロガーを渡す。"""
    with open_article_analysis_invocation(
        "curation", context, CurationConsumerSettings
    ) as invocation:
        failed_items = asyncio.run(
            _run_curation(
                lambda_event,
                invocation.settings,
                logger=invocation.logger,
                failure_recorder=invocation.failure_recorder,
            )
        )
    return SqsBatchFailureResponse(batchItemFailures=failed_items)


async def _run_curation(
    lambda_event: object,
    settings: CurationConsumerSettings,
    *,
    logger: FilteringBoundLogger,
    failure_recorder: ArticleAnalysisLifecycleRecorder,
) -> list[SqsBatchItemIdentifier]:
    """資源を管理して各レコードの開始と結果を記録し、失敗した識別子を返す。"""
    async with open_curation_consumer(
        settings, failure_recorder=failure_recorder
    ) as consumer:
        try:
            record_batch = SqsRecordBatch.from_lambda_event(lambda_event)
        except SqsInputError as exc:
            logger.warning(
                "curation_sqs_input_invalid",
                exc_info=exc,
            )
            raise

        event_reader = EventReader(AnalyzableArticleCreatedEvent.from_input)
        failed_items: list[SqsBatchItemIdentifier] = []

        for record_input in record_batch.records:
            with bound_contextvars(message_id=record_input.message_id):
                started_at_seconds = perf_counter()
                logger.info("curation_message_processing_started")

                read_result = event_reader.read(record_input)

                if isinstance(read_result, EventReadFailed):
                    match read_result.error:
                        case SqsInputError() | SqsMessageJsonInvalidError():
                            log_failure, operation = logger.warning, "parse_message"
                        case AnalyzableEventInvalidError():
                            log_failure, operation = logger.warning, "validate_event"
                        case _:
                            log_failure, operation = logger.error, "parse_message"
                    log_failure(
                        "curation_message_processing_failed",
                        operation=operation,
                        duration_ms=elapsed_ms_since(started_at_seconds),
                        message_disposition="batch_item_failure",
                        exc_info=read_result.error,
                    )
                    failed_items.append(
                        SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                    )
                    continue
                article_event = read_result

                with bound_contextvars(
                    event_id=str(article_event.event_id),
                    analyzable_article_id=article_event.payload.analyzable_article_id,
                ):
                    try:
                        completion = await consumer.consume(
                            article_event.payload, logger=logger
                        )
                    except Exception as exc:
                        completion = RetryCuration(exc)
                    if isinstance(completion, RetryCuration):
                        logger.error(
                            "curation_message_processing_failed",
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="batch_item_failure",
                            exc_info=completion.error,
                        )
                        failed_items.append(
                            SqsBatchItemIdentifier(
                                itemIdentifier=record_input.message_id
                            )
                        )
                    elif isinstance(completion, CurationCompletion):
                        completion_logger = (
                            logger
                            if completion.curation_id is None
                            else logger.bind(curation_id=completion.curation_id)
                        )
                        completion_logger.info(
                            "curation_message_processing_completed",
                            outcome=completion.kind.value,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="completed",
                        )
                    elif isinstance(completion.cause, CurationReadyBuildRejected):
                        logger.warning(
                            "curation_message_processing_failed",
                            operation="build_ready",
                            rejection_code=completion.cause.reason.value,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="completed",
                        )
                    else:
                        logger.warning(
                            "curation_message_processing_failed",
                            code=completion.cause.CODE,
                            failure_reason=completion.cause.reason.value,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="completed",
                        )
        return failed_items
