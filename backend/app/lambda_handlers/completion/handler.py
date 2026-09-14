"""SQSの補完イベントを逐次処理し、再配信対象のメッセージを返す。"""

import asyncio
from typing import TypedDict, assert_never

import structlog

from app.collection.article_acquisition.events import (
    IncompleteArticleEventInvalidError,
)
from app.collection.article_completion.consumer import (
    CompletionFailed,
    CompletionNotRequired,
    CompletionSucceeded,
)
from app.collection.article_completion.consumer_failure_classification import (
    CloseArticleCompletion,
    RetryArticleCompletion,
)
from app.lambda_handlers.article_fetch_lifecycle import ArticleFetchLifecycleRecorder
from app.lambda_handlers.completion.composition import open_completion_resources
from app.lambda_handlers.completion.event import (
    CompletionMessageJsonInvalidError,
    parse_incomplete_article_recorded_event,
)
from app.lambda_handlers.completion.failure_recorder import (
    CompletionLambdaFailureRecorder,
)
from app.lambda_handlers.completion.settings import CompletionConsumerSettings
from app.lambda_handlers.logging import setup_lambda_logging
from app.lambda_handlers.sqs.errors import SqsInputError
from app.lambda_handlers.sqs.records import SqsRecordBatch

logger = structlog.get_logger(__name__)


class SqsBatchItemIdentifier(TypedDict):
    itemIdentifier: str


class SqsBatchFailureResponse(TypedDict):
    batchItemFailures: list[SqsBatchItemIdentifier]


def handler(lambda_event: object, context: object) -> SqsBatchFailureResponse:
    """設定と資源を呼び出し単位で準備し、失敗した項目だけを返す。"""
    setup_lambda_logging()
    try:
        settings = CompletionConsumerSettings()  # type: ignore[call-arg]
    except Exception as exc:
        ArticleFetchLifecycleRecorder(
            logger, operation="completion"
        ).record_initialization_failure("settings", exc)
        raise
    failed_items = asyncio.run(_run_completion(lambda_event, settings))
    return SqsBatchFailureResponse(batchItemFailures=failed_items)


async def _run_completion(
    lambda_event: object, settings: CompletionConsumerSettings
) -> list[SqsBatchItemIdentifier]:
    """全IDを検証してから本文・補完結果を個別の配送応答へ対応付ける。"""
    recorder = CompletionLambdaFailureRecorder(logger)
    async with open_completion_resources(settings) as resources:
        try:
            batch = SqsRecordBatch.from_lambda_event(lambda_event)
        except SqsInputError as exc:
            recorder.record_invalid_sqs_input(exc)
            raise

        failed_items: list[SqsBatchItemIdentifier] = []
        for record in batch.records:
            try:
                body = record.body_text()
            except SqsInputError as exc:
                recorder.record_invalid_body(exc, message_id=record.message_id)
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue

            try:
                article_event = parse_incomplete_article_recorded_event(body)
            except CompletionMessageJsonInvalidError:
                recorder.record_invalid_json(message_id=record.message_id)
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue
            except IncompleteArticleEventInvalidError as exc:
                recorder.record_invalid_event(exc, message_id=record.message_id)
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue
            except Exception as exc:
                recorder.record_message_failure(exc, message_id=record.message_id)
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
                continue

            try:
                completion = await resources.consumer.consume(
                    article_event.payload.incomplete_article_id
                )
                fields: dict[str, object]
                match completion:
                    case CompletionSucceeded(analyzable_article_id=article_id):
                        fields = {
                            "result": "succeeded",
                            "analyzable_article_id": article_id,
                        }
                    case CompletionNotRequired(reason=reason):
                        fields = {"result": "not_required", "reason": reason}
                    case CompletionFailed(decision=decision):
                        fields = {
                            "code": decision.code,
                            "requires_investigation": decision.requires_investigation,
                        }
                        match decision:
                            case RetryArticleCompletion(retry_at=retry_at):
                                failed_items.append(
                                    SqsBatchItemIdentifier(
                                        itemIdentifier=record.message_id
                                    )
                                )
                                fields.update(
                                    result="retry",
                                    retry_at=retry_at.isoformat() if retry_at else None,
                                )
                            case CloseArticleCompletion():
                                fields["result"] = "closed"
                            case _:
                                assert_never(decision)
                    case _:
                        assert_never(completion)
            except Exception as exc:
                recorder.record_message_failure(
                    exc, message_id=record.message_id, article_event=article_event
                )
                failed_items.append(
                    SqsBatchItemIdentifier(itemIdentifier=record.message_id)
                )
            else:
                _log_completion(
                    message_id=record.message_id,
                    event_id=str(article_event.event_id),
                    incomplete_article_id=article_event.payload.incomplete_article_id,
                    **fields,
                )
        return failed_items


def _log_completion(**fields: object) -> None:
    try:
        logger.info("completion_message_processed", **fields)
    except Exception:  # noqa: S110
        # ログの通常障害で受信完了や再試行の判断を変更しない。
        pass
