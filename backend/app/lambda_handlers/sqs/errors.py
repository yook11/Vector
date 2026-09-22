"""SQS入力の構造不正を安全な診断項目で伝える。"""

from enum import StrEnum
from typing import ClassVar

from app.shared.errors import ApplicationError, ApplicationErrorValue


class SqsInputReason(StrEnum):
    """SQS配送構造の不正を示す固定の理由。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    EMPTY_RECEIPT_HANDLE = "empty_receipt_handle"
    EMPTY_MESSAGE_ID = "empty_message_id"
    DUPLICATE_MESSAGE_ID = "duplicate_message_id"


class SqsInputError(ApplicationError):
    """入力値を保持せず、配送構造の不正位置と理由を伝える。"""

    CODE: ClassVar[str] = "sqs_input_invalid"

    def __init__(
        self,
        *,
        reason: SqsInputReason,
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
            f"SQS input validation failed: {reason.value}",
            details=details,
        )
        self.reason = reason
        self.field = field
        self.record_index = record_index


class SqsMessageJsonInvalidError(ApplicationError):
    """SQS本文をJSONとして解析できない。"""

    def __init__(self) -> None:
        super().__init__(
            "SQS message JSON parsing failed",
            details={"reason": "invalid_json"},
        )
