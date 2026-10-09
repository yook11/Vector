"""Curation Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from time import perf_counter

from structlog.contextvars import bound_contextvars
from structlog.typing import FilteringBoundLogger

from app.analysis.curation.consumer_failure_classification import RetryCuration
from app.collection.events import AnalyzableArticleCreatedEvent
from app.lambda_handlers.article_analysis_invocation import (
    open_article_analysis_invocation,
)
from app.lambda_handlers.article_analysis_lifecycle import (
    ArticleAnalysisLifecycleRecorder,
)
from app.lambda_handlers.curation.composition import open_curation_consumer
from app.lambda_handlers.curation.message_recorder import CurationMessageRecorder
from app.lambda_handlers.curation.settings import CurationConsumerSettings
from app.lambda_handlers.event_reader import EventReader, EventReadFailed
from app.lambda_handlers.sqs.errors import SqsInputError
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
        settings,
        failure_recorder=failure_recorder,
    ) as consumer:
        try:
            message_record_batch = SqsRecordBatch.from_lambda_event(lambda_event)

        except SqsInputError as exc:
            logger.warning(
                "curation_sqs_input_invalid",
                exc_info=exc,
            )
            raise

        event_reader = EventReader(AnalyzableArticleCreatedEvent.from_input)
        message_recorder = CurationMessageRecorder(logger)
        failed_items: list[SqsBatchItemIdentifier] = []

        for message_record in message_record_batch.records:
            with bound_contextvars(message_id=message_record.message_id):
                started_at_seconds = perf_counter()
                message_recorder.record_started()

                match event_reader.read(message_record):
                    case EventReadFailed() as read_failed:
                        message_recorder.record_read_failed(
                            read_failed,
                            duration_ms=elapsed_ms_since(started_at_seconds),
                        )
                        failed_items.append(
                            SqsBatchItemIdentifier(
                                itemIdentifier=message_record.message_id
                            )
                        )

                    case AnalyzableArticleCreatedEvent() as article_event:
                        with bound_contextvars(
                            event_id=str(article_event.event_id),
                            analyzable_article_id=article_event.payload.analyzable_article_id,
                        ):
                            try:
                                consume_result = await consumer.consume(
                                    article_event.payload, logger=logger
                                )
                            except Exception as exc:
                                consume_result = RetryCuration(exc)
                            message_recorder.record_consumed(
                                consume_result,
                                duration_ms=elapsed_ms_since(started_at_seconds),
                            )
                            if isinstance(consume_result, RetryCuration):
                                failed_items.append(
                                    SqsBatchItemIdentifier(
                                        itemIdentifier=message_record.message_id
                                    )
                                )
        return failed_items
