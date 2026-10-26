"""SQSを経由したLambdaへの入力を、受信したSQSメッセージのまとまりへ変換する。"""

from enum import StrEnum

from app.aws.sqs.message import (
    InvalidSqsMessage,
    ReceivedSqsMessage,
    SqsMessage,
    SqsMessageInvalidError,
    SqsMessageInvalidReason,
)
from app.shared.errors import ApplicationError, ApplicationErrorValue


class SqsLambdaInputInvalidReason(StrEnum):
    """SQSを経由したLambdaへの入力を、メッセージのまとまりとして読めない固定の理由。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    EMPTY_MESSAGE_ID = "empty_message_id"
    EMPTY_RECEIPT_HANDLE = "empty_receipt_handle"
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


def parse_sqs_lambda_input(lambda_input: object) -> tuple[ReceivedSqsMessage, ...]:
    """構造と全IDを検証して1件ずつSQSメッセージへ変換し、本文の不正はその1件の結果として返す。"""
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
    messages: list[ReceivedSqsMessage] = []
    seen: set[str] = set()
    for index, sqs_record in enumerate(lambda_input["Records"]):
        message = _read_sqs_record(sqs_record, record_index=index)
        if message.message_id in seen:
            raise SqsLambdaInputInvalidError(
                reason=SqsLambdaInputInvalidReason.DUPLICATE_MESSAGE_ID,
                field="messageId",
                record_index=index,
            )
        seen.add(message.message_id)
        messages.append(message)
    return tuple(messages)


def _read_sqs_record(sqs_record: object, *, record_index: int) -> ReceivedSqsMessage:
    """SQSが付けるIDと受信ハンドルが不正なら入力全体を不正とし、送り手が書く本文を使えなければその1件を本文不正とする。"""
    if not isinstance(sqs_record, dict):
        raise SqsLambdaInputInvalidError(
            reason=SqsLambdaInputInvalidReason.INVALID_TYPE,
            field="record",
            record_index=record_index,
        )
    if "messageId" not in sqs_record:
        raise SqsLambdaInputInvalidError(
            reason=SqsLambdaInputInvalidReason.MISSING_REQUIRED_FIELD,
            field="messageId",
            record_index=record_index,
        )
    message_id = sqs_record["messageId"]
    if not isinstance(message_id, str):
        raise SqsLambdaInputInvalidError(
            reason=SqsLambdaInputInvalidReason.INVALID_TYPE,
            field="messageId",
            record_index=record_index,
        )
    if not message_id.strip():
        raise SqsLambdaInputInvalidError(
            reason=SqsLambdaInputInvalidReason.EMPTY_MESSAGE_ID,
            field="messageId",
            record_index=record_index,
        )

    if "receiptHandle" not in sqs_record:
        raise SqsLambdaInputInvalidError(
            reason=SqsLambdaInputInvalidReason.MISSING_REQUIRED_FIELD,
            field="receiptHandle",
            record_index=record_index,
        )
    receipt_handle = sqs_record["receiptHandle"]
    if not isinstance(receipt_handle, str):
        raise SqsLambdaInputInvalidError(
            reason=SqsLambdaInputInvalidReason.INVALID_TYPE,
            field="receiptHandle",
            record_index=record_index,
        )
    if not receipt_handle.strip():
        raise SqsLambdaInputInvalidError(
            reason=SqsLambdaInputInvalidReason.EMPTY_RECEIPT_HANDLE,
            field="receiptHandle",
            record_index=record_index,
        )

    body = sqs_record.get("body")
    if "body" not in sqs_record:
        reason = SqsMessageInvalidReason.MISSING_REQUIRED_FIELD
    elif not isinstance(body, str):
        reason = SqsMessageInvalidReason.INVALID_TYPE
    else:
        return SqsMessage(
            message_id=message_id, receipt_handle=receipt_handle, body=body
        )
    return InvalidSqsMessage(
        message_id=message_id,
        receipt_handle=receipt_handle,
        error=SqsMessageInvalidError(reason=reason, field="body"),
    )
