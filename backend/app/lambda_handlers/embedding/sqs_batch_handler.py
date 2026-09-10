"""SQSレコードの検証と記事処理の結果を部分バッチ応答へまとめる。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Self, TypedDict

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
from app.logfire.exceptions import VectorDomainError

logger = structlog.get_logger(__name__)


class SqsBatchItemFailure(TypedDict):
    itemIdentifier: str


class SqsBatchResponse(TypedDict):
    batchItemFailures: list[SqsBatchItemFailure]


class EmbeddingSqsInputReason(StrEnum):
    """SQS配送構造の不正を示す固定の理由。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    EMPTY_MESSAGE_ID = "empty_message_id"
    DUPLICATE_MESSAGE_ID = "duplicate_message_id"


class EmbeddingSqsInputError(VectorDomainError):
    """入力値を保持せず、配送構造の不正位置と理由を伝える。"""

    CODE: ClassVar[str] = "embedding_sqs_input_invalid"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "reason", "field", "record_index")

    def __init__(
        self,
        *,
        reason: EmbeddingSqsInputReason,
        field: str,
        record_index: int | None = None,
    ) -> None:
        super().__init__()
        self.reason = reason
        self.field = field
        self.record_index = record_index


@dataclass(frozen=True, slots=True)
class SqsRecord:
    message_id: str
    body: object
    body_present: bool

    @classmethod
    def from_input(cls, record: object, *, record_index: int) -> Self:
        """本文の検証より先に、失敗応答に使うIDを確定する。"""
        if not isinstance(record, dict):
            raise EmbeddingSqsInputError(
                reason=EmbeddingSqsInputReason.INVALID_TYPE,
                field="record",
                record_index=record_index,
            )
        if "messageId" not in record:
            raise EmbeddingSqsInputError(
                reason=EmbeddingSqsInputReason.MISSING_REQUIRED_FIELD,
                field="messageId",
                record_index=record_index,
            )
        message_id = record["messageId"]
        if not isinstance(message_id, str):
            reason = EmbeddingSqsInputReason.INVALID_TYPE
        elif not message_id.strip():
            reason = EmbeddingSqsInputReason.EMPTY_MESSAGE_ID
        else:
            return cls(message_id, record.get("body"), "body" in record)
        raise EmbeddingSqsInputError(
            reason=reason, field="messageId", record_index=record_index
        )

    def body_text(self) -> str:
        """本文の欠落と型不正を区別して、JSON解析へ渡す。"""
        if not self.body_present:
            reason = EmbeddingSqsInputReason.MISSING_REQUIRED_FIELD
        elif not isinstance(self.body, str):
            reason = EmbeddingSqsInputReason.INVALID_TYPE
        else:
            return self.body
        raise EmbeddingSqsInputError(reason=reason, field="body")


@dataclass(frozen=True, slots=True)
class SqsRecordBatch:
    """今回受信した、IDの検証を終えたレコードのまとまり。"""

    records: tuple[SqsRecord, ...]

    @classmethod
    def from_input(cls, event: object) -> Self:
        """一覧の構造とID重複を検証し、本文検証は各レコードの処理に委ねる。"""
        if not isinstance(event, dict):
            raise EmbeddingSqsInputError(
                reason=EmbeddingSqsInputReason.INVALID_TYPE, field="event"
            )
        if "Records" not in event:
            raise EmbeddingSqsInputError(
                reason=EmbeddingSqsInputReason.MISSING_REQUIRED_FIELD, field="Records"
            )
        if not isinstance(event["Records"], list):
            raise EmbeddingSqsInputError(
                reason=EmbeddingSqsInputReason.INVALID_TYPE, field="Records"
            )
        records = []
        seen: set[str] = set()
        for index, value in enumerate(event["Records"]):
            record = SqsRecord.from_input(value, record_index=index)
            if record.message_id in seen:
                raise EmbeddingSqsInputError(
                    reason=EmbeddingSqsInputReason.DUPLICATE_MESSAGE_ID,
                    field="messageId",
                    record_index=index,
                )
            seen.add(record.message_id)
            records.append(record)
        return cls(records=tuple(records))


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
    except EmbeddingSqsInputError as exc:
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
    except EmbeddingSqsInputError as exc:
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
