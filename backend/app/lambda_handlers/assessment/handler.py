"""Assessment Lambdaの初期化・バッチ検証・逐次処理・応答と終了を進める。"""

import asyncio
from typing import TypedDict

import structlog

from app.analysis.assessment.domain.ready import AssessmentReadyBuildRejected
from app.lambda_handlers.assessment.composition import open_assessment_consumer
from app.lambda_handlers.assessment.event import (
    AssessmentEventInvalidError,
    parse_curated_signal_event,
)
from app.lambda_handlers.assessment.failure_recorder import (
    AssessmentLambdaFailureRecorder,
)
from app.lambda_handlers.assessment.settings import AssessmentConsumerSettings
from app.lambda_handlers.logging import setup_lambda_logging
from app.lambda_handlers.sqs.errors import SqsInputError
from app.lambda_handlers.sqs.records import SqsRecordBatch

logger = structlog.get_logger(__name__)


class SqsBatchItemIdentifier(TypedDict):
    itemIdentifier: str


class SqsBatchFailureResponse(TypedDict):
    batchItemFailures: list[SqsBatchItemIdentifier]


def handler(lambda_event: object, context: object) -> SqsBatchFailureResponse:
    """設定を読んで処理を実行し、失敗したメッセージをAWSへ報告する。"""
    setup_lambda_logging()
    try:
        settings = AssessmentConsumerSettings()  # type: ignore[call-arg]
    except Exception as exc:
        AssessmentLambdaFailureRecorder(logger).record_initialization_failure(
            "settings", exc
        )
        raise
    failed_items = asyncio.run(_run_assessment(lambda_event, settings))
    return SqsBatchFailureResponse(batchItemFailures=failed_items)


async def _run_assessment(
    lambda_event: object, settings: AssessmentConsumerSettings
) -> list[SqsBatchItemIdentifier]:
    """資源を管理して各レコードを処理し、失敗した項目の識別子を返す。"""
    failure_recorder = AssessmentLambdaFailureRecorder(logger)
    async with open_assessment_consumer(settings) as consumer:
        try:
            record_batch = SqsRecordBatch.from_lambda_event(lambda_event)
        except SqsInputError as exc:
            failure_recorder.record_invalid_sqs_input(exc)
            raise

        failed_items: list[SqsBatchItemIdentifier] = []
        for record in record_batch.records:
            try:
                message_body = record.body_text()
            except SqsInputError as exc:
                failure_recorder.record_invalid_body(exc, message_id=record.message_id)
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue

            try:
                curated_event = parse_curated_signal_event(message_body)
            except AssessmentEventInvalidError as exc:
                failure_recorder.record_invalid_event(exc, message_id=record.message_id)
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue
            except Exception as exc:
                failure_recorder.record_message_failure(
                    exc, message_id=record.message_id
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue

            try:
                completion = await consumer.consume(curated_event.payload)
            except Exception as exc:
                failure_recorder.record_message_failure(
                    exc, message_id=record.message_id, curated_event=curated_event
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
            else:
                rejection_fields = (
                    {"rejection_code": completion.reason.value}
                    if isinstance(completion, AssessmentReadyBuildRejected)
                    else {}
                )
                _log_completion(
                    message_id=record.message_id,
                    event_id=str(curated_event.event_id),
                    curation_id=curated_event.payload.curation_id,
                    analyzable_article_id=curated_event.payload.analyzable_article_id,
                    reason=(
                        "ready_build_rejected"
                        if isinstance(completion, AssessmentReadyBuildRejected)
                        else completion.kind.value
                    ),
                    **rejection_fields,
                )
        return failed_items


def _log_completion(**fields: object) -> None:
    try:
        logger.info("assessment_message_completed", **fields)
    except Exception:  # noqa: S110
        # 診断出力の通常障害でメッセージの結果を変えない。
        pass
