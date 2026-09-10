"""SQSレコードの検証と記事処理の結果を部分バッチ応答へまとめる。"""

from typing import TypedDict

import structlog

from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.service import (
    EmbeddingCompletion,
    EmbeddingCompletionReason,
)
from app.lambda_handlers.embedding.event import (
    EmbeddingEventInvalidError,
    parse_embedding_event,
)
from app.lambda_handlers.embedding.failure_handler import EmbeddingLambdaFailureHandler
from app.lambda_handlers.sqs.errors import SqsInputError
from app.lambda_handlers.sqs.records import SqsRecord, SqsRecordBatch

logger = structlog.get_logger(__name__)


class SqsBatchItemFailure(TypedDict):
    itemIdentifier: str


class SqsBatchResponse(TypedDict):
    batchItemFailures: list[SqsBatchItemFailure]


def _log_completion(**fields: object) -> None:
    try:
        logger.info("embedding_message_completed", **fields)
    except Exception:  # noqa: S110
        # 診断出力の通常障害でメッセージの結果を変えない。
        pass


async def process_embedding_messages(
    event: object, *, consumer: EmbeddingConsumer
) -> SqsBatchResponse:
    """配送構造を先に確定し、入力順に処理して失敗IDだけを返す。"""
    try:
        batch = SqsRecordBatch.from_input(event)
    except SqsInputError as exc:
        EmbeddingLambdaFailureHandler(logger).handle_invalid_sqs_input(exc)
        raise

    failures: list[SqsBatchItemFailure] = []
    for record in batch.records:
        succeeded = await _process_record(record, consumer=consumer)
        if not succeeded:
            failures.append({"itemIdentifier": record.message_id})
    return {"batchItemFailures": failures}


async def _process_record(record: SqsRecord, *, consumer: EmbeddingConsumer) -> bool:
    """1件の本文検証・記事処理・結果ログを行い、正常完了したかを返す。"""
    failure_handler = EmbeddingLambdaFailureHandler(logger)
    try:
        body = record.body_text()
    except SqsInputError as exc:
        failure_handler.handle_invalid_body(exc, message_id=record.message_id)
        return False
    try:
        received = parse_embedding_event(body)
    except EmbeddingEventInvalidError as exc:
        failure_handler.handle_invalid_event(exc, message_id=record.message_id)
        return False
    except Exception as exc:
        failure_handler.handle_message_failure(exc, message_id=record.message_id)
        return False

    try:
        completion = await consumer.consume(received.payload)
        if not isinstance(completion, EmbeddingCompletion) or not (
            completion.reason is EmbeddingCompletionReason.SAVED
            or completion.reason is EmbeddingCompletionReason.ALREADY_EMBEDDED
        ):
            raise TypeError("invalid_embedding_completion")
    except Exception as exc:
        failure_handler.handle_message_failure(
            exc, message_id=record.message_id, received=received
        )
        return False
    else:
        _log_completion(
            message_id=record.message_id,
            event_id=str(received.event_id),
            analyzed_article_id=received.payload.analyzed_article_id,
            reason=completion.reason.value,
        )
    return True
