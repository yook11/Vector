"""SQSを経由したLambdaへの入力を、IDを確定したレコードのまとまりとして読む。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from app.aws.sqs.message import (
    SqsMessage,
    SqsMessageInvalidError,
    SqsMessageInvalidReason,
)
from app.shared.errors import ApplicationError, ApplicationErrorValue


class SqsLambdaInputInvalidReason(StrEnum):
    """SQSを経由したLambdaへの入力を、レコードのまとまりとして読めない固定の理由。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    EMPTY_MESSAGE_ID = "empty_message_id"
    DUPLICATE_MESSAGE_ID = "duplicate_message_id"


class SqsLambdaInputInvalidError(ApplicationError):
    """Lambdaへの入力を読めない位置と理由を、入力値を持たずに伝える。"""

    def __init__(
        self,
        *,
        reason: SqsLambdaInputInvalidReason,
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


class ReceiptHandleInvalidReason(StrEnum):
    """レコードの受信ハンドルを可視性変更に使えない固定の理由。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    EMPTY_RECEIPT_HANDLE = "empty_receipt_handle"


class ReceiptHandleInvalidError(ApplicationError):
    """受信ハンドルを使えない理由を、入力値を持たずに伝える。"""

    def __init__(self, *, reason: ReceiptHandleInvalidReason) -> None:
        super().__init__(
            f"Receipt handle validation failed: {reason.value}",
            details={"reason": reason.value, "field": "receiptHandle"},
        )
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SqsLambdaRecord:
    """Lambdaが渡したRecordsの1件で、IDだけを確定し、本文と受信ハンドルは使うときに検証する。"""

    message_id: str
    _raw_record: Mapping[object, object] = field(repr=False)

    @classmethod
    def from_input(cls, raw_record: object, *, record_index: int) -> Self:
        """本文より先にIDを確定し、確定できなければLambdaへの入力全体を不正とする。"""
        if not isinstance(raw_record, dict):
            raise SqsLambdaInputInvalidError(
                reason=SqsLambdaInputInvalidReason.INVALID_TYPE,
                field="record",
                record_index=record_index,
            )
        if "messageId" not in raw_record:
            raise SqsLambdaInputInvalidError(
                reason=SqsLambdaInputInvalidReason.MISSING_REQUIRED_FIELD,
                field="messageId",
                record_index=record_index,
            )
        message_id = raw_record["messageId"]
        if not isinstance(message_id, str):
            reason = SqsLambdaInputInvalidReason.INVALID_TYPE
        elif not message_id.strip():
            reason = SqsLambdaInputInvalidReason.EMPTY_MESSAGE_ID
        else:
            # 呼び出し側が元の入力を書き換えても読む値が変わらないよう、複製して持つ。
            return cls(message_id, MappingProxyType(dict(raw_record)))
        raise SqsLambdaInputInvalidError(
            reason=reason, field="messageId", record_index=record_index
        )

    def read_message(self) -> SqsMessage:
        """本文の存在と文字列型を検証し、IDと本文をSQSメッセージにする。"""
        if "body" not in self._raw_record:
            raise SqsMessageInvalidError(
                reason=SqsMessageInvalidReason.MISSING_REQUIRED_FIELD,
                field="body",
            )
        body = self._raw_record["body"]
        if not isinstance(body, str):
            raise SqsMessageInvalidError(
                reason=SqsMessageInvalidReason.INVALID_TYPE, field="body"
            )
        return SqsMessage(message_id=self.message_id, body=body)

    def read_receipt_handle(self) -> str:
        """可視性変更が必要なときだけ、受信時の操作情報を検証する。"""
        if "receiptHandle" not in self._raw_record:
            reason = ReceiptHandleInvalidReason.MISSING_REQUIRED_FIELD
        else:
            receipt_handle = self._raw_record["receiptHandle"]
            if not isinstance(receipt_handle, str):
                reason = ReceiptHandleInvalidReason.INVALID_TYPE
            elif not receipt_handle.strip():
                reason = ReceiptHandleInvalidReason.EMPTY_RECEIPT_HANDLE
            else:
                return receipt_handle
        raise ReceiptHandleInvalidError(reason=reason)


def parse_sqs_lambda_input(lambda_input: object) -> tuple[SqsLambdaRecord, ...]:
    """一覧の構造とID重複を検証し、本文と受信ハンドルの検証は各レコードを使うときに委ねる。"""
    if not isinstance(lambda_input, dict):
        raise SqsLambdaInputInvalidError(
            reason=SqsLambdaInputInvalidReason.INVALID_TYPE, field="event"
        )
    if "Records" not in lambda_input:
        raise SqsLambdaInputInvalidError(
            reason=SqsLambdaInputInvalidReason.MISSING_REQUIRED_FIELD,
            field="Records",
        )
    if not isinstance(lambda_input["Records"], list):
        raise SqsLambdaInputInvalidError(
            reason=SqsLambdaInputInvalidReason.INVALID_TYPE,
            field="Records",
        )
    records: list[SqsLambdaRecord] = []
    seen: set[str] = set()
    for index, raw_record in enumerate(lambda_input["Records"]):
        record = SqsLambdaRecord.from_input(raw_record, record_index=index)
        if record.message_id in seen:
            raise SqsLambdaInputInvalidError(
                reason=SqsLambdaInputInvalidReason.DUPLICATE_MESSAGE_ID,
                field="messageId",
                record_index=index,
            )
        seen.add(record.message_id)
        records.append(record)
    return tuple(records)
