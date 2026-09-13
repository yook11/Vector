"""SQS本文を検証し、共通記事完成イベントへ復元する。"""

import json
from enum import StrEnum
from typing import ClassVar

from app.collection.events import (
    AnalyzableArticleCreatedEvent,
    AnalyzableEventValidationError,
    AnalyzableEventValidationIssue,
)
from app.logfire.exceptions import VectorDomainError


class CurationEventInvalidReason(StrEnum):
    """受信本文をイベントとして扱えない理由。"""

    INVALID_JSON = "invalid_json"
    INVALID_ENVELOPE = "invalid_envelope"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    INVALID_PAYLOAD = "invalid_payload"


class CurationEventInvalidError(VectorDomainError):
    """本文を保持せず、入力不正の理由と安全な検証詳細を返す。"""

    CODE: ClassVar[str] = "curation_event_invalid"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "reason", "issues")

    def __init__(
        self,
        *,
        reason: CurationEventInvalidReason,
        issues: tuple[AnalyzableEventValidationIssue, ...] = (),
    ) -> None:
        super().__init__()
        self.reason = reason
        self.issues = issues


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("nonstandard_json_constant")


def parse_analyzable_article_created_event(
    message_body: str,
) -> AnalyzableArticleCreatedEvent:
    """JSONの解析と契約検証を行い、失敗時は安全な理由だけを伝える。"""
    if not isinstance(message_body, str):
        raise CurationEventInvalidError(reason=CurationEventInvalidReason.INVALID_JSON)
    issues: tuple[AnalyzableEventValidationIssue, ...] = ()
    try:
        data = json.loads(
            message_body,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        reason = CurationEventInvalidReason.INVALID_JSON
    else:
        try:
            return AnalyzableArticleCreatedEvent.from_input(data)
        except AnalyzableEventValidationError as exc:
            failure = exc.failure
            reason = CurationEventInvalidReason(failure.reason)
            issues = failure.issues
    # 検証例外のcontextに入力本文を残さないよう、exceptの外で送出する。
    raise CurationEventInvalidError(reason=reason, issues=issues)
