"""SQSレコード群の構造を検証し、本文を個別に取り出す。"""

import json
from dataclasses import dataclass, field
from typing import Self

from app.lambda_handlers.sqs.errors import (
    SqsInputError,
    SqsInputReason,
    SqsMessageJsonInvalidError,
)


@dataclass(frozen=True, slots=True)
class SqsRecordInput:
    """IDを確認済みで、本文はまだ検証していない受信データ。"""

    message_id: str
    body: object = field(repr=False)
    body_present: bool
    receipt_handle: object = field(default=None, repr=False)
    receipt_handle_present: bool = False

    @classmethod
    def from_lambda_record(cls, record: object, *, record_index: int) -> Self:
        """本文の検証より先に、失敗応答に使うIDを確定する。"""
        if not isinstance(record, dict):
            raise SqsInputError(
                reason=SqsInputReason.INVALID_TYPE,
                field="record",
                record_index=record_index,
            )
        if "messageId" not in record:
            raise SqsInputError(
                reason=SqsInputReason.MISSING_REQUIRED_FIELD,
                field="messageId",
                record_index=record_index,
            )
        message_id = record["messageId"]
        if not isinstance(message_id, str):
            reason = SqsInputReason.INVALID_TYPE
        elif not message_id.strip():
            reason = SqsInputReason.EMPTY_MESSAGE_ID
        else:
            return cls(
                message_id,
                record.get("body"),
                "body" in record,
                record.get("receiptHandle"),
                "receiptHandle" in record,
            )
        raise SqsInputError(reason=reason, field="messageId", record_index=record_index)

    def to_record(self) -> "SqsRecord":
        """本文の存在と文字列型を検証して、解析可能なレコードを返す。"""
        if not self.body_present:
            reason = SqsInputReason.MISSING_REQUIRED_FIELD
        elif not isinstance(self.body, str):
            reason = SqsInputReason.INVALID_TYPE
        else:
            return SqsRecord(
                message_id=self.message_id,
                body=self.body,
                receipt_handle=self.receipt_handle,
                receipt_handle_present=self.receipt_handle_present,
            )
        raise SqsInputError(reason=reason, field="body")


@dataclass(frozen=True, slots=True)
class SqsRecord:
    """本文の文字列型を検証済みのSQSレコード。"""

    message_id: str
    body: str = field(repr=False)
    receipt_handle: object = field(default=None, repr=False)
    receipt_handle_present: bool = False

    def parse_json(self) -> object:
        """イベント契約を解釈せず、本文をJSONとして解析する。"""
        try:
            return json.loads(
                self.body,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (ValueError, RecursionError):
            pass
        # 入力を含む解析例外をcontextに残さないよう、exceptの外で送出する。
        raise SqsMessageJsonInvalidError()

    def receipt_handle_text(self) -> str:
        """可視性変更が必要なときだけ、受信時の操作情報を検証する。"""
        if not self.receipt_handle_present:
            reason = SqsInputReason.MISSING_REQUIRED_FIELD
        elif not isinstance(self.receipt_handle, str):
            reason = SqsInputReason.INVALID_TYPE
        elif not self.receipt_handle.strip():
            reason = SqsInputReason.EMPTY_RECEIPT_HANDLE
        else:
            return self.receipt_handle
        raise SqsInputError(reason=reason, field="receiptHandle")


@dataclass(frozen=True, slots=True)
class SqsRecordBatch:
    """今回受信した、IDの検証を終えたレコードのまとまり。"""

    records: tuple[SqsRecordInput, ...]

    @classmethod
    def from_lambda_event(cls, lambda_event: object) -> Self:
        """一覧の構造とID重複を検証し、本文検証は各レコードの処理に委ねる。"""
        if not isinstance(lambda_event, dict):
            raise SqsInputError(reason=SqsInputReason.INVALID_TYPE, field="event")
        if "Records" not in lambda_event:
            raise SqsInputError(
                reason=SqsInputReason.MISSING_REQUIRED_FIELD, field="Records"
            )
        if not isinstance(lambda_event["Records"], list):
            raise SqsInputError(reason=SqsInputReason.INVALID_TYPE, field="Records")
        records = []
        seen: set[str] = set()
        for index, value in enumerate(lambda_event["Records"]):
            record = SqsRecordInput.from_lambda_record(value, record_index=index)
            if record.message_id in seen:
                raise SqsInputError(
                    reason=SqsInputReason.DUPLICATE_MESSAGE_ID,
                    field="messageId",
                    record_index=index,
                )
            seen.add(record.message_id)
            records.append(record)
        return cls(records=tuple(records))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("nonstandard_json_constant")
