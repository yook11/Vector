"""SQS本文を検証し、配送情報を含まない対象内判定イベントへ復元する。"""

import json
from enum import StrEnum
from typing import ClassVar

from pydantic import ValidationError

from app.analysis.assessment.events import (
    ArticleAssessedInScopeEvent,
    assessed_event_invalid_reason,
)
from app.logfire.exceptions import VectorDomainError


class EmbeddingEventInvalidReason(StrEnum):
    """受信本文をイベントとして扱えない理由。"""

    INVALID_JSON = "invalid_json"
    INVALID_ENVELOPE = "invalid_envelope"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    INVALID_PAYLOAD = "invalid_payload"


class EmbeddingEventInvalidError(VectorDomainError):
    """本文や検証詳細を保持せず、入力不正の理由だけを返す。"""

    CODE: ClassVar[str] = "embedding_event_invalid"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "reason")

    def __init__(self, *, reason: EmbeddingEventInvalidReason) -> None:
        super().__init__()
        self.reason = reason


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("nonstandard_json_constant")


def parse_embedding_event(body: str) -> ArticleAssessedInScopeEvent:
    """JSONの解析と契約検証を行い、失敗時は安全な理由だけを伝える。"""
    if not isinstance(body, str):
        raise EmbeddingEventInvalidError(
            reason=EmbeddingEventInvalidReason.INVALID_JSON
        )
    try:
        data = json.loads(
            body, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, RecursionError):
        reason = EmbeddingEventInvalidReason.INVALID_JSON
    else:
        try:
            return ArticleAssessedInScopeEvent.model_validate(data)
        except ValidationError as exc:
            reason = EmbeddingEventInvalidReason(assessed_event_invalid_reason(exc))
    # 検証例外のcontextに入力本文を残さないよう、exceptの外で送出する。
    raise EmbeddingEventInvalidError(reason=reason)
