"""SQSへ送る本文と照合用のMD5を持ち、SQSが本文どおりに受け付けたかを検証する。"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import md5
from types import MappingProxyType

from app.aws.sqs.errors import MessageSendError, to_sqs_service_error

# SQSのメッセージ1件と、SendMessageBatch 1回分の本文合計の上限(UTF-8のバイト数)。
MAX_MESSAGE_BYTES = 1_048_576


class SqsResponseField(StrEnum):
    """SQS応答のどの部分に問題があるかを表す。"""

    RESPONSE = "response"
    SUCCESSFUL_ENTRIES = "successful_entries"
    FAILED_ENTRIES = "failed_entries"
    SUCCESSFUL_ENTRY = "successful_entry"
    FAILED_ENTRY = "failed_entry"
    ENTRY_ID = "entry_id"
    MESSAGE_ID = "message_id"
    BODY_CHECKSUM = "body_checksum"
    ERROR_CODE = "error_code"
    SENDER_FAULT = "sender_fault"


class SqsResponseInvalidReason(StrEnum):
    """SQSの応答の項目またはIDの、検証条件を満たさなかった理由を表す。"""

    # 値の形の違反。
    INVALID_TYPE = "invalid_type"
    MISSING = "missing"
    EMPTY = "empty"
    INVALID_FORMAT = "invalid_format"
    # 送ったIDとの対応の違反で、項目は常にentry_idになる。
    NOT_IN_REQUEST = "not_in_request"
    DUPLICATE = "duplicate"
    NOT_RETURNED = "not_returned"


class MessageTooLargeError(MessageSendError):
    """本文がSQSの1件の上限を超えていて、送る前に止めた。"""

    def __init__(self) -> None:
        super().__init__("メッセージの本文が送信できる大きさを超えています")


class SqsResponseInvalidError(MessageSendError):
    """SQSの応答の形式または送ったIDとの対応が不正で、受け付けたかを確かめられない。"""

    def __init__(
        self, *, reason: SqsResponseInvalidReason, field: SqsResponseField
    ) -> None:
        self.reason = reason
        self.field = field
        super().__init__(
            "SQSの応答を検証できませんでした",
            details={"reason": self.reason.value, "field": self.field.value},
        )


class MessageBodyMismatchError(MessageSendError):
    """SQSが受け取った本文のMD5が、送った本文のMD5と一致しない。"""

    def __init__(self) -> None:
        super().__init__(
            "SQSが受け取った本文が送信した本文と一致しません。"
            "SQSには受け付けられている可能性があります"
        )


def _required_string(
    fields: Mapping[str, object], key: str, field: SqsResponseField
) -> str:
    if key not in fields:
        raise SqsResponseInvalidError(
            reason=SqsResponseInvalidReason.MISSING, field=field
        )
    value = fields[key]
    if not isinstance(value, str):
        raise SqsResponseInvalidError(
            reason=SqsResponseInvalidReason.INVALID_TYPE, field=field
        )
    if not value:
        raise SqsResponseInvalidError(
            reason=SqsResponseInvalidReason.EMPTY, field=field
        )
    return value


@dataclass(frozen=True, slots=True)
class AcceptedSqsMessage:
    """SQSに受け付けられ、SQSが受け取った本文が送った本文と一致したメッセージ。"""

    message_id: str


@dataclass(frozen=True, slots=True)
class OutgoingSqsMessage:
    """送る本文から期待するMD5を確定し、SQSが受け付けたと返した項目がその本文かを照合する。"""

    body: str
    body_md5: str = field(init=False)

    def __post_init__(self) -> None:
        encoded = self.body.encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise MessageTooLargeError()
        object.__setattr__(
            self, "body_md5", md5(encoded, usedforsecurity=False).hexdigest()
        )

    def verify(self, result: object) -> AcceptedSqsMessage:
        if not isinstance(result, dict):
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.INVALID_TYPE,
                field=SqsResponseField.RESPONSE,
            )
        message_id = _required_string(result, "MessageId", SqsResponseField.MESSAGE_ID)
        checksum = _required_string(
            result, "MD5OfMessageBody", SqsResponseField.BODY_CHECKSUM
        )
        if re.fullmatch(r"[0-9a-fA-F]{32}", checksum) is None:
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.INVALID_FORMAT,
                field=SqsResponseField.BODY_CHECKSUM,
            )
        if checksum.lower() != self.body_md5:
            raise MessageBodyMismatchError()
        return AcceptedSqsMessage(message_id)


@dataclass(frozen=True, slots=True)
class OutgoingSqsMessageBatch:
    """1回のSendMessageBatchで送るメッセージをIDごとに持ち、応答をIDで突き合わせる。"""

    messages: Mapping[str, OutgoingSqsMessage]

    def __post_init__(self) -> None:
        messages = MappingProxyType(dict(self.messages))
        if not 1 <= len(messages) <= 10:
            raise ValueError("batch must contain between one and ten messages")
        if (
            sum(len(message.body.encode("utf-8")) for message in messages.values())
            > MAX_MESSAGE_BYTES
        ):
            raise ValueError("batch message bodies exceed the size limit")
        object.__setattr__(self, "messages", messages)

    def entries(self) -> list[dict[str, str]]:
        return [
            {"Id": entry_id, "MessageBody": message.body}
            for entry_id, message in self.messages.items()
        ]

    def verify(
        self, response: object
    ) -> Mapping[str, AcceptedSqsMessage | MessageSendError]:
        if not isinstance(response, dict):
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.INVALID_TYPE,
                field=SqsResponseField.RESPONSE,
            )
        successful = response.get("Successful", [])
        if not isinstance(successful, list):
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.INVALID_TYPE,
                field=SqsResponseField.SUCCESSFUL_ENTRIES,
            )
        failed = response.get("Failed", [])
        if not isinstance(failed, list):
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.INVALID_TYPE,
                field=SqsResponseField.FAILED_ENTRIES,
            )
        seen: set[str] = set()
        results: dict[str, AcceptedSqsMessage | MessageSendError] = {}
        for entry in successful:
            if not isinstance(entry, dict):
                raise SqsResponseInvalidError(
                    reason=SqsResponseInvalidReason.INVALID_TYPE,
                    field=SqsResponseField.SUCCESSFUL_ENTRY,
                )
            entry_id = self._claim_id(entry, seen)
            try:
                results[entry_id] = self.messages[entry_id].verify(entry)
            except MessageBodyMismatchError as mismatch:
                results[entry_id] = mismatch
        for entry in failed:
            if not isinstance(entry, dict):
                raise SqsResponseInvalidError(
                    reason=SqsResponseInvalidReason.INVALID_TYPE,
                    field=SqsResponseField.FAILED_ENTRY,
                )
            entry_id = self._claim_id(entry, seen)
            if "SenderFault" not in entry:
                raise SqsResponseInvalidError(
                    reason=SqsResponseInvalidReason.MISSING,
                    field=SqsResponseField.SENDER_FAULT,
                )
            if type(entry["SenderFault"]) is not bool:
                raise SqsResponseInvalidError(
                    reason=SqsResponseInvalidReason.INVALID_TYPE,
                    field=SqsResponseField.SENDER_FAULT,
                )
            results[entry_id] = to_sqs_service_error(
                _required_string(entry, "Code", SqsResponseField.ERROR_CODE)
            )
        if seen != self.messages.keys():
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.NOT_RETURNED,
                field=SqsResponseField.ENTRY_ID,
            )
        return MappingProxyType(
            {entry_id: results[entry_id] for entry_id in self.messages}
        )

    def _claim_id(self, entry: Mapping[str, object], seen: set[str]) -> str:
        entry_id = _required_string(entry, "Id", SqsResponseField.ENTRY_ID)
        if entry_id not in self.messages:
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.NOT_IN_REQUEST,
                field=SqsResponseField.ENTRY_ID,
            )
        if entry_id in seen:
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.DUPLICATE,
                field=SqsResponseField.ENTRY_ID,
            )
        seen.add(entry_id)
        return entry_id
