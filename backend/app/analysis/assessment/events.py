from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from pydantic_core import PydanticCustomError


class ArticleAssessedInScope(BaseModel):
    """In Scopeの分析結果を保存した場合だけ発行し、Out of Scopeは対象にしない。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    EVENT_TYPE: ClassVar[str] = "article.assessed_in_scope"
    SCHEMA_VERSION: ClassVar[int] = 1

    curation_id: int = Field(gt=0)
    analyzed_article_id: int = Field(gt=0)


class ArticleAssessedInScopeEvent(BaseModel):
    """送受信で共有する、対象内判定イベント全体の契約。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    event_id: UUID
    event_type: str
    schema_version: int
    occurred_at: AwareDatetime
    payload: ArticleAssessedInScope

    @field_validator("event_id", mode="before")
    @classmethod
    def restore_event_id(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) else value

    @field_validator("occurred_at", mode="before")
    @classmethod
    def restore_occurred_at(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("event_type")
    @classmethod
    def require_event_type(cls, value: str) -> str:
        if value != ArticleAssessedInScope.EVENT_TYPE:
            raise PydanticCustomError(
                "unsupported_event_type", "Unsupported event type"
            )
        return value

    @field_validator("schema_version")
    @classmethod
    def require_schema_version(cls, value: int) -> int:
        if value != ArticleAssessedInScope.SCHEMA_VERSION:
            raise PydanticCustomError(
                "unsupported_schema_version", "Unsupported schema version"
            )
        return value


class AssessedEventInvalidReason(StrEnum):
    """入力値を含めずに共有できる、イベント契約違反の理由。"""

    INVALID_ENVELOPE = "invalid_envelope"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    INVALID_PAYLOAD = "invalid_payload"


def assessed_event_invalid_reason(error: ValidationError) -> AssessedEventInvalidReason:
    """外側の構造、種別、バージョン、payloadの順で違反理由を選ぶ。"""
    reasons = set()
    for detail in error.errors(include_input=False, include_context=False):
        if detail["type"] == "unsupported_event_type":
            reasons.add(AssessedEventInvalidReason.UNSUPPORTED_EVENT_TYPE)
        elif detail["type"] == "unsupported_schema_version":
            reasons.add(AssessedEventInvalidReason.UNSUPPORTED_SCHEMA_VERSION)
        elif len(detail["loc"]) > 1 and detail["loc"][0] == "payload":
            reasons.add(AssessedEventInvalidReason.INVALID_PAYLOAD)
        else:
            reasons.add(AssessedEventInvalidReason.INVALID_ENVELOPE)
    return next(reason for reason in AssessedEventInvalidReason if reason in reasons)
