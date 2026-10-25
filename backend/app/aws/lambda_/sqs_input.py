"""SQSを経由したLambdaへの入力を、SQSメッセージのまとまりとして読む。"""

from enum import StrEnum

from app.aws.sqs.message import SqsMessage
from app.shared.errors import ApplicationError, ApplicationErrorValue


class SqsLambdaInputInvalidReason(StrEnum):
    """SQSを経由したLambdaへの入力を、メッセージのまとまりとして読めない固定の理由。"""

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


def parse_sqs_lambda_input(lambda_input: object) -> tuple[SqsMessage, ...]:
    """一覧の構造とID重複を検証し、本文検証は各メッセージの処理に委ねる。"""
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
    messages: list[SqsMessage] = []
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


def _read_sqs_record(sqs_record: object, *, record_index: int) -> SqsMessage:
    """本文より先にIDを確定し、確定できなければバッチ全体を不正とする。"""
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
        reason = SqsLambdaInputInvalidReason.INVALID_TYPE
    elif not message_id.strip():
        reason = SqsLambdaInputInvalidReason.EMPTY_MESSAGE_ID
    else:
        return SqsMessage(
            message_id=message_id,
            body=sqs_record.get("body"),
            body_present="body" in sqs_record,
            receipt_handle=sqs_record.get("receiptHandle"),
            receipt_handle_present="receiptHandle" in sqs_record,
        )
    raise SqsLambdaInputInvalidError(
        reason=reason, field="messageId", record_index=record_index
    )
