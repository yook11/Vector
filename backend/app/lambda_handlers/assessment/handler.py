"""Assessment Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from time import perf_counter

from structlog.contextvars import bound_contextvars
from structlog.typing import FilteringBoundLogger

from app.ai_providers.errors import AIProviderError
from app.analysis.assessment.consumer_failure_classification import RetryAssessment
from app.analysis.assessment.domain.ready import AssessmentReadyBuildRejected
from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
)
from app.analysis.curation.events import (
    ArticleCuratedSignalEvent,
    CuratedEventInvalidError,
)
from app.lambda_handlers.article_analysis_invocation import (
    open_article_analysis_invocation,
)
from app.lambda_handlers.article_analysis_lifecycle import (
    ArticleAnalysisLifecycleRecorder,
)
from app.lambda_handlers.assessment.composition import (
    build_article_list_notifier,
    open_assessment_consumer,
)
from app.lambda_handlers.assessment.notification import ArticleListUpdateNotifier
from app.lambda_handlers.assessment.settings import (
    AssessmentConsumerSettings,
    AssessmentLambdaSettings,
)
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
        "assessment", context, AssessmentLambdaSettings.load
    ) as invocation:
        settings = invocation.settings
        notifier = build_article_list_notifier(
            settings.notification, aws_region=settings.consumer.aws_region
        )
        failed_items = asyncio.run(
            _run_assessment(
                lambda_event,
                settings.consumer,
                notifier,
                logger=invocation.logger,
                failure_recorder=invocation.failure_recorder,
            )
        )
    return SqsBatchFailureResponse(batchItemFailures=failed_items)


async def _run_assessment(
    lambda_event: object,
    settings: AssessmentConsumerSettings,
    notifier: ArticleListUpdateNotifier,
    *,
    logger: FilteringBoundLogger,
    failure_recorder: ArticleAnalysisLifecycleRecorder,
) -> list[SqsBatchItemIdentifier]:
    """資源を管理して各レコードの開始と結果を記録し、失敗した識別子を返す。"""
    async with open_assessment_consumer(
        settings, failure_recorder=failure_recorder
    ) as consumer:
        try:
            record_batch = SqsRecordBatch.from_lambda_event(lambda_event)
        except SqsInputError as exc:
            logger.warning(
                "assessment_sqs_input_invalid",
                exc_info=exc,
            )
            raise

        failed_items: list[SqsBatchItemIdentifier] = []
        for record_input in record_batch.records:
            with bound_contextvars(message_id=record_input.message_id):
                started_at_seconds = perf_counter()
                logger.info("assessment_message_processing_started")
                try:
                    record = record_input.to_record()
                    parsed_body = record.parse_json()
                    curated_event = ArticleCuratedSignalEvent.from_input(parsed_body)
                except (SqsInputError, SqsMessageJsonInvalidError) as exc:
                    logger.warning(
                        "assessment_message_processing_failed",
                        operation="parse_message",
                        duration_ms=elapsed_ms_since(started_at_seconds),
                        message_disposition="batch_item_failure",
                        exc_info=exc,
                    )
                    failed_items.append(
                        SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                    )
                    continue
                except CuratedEventInvalidError as exc:
                    logger.warning(
                        "assessment_message_processing_failed",
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
                    logger.error(
                        "assessment_message_processing_failed",
                        operation="parse_message",
                        duration_ms=elapsed_ms_since(started_at_seconds),
                        message_disposition="batch_item_failure",
                        exc_info=exc,
                    )
                    failed_items.append(
                        SqsBatchItemIdentifier(itemIdentifier=record_input.message_id)
                    )
                    continue

                with bound_contextvars(
                    event_id=str(curated_event.event_id),
                    curation_id=curated_event.payload.curation_id,
                    analyzable_article_id=curated_event.payload.analyzable_article_id,
                ):
                    try:
                        completion = await consumer.consume(
                            curated_event.payload, logger=logger
                        )
                    except Exception as exc:
                        completion = RetryAssessment(exc)
                    if isinstance(completion, RetryAssessment):
                        logger.error(
                            "assessment_message_processing_failed",
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="batch_item_failure",
                            exc_info=completion.error,
                        )
                        failed_items.append(
                            SqsBatchItemIdentifier(
                                itemIdentifier=record_input.message_id
                            )
                        )
                    elif isinstance(completion, AssessmentCompletion):
                        if completion.kind is AssessmentCompletionKind.IN_SCOPE:
                            await notifier.notify_article_list_updated()
                        completion_logger = (
                            logger
                            if completion.analyzed_article_id is None
                            else logger.bind(
                                analyzed_article_id=completion.analyzed_article_id
                            )
                        )
                        completion_logger.info(
                            "assessment_message_processing_completed",
                            outcome=completion.kind.value,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="completed",
                        )
                    elif isinstance(completion.cause, AssessmentReadyBuildRejected):
                        logger.warning(
                            "assessment_message_processing_failed",
                            operation="build_ready",
                            rejection_code=completion.cause.reason.value,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="completed",
                        )
                    elif isinstance(completion.cause, AIProviderError):
                        logger.warning(
                            "assessment_message_processing_failed",
                            code=completion.cause.CODE,
                            failure_reason=completion.cause.reason.value,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="completed",
                        )
                    else:
                        logger.warning(
                            "assessment_message_processing_failed",
                            code=completion.cause.code,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                            message_disposition="completed",
                        )
        return failed_items
