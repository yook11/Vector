"""Assessment Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from time import perf_counter

import structlog
from structlog.typing import FilteringBoundLogger

from app.analysis.assessment.domain.ready import AssessmentReadyBuildRejected
from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
)
from app.analysis.curation.events import CuratedEventInvalidError
from app.analysis.logging import create_article_analysis_logger
from app.lambda_handlers.assessment.composition import (
    build_article_list_notifier,
    open_assessment_consumer,
)
from app.lambda_handlers.assessment.event import (
    AssessmentMessageJsonInvalidError,
    parse_curated_signal_event,
)
from app.lambda_handlers.assessment.notification import ArticleListUpdateNotifier
from app.lambda_handlers.assessment.settings import AssessmentConsumerSettings
from app.lambda_handlers.sqs.errors import SqsInputError
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
        logger = create_article_analysis_logger().bind(stage="assessment")
        structlog.contextvars.bind_contextvars(
            service="article_analysis", stage="assessment"
        )
        request_id = getattr(context, "aws_request_id", None)
        if isinstance(request_id, str) and request_id.strip():
            logger = logger.bind(request_id=request_id)
            structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            settings = AssessmentConsumerSettings()  # type: ignore[call-arg]
            logger = logger.bind(environment=settings.env)
            structlog.contextvars.bind_contextvars(environment=settings.env)
            notifier = build_article_list_notifier(aws_region=settings.aws_region)
        except Exception as exc:
            logger.error(
                "assessment_initialization_failed",
                operation="settings",
                exc_info=exc,
            )
            raise
        failed_items = asyncio.run(
            _run_assessment(lambda_event, settings, notifier, logger=logger)
        )
        return SqsBatchFailureResponse(batchItemFailures=failed_items)
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**outer_context)


async def _run_assessment(
    lambda_event: object,
    settings: AssessmentConsumerSettings,
    notifier: ArticleListUpdateNotifier,
    *,
    logger: FilteringBoundLogger,
) -> list[SqsBatchItemIdentifier]:
    """資源を管理して各レコードの開始と結果を記録し、失敗した識別子を返す。"""
    async with open_assessment_consumer(settings, logger=logger) as consumer:
        try:
            record_batch = SqsRecordBatch.from_lambda_event(lambda_event)
        except SqsInputError as exc:
            logger.warning(
                "assessment_sqs_input_invalid",
                exc_info=exc,
            )
            raise

        failed_items: list[SqsBatchItemIdentifier] = []
        for record in record_batch.records:
            message_logger = logger.bind(message_id=record.message_id)
            started_at_seconds = perf_counter()
            message_logger.info("assessment_message_processing_started")
            try:
                message_body = record.body_text()
                curated_event = parse_curated_signal_event(message_body)
            except (SqsInputError, AssessmentMessageJsonInvalidError) as exc:
                message_logger.warning(
                    "assessment_message_processing_failed",
                    operation="parse_message",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=exc,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue
            except CuratedEventInvalidError as exc:
                message_logger.warning(
                    "assessment_message_processing_failed",
                    operation="validate_event",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=exc,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue
            except Exception as exc:
                message_logger.error(
                    "assessment_message_processing_failed",
                    operation="parse_message",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=exc,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue

            message_logger = message_logger.bind(
                event_id=str(curated_event.event_id),
                curation_id=curated_event.payload.curation_id,
                analyzable_article_id=curated_event.payload.analyzable_article_id,
            )
            try:
                completion = await consumer.consume(
                    curated_event.payload, logger=message_logger
                )
            except Exception as exc:
                message_logger.error(
                    "assessment_message_processing_failed",
                    duration_ms=elapsed_ms_since(started_at_seconds),
                    message_disposition="batch_item_failure",
                    exc_info=exc,
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
            else:
                if (
                    isinstance(completion, AssessmentCompletion)
                    and completion.kind is AssessmentCompletionKind.IN_SCOPE
                ):
                    with structlog.contextvars.bound_contextvars(
                        message_id=record.message_id,
                        event_id=str(curated_event.event_id),
                    ):
                        await notifier.notify_article_list_updated()
                if isinstance(completion, AssessmentReadyBuildRejected):
                    message_logger.warning(
                        "assessment_message_processing_failed",
                        operation="build_ready",
                        rejection_code=completion.reason.value,
                        duration_ms=elapsed_ms_since(started_at_seconds),
                        message_disposition="completed",
                    )
                else:
                    if completion.analyzed_article_id is not None:
                        message_logger = message_logger.bind(
                            analyzed_article_id=completion.analyzed_article_id
                        )
                    message_logger.info(
                        "assessment_message_processing_completed",
                        outcome=completion.kind.value,
                        duration_ms=elapsed_ms_since(started_at_seconds),
                        message_disposition="completed",
                    )
        return failed_items
