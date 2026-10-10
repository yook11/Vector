"""SQSトリガーから渡されたバッチを受信メッセージとして読み、本文と受信ハンドルは1件ずつ使うときに検証する。"""

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self

from app.shared.errors import ApplicationError, ApplicationErrorValue


class ReceivedMessageInvalidReason(StrEnum):
    """受信メッセージがこのシステムのメッセージの形を満たさない固定の理由。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    INVALID_JSON = "invalid_json"
    EMPTY_RECEIPT_HANDLE = "empty_receipt_handle"


class ReceivedMessageInvalidError(ApplicationError):
    """受信メッセージの形を満たさない項目と理由を、入力値を持たずに伝える。"""

    def __init__(self, *, reason: ReceivedMessageInvalidReason, field: str) -> None:
        super().__init__(
            f"Received message validation failed: {reason.value}",
            details={"reason": reason.value, "field": field},
        )
        self.reason = reason
        self.field = field


class ReceivedMessageBatchInvalidReason(StrEnum):
    """SQSトリガーから渡されたバッチを、受信メッセージのまとまりとして読めない固定の理由。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    EMPTY_MESSAGE_ID = "empty_message_id"
    DUPLICATE_MESSAGE_ID = "duplicate_message_id"


class ReceivedMessageBatchInvalidError(ApplicationError):
    """渡されたバッチを読めない位置と理由を、入力値を持たずに伝える。"""

    def __init__(
        self,
        *,
        reason: ReceivedMessageBatchInvalidReason,
        field: str,
        record_index: int | None = None,
    ) -> None:
        details: dict[str, ApplicationErrorValue] = {
            "reason": reason.value,
            "field": field,
        }
        if record_index is not None:
            details["record_index"] = record_index
        super().__init__(
            f"Received message batch validation failed: {reason.value}",
            details=details,
        )
        self.reason = reason
        self.field = field
        self.record_index = record_index


@dataclass(frozen=True, slots=True)
class ReceivedMessage:
    """IDで1件を特定できる受信メッセージで、本文と受信ハンドルは使うときに検証する。"""

    message_id: str
    body: object = field(repr=False)
    body_present: bool = True
    receipt_handle: object = field(default=None, repr=False)
    receipt_handle_present: bool = False

    @classmethod
    def from_sqs_record(cls, sqs_record: object, *, record_index: int) -> Self:
        """本文より先にIDを確定し、確定できなければバッチ全体を不正とする。"""
        if not isinstance(sqs_record, dict):
            raise ReceivedMessageBatchInvalidError(
                reason=ReceivedMessageBatchInvalidReason.INVALID_TYPE,
                field="record",
                record_index=record_index,
            )
        if "messageId" not in sqs_record:
            raise ReceivedMessageBatchInvalidError(
                reason=ReceivedMessageBatchInvalidReason.MISSING_REQUIRED_FIELD,
                field="messageId",
                record_index=record_index,
            )
        message_id = sqs_record["messageId"]
        if not isinstance(message_id, str):
            reason = ReceivedMessageBatchInvalidReason.INVALID_TYPE
        elif not message_id.strip():
            reason = ReceivedMessageBatchInvalidReason.EMPTY_MESSAGE_ID
        else:
            return cls(
                message_id,
                sqs_record.get("body"),
                "body" in sqs_record,
                sqs_record.get("receiptHandle"),
                "receiptHandle" in sqs_record,
            )
        raise ReceivedMessageBatchInvalidError(
            reason=reason, field="messageId", record_index=record_index
        )

    def parse_json(self) -> object:
        """本文の存在と文字列型を検証し、イベント契約を解釈せずJSONとして解析する。"""
        if not self.body_present:
            raise ReceivedMessageInvalidError(
                reason=ReceivedMessageInvalidReason.MISSING_REQUIRED_FIELD, field="body"
            )
        if not isinstance(self.body, str):
            raise ReceivedMessageInvalidError(
                reason=ReceivedMessageInvalidReason.INVALID_TYPE, field="body"
            )
        try:
            return json.loads(
                self.body,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (ValueError, RecursionError):
            pass
        # 入力を含む解析例外をcontextに残さないよう、exceptの外で送出する。
        raise ReceivedMessageInvalidError(
            reason=ReceivedMessageInvalidReason.INVALID_JSON, field="body"
        )

    def receipt_handle_text(self) -> str:
        """可視性変更が必要なときだけ、受信時の操作情報を検証する。"""
        if not self.receipt_handle_present:
            reason = ReceivedMessageInvalidReason.MISSING_REQUIRED_FIELD
        elif not isinstance(self.receipt_handle, str):
            reason = ReceivedMessageInvalidReason.INVALID_TYPE
        elif not self.receipt_handle.strip():
            reason = ReceivedMessageInvalidReason.EMPTY_RECEIPT_HANDLE
        else:
            return self.receipt_handle
        raise ReceivedMessageInvalidError(reason=reason, field="receiptHandle")


@dataclass(frozen=True, slots=True)
class ReceivedMessageBatch:
    """SQSトリガーが1回の呼び出しで渡した、IDの検証を終えたメッセージのまとまり。"""

    messages: tuple[ReceivedMessage, ...]

    @classmethod
    def from_sqs_event(cls, sqs_event: object) -> Self:
        """一覧の構造とID重複を検証し、本文検証は各メッセージの処理に委ねる。"""
        if not isinstance(sqs_event, dict):
            raise ReceivedMessageBatchInvalidError(
                reason=ReceivedMessageBatchInvalidReason.INVALID_TYPE, field="event"
            )
        if "Records" not in sqs_event:
            raise ReceivedMessageBatchInvalidError(
                reason=ReceivedMessageBatchInvalidReason.MISSING_REQUIRED_FIELD,
                field="Records",
            )
        if not isinstance(sqs_event["Records"], list):
            raise ReceivedMessageBatchInvalidError(
                reason=ReceivedMessageBatchInvalidReason.INVALID_TYPE, field="Records"
            )
        messages = []
        seen: set[str] = set()
        for index, sqs_record in enumerate(sqs_event["Records"]):
            message = ReceivedMessage.from_sqs_record(sqs_record, record_index=index)
            if message.message_id in seen:
                raise ReceivedMessageBatchInvalidError(
                    reason=ReceivedMessageBatchInvalidReason.DUPLICATE_MESSAGE_ID,
                    field="messageId",
                    record_index=index,
                )
            seen.add(message.message_id)
            messages.append(message)
        return cls(messages=tuple(messages))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("nonstandard_json_constant")
