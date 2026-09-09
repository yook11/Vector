"""SQS応答検証とエラーマッピングで共有する、理由付きの応答違反。"""

from enum import StrEnum

from app.outbox.publishing.errors import PublishResponseInvalidReason


class SqsResponseField(StrEnum):
    """応答の不正箇所を、外部の値を含まないSQSの項目名で表す。"""

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


class InvalidSqsBatchResponse(ValueError):
    """応答の値を保持せず、違反理由と不正箇所だけを伝える。"""

    def __init__(
        self, *, reason: PublishResponseInvalidReason, field: SqsResponseField
    ) -> None:
        super().__init__()
        self.reason = reason
        self.field = field
