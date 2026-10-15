"""SQSへ送る本文と照合用のMD5を持ち、SQSが本文どおりに受け付けたかを照合する。"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import md5
from types import MappingProxyType

from app.aws.sqs.errors import (
    MessageBodyMismatchError,
    MessageTooLargeError,
    SqsResponseField,
    SqsResponseInvalidError,
    SqsResponseInvalidReason,
)

# SQSのメッセージ1件と、SendMessageBatch 1回分の本文合計の上限(UTF-8のバイト数)。
MAX_MESSAGE_BYTES = 1_048_576


def _required_string(
    fields: Mapping[str, object], key: str, field: SqsResponseField
) -> str:
    if key not in fields:
        raise SqsResponseInvalidError(
            reason=SqsResponseInvalidReason.MISSING_REQUIRED_FIELD, field=field
        )
    value = fields[key]
    if not isinstance(value, str):
        raise SqsResponseInvalidError(
            reason=SqsResponseInvalidReason.INVALID_TYPE, field=field
        )
    if not value:
        raise SqsResponseInvalidError(
            reason=SqsResponseInvalidReason.EMPTY_REQUIRED_FIELD, field=field
        )
    return value


@dataclass(frozen=True, slots=True)
class OutgoingSqsMessage:
    """送る本文から期待するMD5を確定し、SQSが受け付けたと返した項目がその本文かを照合する。"""

    body: str = field(repr=False)
    body_md5: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        encoded = self.body.encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise MessageTooLargeError()
        object.__setattr__(
            self, "body_md5", md5(encoded, usedforsecurity=False).hexdigest()
        )

    def verify(self, result: object) -> None:
        if not isinstance(result, dict):
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.INVALID_TYPE,
                field=SqsResponseField.RESPONSE,
            )
        _required_string(result, "MessageId", SqsResponseField.MESSAGE_ID)
        checksum = _required_string(
            result, "MD5OfMessageBody", SqsResponseField.BODY_CHECKSUM
        )
        if re.fullmatch(r"[0-9a-fA-F]{32}", checksum) is None:
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.INVALID_CHECKSUM_FORMAT,
                field=SqsResponseField.BODY_CHECKSUM,
            )
        if checksum.lower() != self.body_md5:
            raise MessageBodyMismatchError()


@dataclass(frozen=True, slots=True)
class SqsBatchSendResult:
    """送ったIDごとの結果で、どちらにも含まれないIDはSQSが本文どおりに受け付けた。"""

    body_mismatched: frozenset[str]
    rejected_codes: Mapping[str, str] = field(repr=False)
    request_id: str | None = field(repr=False)


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

    def read(self, response: object) -> SqsBatchSendResult:
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
        body_mismatched: set[str] = set()
        for entry in successful:
            if not isinstance(entry, dict):
                raise SqsResponseInvalidError(
                    reason=SqsResponseInvalidReason.INVALID_TYPE,
                    field=SqsResponseField.SUCCESSFUL_ENTRY,
                )
            entry_id = self._claim_id(entry, seen)
            try:
                self.messages[entry_id].verify(entry)
            except MessageBodyMismatchError:
                body_mismatched.add(entry_id)
        rejected_codes: dict[str, str] = {}
        for entry in failed:
            if not isinstance(entry, dict):
                raise SqsResponseInvalidError(
                    reason=SqsResponseInvalidReason.INVALID_TYPE,
                    field=SqsResponseField.FAILED_ENTRY,
                )
            entry_id = self._claim_id(entry, seen)
            if "SenderFault" not in entry:
                raise SqsResponseInvalidError(
                    reason=SqsResponseInvalidReason.MISSING_REQUIRED_FIELD,
                    field=SqsResponseField.SENDER_FAULT,
                )
            if type(entry["SenderFault"]) is not bool:
                raise SqsResponseInvalidError(
                    reason=SqsResponseInvalidReason.INVALID_TYPE,
                    field=SqsResponseField.SENDER_FAULT,
                )
            rejected_codes[entry_id] = _required_string(
                entry, "Code", SqsResponseField.ERROR_CODE
            )
        if seen != self.messages.keys():
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.MISSING_ENTRY_ID,
                field=SqsResponseField.ENTRY_ID,
            )
        metadata = response.get("ResponseMetadata")
        request_id = metadata.get("RequestId") if isinstance(metadata, dict) else None
        return SqsBatchSendResult(
            body_mismatched=frozenset(body_mismatched),
            rejected_codes=MappingProxyType(rejected_codes),
            request_id=request_id if isinstance(request_id, str) else None,
        )

    def _claim_id(self, entry: Mapping[str, object], seen: set[str]) -> str:
        entry_id = _required_string(entry, "Id", SqsResponseField.ENTRY_ID)
        if entry_id not in self.messages:
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.UNKNOWN_ENTRY_ID,
                field=SqsResponseField.ENTRY_ID,
            )
        if entry_id in seen:
            raise SqsResponseInvalidError(
                reason=SqsResponseInvalidReason.DUPLICATE_ENTRY_ID,
                field=SqsResponseField.ENTRY_ID,
            )
        seen.add(entry_id)
        return entry_id
