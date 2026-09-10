"""SQSメッセージをConsumerへ接続し、個別の失敗をAWSへ伝える。"""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, TypedDict

import structlog

from app.ai_providers.gemini.client import open_gemini_client
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.embedder import GeminiEmbedder
from app.analysis.embedding.service import (
    EmbeddingCompletion,
    EmbeddingCompletionReason,
)
from app.audit.error_fields import exception_fqn
from app.lambda_handlers.embedding_event import (
    EmbeddingEventInvalidError,
    parse_embedding_event,
)
from app.lambda_handlers.embedding_resources import open_embedding_resources
from app.lambda_handlers.settings import EmbeddingConsumerSettings
from app.logfire.exceptions import VectorDomainError

logger = structlog.get_logger(__name__)


class SqsBatchItemFailure(TypedDict):
    itemIdentifier: str


class SqsBatchResponse(TypedDict):
    batchItemFailures: list[SqsBatchItemFailure]


class EmbeddingSqsInputReason(StrEnum):
    """個別応答の対象を特定できない配送構造の不正。"""

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
class _SqsRecord:
    message_id: str
    body: object
    body_present: bool


def _validate_records(event: object) -> list[_SqsRecord]:
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
    for index, record in enumerate(event["Records"]):
        if not isinstance(record, dict):
            raise EmbeddingSqsInputError(
                reason=EmbeddingSqsInputReason.INVALID_TYPE,
                field="record",
                record_index=index,
            )
        if "messageId" not in record:
            raise EmbeddingSqsInputError(
                reason=EmbeddingSqsInputReason.MISSING_REQUIRED_FIELD,
                field="messageId",
                record_index=index,
            )
        message_id = record["messageId"]
        if not isinstance(message_id, str):
            reason = EmbeddingSqsInputReason.INVALID_TYPE
        elif not message_id.strip():
            reason = EmbeddingSqsInputReason.EMPTY_MESSAGE_ID
        elif message_id in seen:
            reason = EmbeddingSqsInputReason.DUPLICATE_MESSAGE_ID
        else:
            seen.add(message_id)
            records.append(_SqsRecord(message_id, record.get("body"), "body" in record))
            continue
        raise EmbeddingSqsInputError(
            reason=reason, field="messageId", record_index=index
        )
    return records


def _log(event: str, *, failed: bool = False, **fields: object) -> None:
    try:
        write = logger.warning if failed else logger.info
        write(event, **fields)
    except Exception:  # noqa: S110
        # 診断出力の通常障害でメッセージの結果を変えない。
        pass


async def process_embedding_messages(
    event: object, *, consumer: EmbeddingConsumer
) -> SqsBatchResponse:
    """配送構造を先に確定し、入力順に処理して失敗IDだけを返す。"""
    try:
        records = _validate_records(event)
    except EmbeddingSqsInputError as exc:
        _log(
            "embedding_sqs_input_invalid",
            failed=True,
            reason=exc.reason.value,
            field=exc.field,
            record_index=exc.record_index,
        )
        raise

    failures: list[SqsBatchItemFailure] = []
    for record in records:
        fields: dict[str, object] = {"message_id": record.message_id}
        if not record.body_present or not isinstance(record.body, str):
            _log(
                "embedding_message_input_invalid",
                failed=True,
                **fields,
                reason="invalid_body",
                issues=[
                    {
                        "field": "body",
                        "code": "missing_required_field"
                        if not record.body_present
                        else "invalid_type",
                    }
                ],
            )
            failures.append({"itemIdentifier": record.message_id})
            continue
        try:
            received = parse_embedding_event(record.body)
        except EmbeddingEventInvalidError as exc:
            _log(
                "embedding_message_input_invalid",
                failed=True,
                **fields,
                reason=exc.reason.value,
                issues=[
                    {"field": issue.field.value, "code": issue.code.value}
                    for issue in exc.issues
                ],
            )
            failures.append({"itemIdentifier": record.message_id})
            continue
        except Exception as exc:
            _log(
                "embedding_message_failed",
                failed=True,
                **fields,
                error_class=exception_fqn(exc),
            )
            failures.append({"itemIdentifier": record.message_id})
            continue

        fields.update(
            event_id=str(received.event_id),
            analyzed_article_id=received.payload.analyzed_article_id,
        )
        try:
            completion = await consumer.consume(received.payload)
            if not isinstance(completion, EmbeddingCompletion) or not (
                completion.reason is EmbeddingCompletionReason.SAVED
                or completion.reason is EmbeddingCompletionReason.ALREADY_EMBEDDED
            ):
                raise TypeError("invalid_embedding_completion")
        except Exception as exc:
            _log(
                "embedding_message_failed",
                failed=True,
                **fields,
                error_class=exception_fqn(exc),
            )
            failures.append({"itemIdentifier": record.message_id})
        else:
            _log(
                "embedding_message_completed", **fields, reason=completion.reason.value
            )
    return {"batchItemFailures": failures}


async def _run_embedding(
    event: object, settings: EmbeddingConsumerSettings
) -> SqsBatchResponse:
    """呼び出し内で資源を共有し、終了してから処理結果を返す。"""
    async with AsyncExitStack() as stack:
        stage = "resources"
        try:
            resources = await stack.enter_async_context(
                open_embedding_resources(settings)
            )
            stage = "gemini_client"
            client = await stack.enter_async_context(
                open_gemini_client(
                    api_key=resources.gemini_api_key,
                    settings=GeminiConnectionSettings(),
                )
            )
            stage = "consumer"
            consumer = EmbeddingConsumer(
                resources.session_factory, GeminiEmbedder(client=client)
            )
        except Exception as exc:
            _log(
                "embedding_initialization_failed",
                failed=True,
                stage=stage,
                error_class=exception_fqn(exc),
            )
            raise
        return await process_embedding_messages(event, consumer=consumer)


def handler(event: object, context: object) -> SqsBatchResponse:
    """今回の初期化・メッセージ処理・資源終了をひとつの非同期実行にまとめる。"""
    try:
        settings = EmbeddingConsumerSettings()  # type: ignore[call-arg]
    except Exception as exc:
        _log(
            "embedding_initialization_failed",
            failed=True,
            stage="settings",
            error_class=exception_fqn(exc),
        )
        raise
    return asyncio.run(_run_embedding(event, settings))
