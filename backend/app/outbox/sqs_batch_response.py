"""SQS応答の形式・ID対応・本文の整合性を確認し、共通の送信結果へ変換する。"""

import re
from dataclasses import dataclass, field
from itertools import chain
from uuid import UUID

from app.outbox.publish_errors import (
    PublishIntegrityError,
    PublishIntegrityReason,
    PublishResponseInvalidReason,
)
from app.outbox.publisher import PublishFailed, PublishSucceeded
from app.outbox.sqs_error_mapping import publish_error_from_sqs_entry
from app.outbox.sqs_message import SqsMessage
from app.outbox.sqs_message_batch import SqsMessageBatch
from app.outbox.sqs_response_errors import InvalidSqsBatchResponse, SqsResponseField


@dataclass(frozen=True, slots=True)
class SqsSuccessfulEntry:
    """形式検証を通過したSQSの受付成功で、本文の照合はまだ行っていない。"""

    id: str = field(repr=False)
    message_id: str = field(repr=False)
    md5_of_message_body: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SqsFailedEntry:
    """形式検証を通過したSQSの個別失敗で、自由文は保持しない。"""

    id: str = field(repr=False)
    code: str = field(repr=False)
    sender_fault: bool


@dataclass(frozen=True, slots=True)
class SqsBatchResponse:
    """形式検証済みの応答で、送信IDとの照合は別に行う。"""

    successful: tuple[SqsSuccessfulEntry, ...]
    failed: tuple[SqsFailedEntry, ...]
    request_id: str | None = field(repr=False)


def _required_string(
    entry: dict[str, object], key: str, *, field: SqsResponseField
) -> str:
    if key not in entry:
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.MISSING_REQUIRED_FIELD, field=field
        )
    value = entry.get(key)
    if not isinstance(value, str):
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.INVALID_TYPE, field=field
        )
    if not value:
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.EMPTY_REQUIRED_FIELD, field=field
        )
    return value


def _decode_successful_entry(entry: object) -> SqsSuccessfulEntry:
    if not isinstance(entry, dict):
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.INVALID_TYPE,
            field=SqsResponseField.SUCCESSFUL_ENTRY,
        )
    body_md5 = _required_string(
        entry, "MD5OfMessageBody", field=SqsResponseField.BODY_CHECKSUM
    )
    if re.fullmatch(r"[0-9a-fA-F]{32}", body_md5) is None:
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.INVALID_CHECKSUM_FORMAT,
            field=SqsResponseField.BODY_CHECKSUM,
        )
    return SqsSuccessfulEntry(
        id=_required_string(entry, "Id", field=SqsResponseField.ENTRY_ID),
        message_id=_required_string(
            entry, "MessageId", field=SqsResponseField.MESSAGE_ID
        ),
        md5_of_message_body=body_md5.lower(),
    )


def _decode_failed_entry(entry: object) -> SqsFailedEntry:
    if not isinstance(entry, dict):
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.INVALID_TYPE,
            field=SqsResponseField.FAILED_ENTRY,
        )
    if "SenderFault" not in entry:
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.MISSING_REQUIRED_FIELD,
            field=SqsResponseField.SENDER_FAULT,
        )
    if type(entry["SenderFault"]) is not bool:
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.INVALID_TYPE,
            field=SqsResponseField.SENDER_FAULT,
        )
    return SqsFailedEntry(
        id=_required_string(entry, "Id", field=SqsResponseField.ENTRY_ID),
        code=_required_string(entry, "Code", field=SqsResponseField.ERROR_CODE),
        sender_fault=entry["SenderFault"],
    )


def decode_sqs_batch_response(response: object) -> SqsBatchResponse:
    """未検証のSDK応答を形式検証し、ID・本文の照合前の内部型へ変換する。"""
    if not isinstance(response, dict):
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.INVALID_TYPE,
            field=SqsResponseField.RESPONSE,
        )
    successful = response.get("Successful", [])
    failed = response.get("Failed", [])
    if not isinstance(successful, list):
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.INVALID_TYPE,
            field=SqsResponseField.SUCCESSFUL_ENTRIES,
        )
    if not isinstance(failed, list):
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.INVALID_TYPE,
            field=SqsResponseField.FAILED_ENTRIES,
        )
    metadata = response.get("ResponseMetadata")
    request_id = metadata.get("RequestId") if isinstance(metadata, dict) else None
    return SqsBatchResponse(
        successful=tuple(_decode_successful_entry(entry) for entry in successful),
        failed=tuple(_decode_failed_entry(entry) for entry in failed),
        request_id=request_id if isinstance(request_id, str) else None,
    )


def validate_sqs_batch_event_ids(
    response: SqsBatchResponse, *, event_ids: set[str]
) -> None:
    """送った各イベントに成功または失敗が重複・欠落なく対応することを確認する。"""
    seen: set[str] = set()
    for entry in chain(response.successful, response.failed):
        if entry.id not in event_ids:
            raise InvalidSqsBatchResponse(
                reason=PublishResponseInvalidReason.UNKNOWN_ENTRY_ID,
                field=SqsResponseField.ENTRY_ID,
            )
        if entry.id in seen:
            raise InvalidSqsBatchResponse(
                reason=PublishResponseInvalidReason.DUPLICATE_ENTRY_ID,
                field=SqsResponseField.ENTRY_ID,
            )
        seen.add(entry.id)
    if seen != event_ids:
        raise InvalidSqsBatchResponse(
            reason=PublishResponseInvalidReason.MISSING_ENTRY_ID,
            field=SqsResponseField.ENTRY_ID,
        )


def _verify_sqs_body_checksum(
    entry: SqsSuccessfulEntry,
    *,
    message: SqsMessage,
    request_id: str | None,
) -> PublishSucceeded | PublishFailed:
    """SQSが返した本文のMD5を送信本文と照合し、送信結果を返す。"""
    if entry.md5_of_message_body == message.body_md5:
        return PublishSucceeded(message.event_id)
    return PublishFailed(
        message.event_id,
        PublishIntegrityError(
            reason=PublishIntegrityReason.BODY_CHECKSUM_MISMATCH,
            request_id=request_id,
        ),
    )


def results_from_sqs_batch_response(
    response: object, *, batch: SqsMessageBatch
) -> dict[UUID, PublishSucceeded | PublishFailed]:
    """応答全体の検証後、本文不一致や個別の分類失敗を該当イベントに限定する。"""
    decoded = decode_sqs_batch_response(response)
    messages_by_id = {str(message.event_id): message for message in batch.messages}
    validate_sqs_batch_event_ids(decoded, event_ids=set(messages_by_id))
    results: dict[UUID, PublishSucceeded | PublishFailed] = {}
    for entry in decoded.successful:
        message = messages_by_id[entry.id]
        results[message.event_id] = _verify_sqs_body_checksum(
            entry, message=message, request_id=decoded.request_id
        )
    for entry in decoded.failed:
        event_id = messages_by_id[entry.id].event_id
        error = publish_error_from_sqs_entry(
            code=entry.code, request_id=decoded.request_id
        )
        results[event_id] = PublishFailed(event_id, error)
    return results
