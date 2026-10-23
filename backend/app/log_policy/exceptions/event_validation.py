"""イベントの検証境界が整理した理由と項目別診断を変換する。"""

from typing import Literal, TypedDict

from app.log_policy.exceptions.types import ConvertedException
from app.shared.event_contract import EventInvalidError


class EventValidationIssue(TypedDict):
    """検証境界で分類済みの項目と違反コード。"""

    field: str
    code: str


class EventValidationDetails(TypedDict):
    """イベント検証例外から取得する診断情報。"""

    kind: Literal["application_validation"]
    reason: str
    issues: list[EventValidationIssue]


def convert_event_validation_exception(exc: EventInvalidError) -> ConvertedException:
    """入力を含む元のメッセージを使わず、検証境界の理由と項目別診断から組み立てる。"""
    try:
        invalid = exc.invalid
        issues: list[EventValidationIssue] = []
        for issue in invalid.issues:
            issues.append({"field": issue.field.value, "code": issue.code.value})

        details: EventValidationDetails = {
            "kind": "application_validation",
            "reason": invalid.reason.value,
            "issues": issues,
        }
        return ConvertedException(
            message=f"Validation failed: {invalid.reason.value}",
            error_details=details,
        )
    except Exception:
        return ConvertedException(message="[exception message unavailable]")
