"""SQSメッセージの本文と受信ハンドルを、1件ずつ使うときに検証する。"""

import json
from dataclasses import dataclass, field
from enum import StrEnum

from app.shared.errors import ApplicationError


class SqsMessageInvalidReason(StrEnum):
    """受信したSQSメッセージの本文または受信ハンドルを使えない固定の理由。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    INVALID_JSON = "invalid_json"
    EMPTY_RECEIPT_HANDLE = "empty_receipt_handle"


class SqsMessageInvalidError(ApplicationError):
    """受信したSQSメッセージの使えない項目と理由を、入力値を持たずに伝える。"""

    def __init__(self, *, reason: SqsMessageInvalidReason, field: str) -> None:
        super().__init__(
            f"Received message validation failed: {reason.value}",
            details={"reason": reason.value, "field": field},
        )
        self.reason = reason
        self.field = field


@dataclass(frozen=True, slots=True)
class SqsMessage:
    """IDで1件を特定できるSQSメッセージで、本文と受信ハンドルは使うときに検証する。"""

    message_id: str
    body: object
    body_present: bool = True
    receipt_handle: object = field(default=None, repr=False)
    receipt_handle_present: bool = False

    def parse_json(self) -> object:
        """本文の存在と文字列型を検証し、イベント契約を解釈せずJSONとして解析する。"""
        if not self.body_present:
            raise SqsMessageInvalidError(
                reason=SqsMessageInvalidReason.MISSING_REQUIRED_FIELD,
                field="body",
            )
        if not isinstance(self.body, str):
            raise SqsMessageInvalidError(
                reason=SqsMessageInvalidReason.INVALID_TYPE, field="body"
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
        raise SqsMessageInvalidError(
            reason=SqsMessageInvalidReason.INVALID_JSON, field="body"
        )

    def receipt_handle_text(self) -> str:
        """可視性変更が必要なときだけ、受信時の操作情報を検証する。"""
        if not self.receipt_handle_present:
            reason = SqsMessageInvalidReason.MISSING_REQUIRED_FIELD
        elif not isinstance(self.receipt_handle, str):
            reason = SqsMessageInvalidReason.INVALID_TYPE
        elif not self.receipt_handle.strip():
            reason = SqsMessageInvalidReason.EMPTY_RECEIPT_HANDLE
        else:
            return self.receipt_handle
        raise SqsMessageInvalidError(reason=reason, field="receiptHandle")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("nonstandard_json_constant")
