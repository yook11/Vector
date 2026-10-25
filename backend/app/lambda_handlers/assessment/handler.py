"""Assessment Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from time import perf_counter

from structlog.contextvars import bound_contextvars
from structlog.typing import FilteringBoundLogger

from app.analysis.assessment.consumer_failure_classification import RetryAssessment
from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
)
from app.analysis.curation.events import ArticleCuratedSignalEvent
from app.aws.lambda_.sqs_input import (
    SqsLambdaInputInvalidError,
    parse_sqs_lambda_input,
)
from app.aws.lambda_.sqs_response import (
    PartialBatchResponse,
    partial_batch_response,
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
from app.lambda_handlers.assessment.message_recorder import AssessmentMessageRecorder
from app.lambda_handlers.assessment.notification import ArticleListUpdateNotifier
from app.lambda_handlers.assessment.settings import (
    AssessmentConsumerSettings,
    AssessmentLambdaSettings,
)
from app.lambda_handlers.event_reader import EventReader, EventReadFailed
from app.shared.time import elapsed_ms_since


def handler(sqs_input: object, context: object) -> PartialBatchResponse:
    """呼び出しの文脈と設定を用意し、資源解放まで同じロガーを渡す。"""
    with open_article_analysis_invocation(
        "assessment", context, AssessmentLambdaSettings.load
    ) as invocation:
        settings = invocation.settings
        notifier = build_article_list_notifier(
            settings.notification, aws_region=settings.consumer.aws_region
        )
        return asyncio.run(
            _run_assessment(
                sqs_input,
                settings.consumer,
                notifier,
                logger=invocation.logger,
                failure_recorder=invocation.failure_recorder,
            )
        )


async def _run_assessment(
    sqs_input: object,
    settings: AssessmentConsumerSettings,
    notifier: ArticleListUpdateNotifier,
    *,
    logger: FilteringBoundLogger,
    failure_recorder: ArticleAnalysisLifecycleRecorder,
) -> PartialBatchResponse:
    """バッチの各メッセージを順に処理し、再配信させるメッセージのIDだけを返す。"""
    async with open_assessment_consumer(
        settings, failure_recorder=failure_recorder
    ) as consumer:
        # 失敗はメッセージIDで1件ずつ返すため、
        # IDを特定できないレコードがあれば全件を処理せず再配信させる。
        try:
            records = parse_sqs_lambda_input(sqs_input)
        except SqsLambdaInputInvalidError as exc:
            logger.warning(
                "assessment_sqs_input_invalid",
                exc_info=exc,
            )
            raise

        event_reader = EventReader(ArticleCuratedSignalEvent.from_input)
        message_recorder = AssessmentMessageRecorder(logger)
        redelivery_message_ids: list[str] = []

        for record in records:
            with bound_contextvars(message_id=record.message_id):
                started_at_seconds = perf_counter()
                message_recorder.record_started()

                match event_reader.read(record):
                    case EventReadFailed() as read_failed:
                        message_recorder.record_read_failed(
                            read_failed,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                        )
                        redelivery_message_ids.append(record.message_id)

                    case ArticleCuratedSignalEvent() as curated_event:
                        with bound_contextvars(
                            event_id=str(curated_event.event_id),
                            curation_id=curated_event.payload.curation_id,
                            analyzable_article_id=curated_event.payload.analyzable_article_id,
                        ):
                            try:
                                consume_result = await consumer.consume(
                                    curated_event.payload, logger=logger
                                )
                            except Exception as exc:
                                # Consumerから結果を受け取れなかった場合は再配信する。
                                message_recorder.record_consume_failed(
                                    exc,
                                    duration_ms=elapsed_ms_since(started_at_seconds),
                                )
                                redelivery_message_ids.append(record.message_id)
                                continue
                            if (
                                isinstance(consume_result, AssessmentCompletion)
                                and consume_result.kind
                                is AssessmentCompletionKind.IN_SCOPE
                            ):
                                await notifier.notify_article_list_updated()
                            message_recorder.record_consumed(
                                consume_result,
                                duration_ms=elapsed_ms_since(started_at_seconds),
                            )
                            if isinstance(consume_result, RetryAssessment):
                                redelivery_message_ids.append(record.message_id)
        return partial_batch_response(redelivery_message_ids)
