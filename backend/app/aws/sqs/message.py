"""IDと本文を持つSQSメッセージを表し、本文をJSONとして解析する。"""

import json
from dataclasses import dataclass
from enum import StrEnum

from app.shared.errors import ApplicationError


class SqsMessageInvalidReason(StrEnum):
    """SQSメッセージの本文を使えない固定の理由。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    INVALID_JSON = "invalid_json"


class SqsMessageInvalidError(ApplicationError):
    """SQSメッセージの使えない項目と理由を、入力値を持たずに伝える。"""

    def __init__(self, *, reason: SqsMessageInvalidReason, field: str) -> None:
        super().__init__(
            f"Received message validation failed: {reason.value}",
            details={"reason": reason.value, "field": field},
        )
        self.reason = reason
        self.field = field


@dataclass(frozen=True, slots=True)
class SqsMessage:
    """IDで1件を特定でき、本文が文字列であるSQSメッセージ。"""

    message_id: str
    body: str

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
        raise SqsMessageInvalidError(
            reason=SqsMessageInvalidReason.INVALID_JSON, field="body"
        )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("nonstandard_json_constant")
