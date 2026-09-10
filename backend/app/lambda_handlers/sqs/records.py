"""SQSレコード群の構造を検証し、本文を個別に取り出す。"""

from dataclasses import dataclass
from typing import Self

from app.lambda_handlers.sqs.errors import SqsInputError, SqsInputReason


@dataclass(frozen=True, slots=True)
class SqsRecord:
    message_id: str
    body: object
    body_present: bool

    @classmethod
    def from_input(cls, record: object, *, record_index: int) -> Self:
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
            return cls(message_id, record.get("body"), "body" in record)
        raise SqsInputError(reason=reason, field="messageId", record_index=record_index)

    def body_text(self) -> str:
        """本文の欠落と型不正を区別して、JSON解析へ渡す。"""
        if not self.body_present:
            reason = SqsInputReason.MISSING_REQUIRED_FIELD
        elif not isinstance(self.body, str):
            reason = SqsInputReason.INVALID_TYPE
        else:
            return self.body
        raise SqsInputError(reason=reason, field="body")


@dataclass(frozen=True, slots=True)
class SqsRecordBatch:
    """今回受信した、IDの検証を終えたレコードのまとまり。"""

    records: tuple[SqsRecord, ...]

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
            record = SqsRecord.from_input(value, record_index=index)
            if record.message_id in seen:
                raise SqsInputError(
                    reason=SqsInputReason.DUPLICATE_MESSAGE_ID,
                    field="messageId",
                    record_index=index,
                )
            seen.add(record.message_id)
            records.append(record)
        return cls(records=tuple(records))
