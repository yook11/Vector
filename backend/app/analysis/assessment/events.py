from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
)
from pydantic_core import PydanticCustomError

from app.logfire.exceptions import VectorDomainError


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

    @classmethod
    def from_input(cls, data: object) -> Self:
        """入力を検証し、イベントまたは安全な検証例外を返す。"""
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            failure = assessed_event_validation_failure(exc)
        # 入力値を含むValidationErrorをcontextに残さない。
        raise AssessedEventValidationError(failure)

    @field_validator("event_id", mode="before")
    @classmethod
    def restore_event_id(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) else value

    @field_validator("occurred_at", mode="before")
    @classmethod
    def restore_occurred_at(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @field_serializer("occurred_at", when_used="json")
    @classmethod
    def serialize_occurred_at(cls, value: datetime) -> str:
        """本文ではUTCのZ表記に揃え、元の小数秒は保つ。"""
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

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


class AssessedEventValidationField(StrEnum):
    """診断に公開できる、契約で定義済みの項目。"""

    EVENT = "event"
    EVENT_ID = "event_id"
    EVENT_TYPE = "event_type"
    SCHEMA_VERSION = "schema_version"
    OCCURRED_AT = "occurred_at"
    PAYLOAD = "payload"
    CURATION_ID = "payload.curation_id"
    ANALYZED_ARTICLE_ID = "payload.analyzed_article_id"


class AssessedEventValidationCode(StrEnum):
    """入力値や検証ライブラリの自由文に依存しない違反コード。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"


@dataclass(frozen=True, slots=True)
class AssessedEventValidationIssue:
    """入力の内容を保持しない、一項目の契約違反。"""

    field: AssessedEventValidationField
    code: AssessedEventValidationCode


@dataclass(frozen=True, slots=True)
class AssessedEventValidationFailure:
    """優先する失敗理由と、重複のない安全な検証詳細。"""

    reason: AssessedEventInvalidReason
    issues: tuple[AssessedEventValidationIssue, ...]


class AssessedEventValidationError(VectorDomainError):
    """イベントの構築失敗を、安全な理由と検証詳細で伝える。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("failure",)

    def __init__(self, failure: AssessedEventValidationFailure) -> None:
        super().__init__()
        self.failure = failure


def assessed_event_validation_failure(
    error: ValidationError,
) -> AssessedEventValidationFailure:
    """外側の構造、種別、バージョン、payloadの順に分類し詳細を抽出する。"""
    reasons: set[AssessedEventInvalidReason] = set()
    issues: dict[AssessedEventValidationIssue, None] = {}
    known_paths = {
        tuple(field.value.split(".")): field for field in AssessedEventValidationField
    }
    for detail in error.errors(include_input=False, include_context=False):
        kind = detail["type"]
        location = detail["loc"]
        if kind == "unsupported_event_type":
            reasons.add(AssessedEventInvalidReason.UNSUPPORTED_EVENT_TYPE)
            code = AssessedEventValidationCode.UNSUPPORTED_EVENT_TYPE
        elif kind == "unsupported_schema_version":
            reasons.add(AssessedEventInvalidReason.UNSUPPORTED_SCHEMA_VERSION)
            code = AssessedEventValidationCode.UNSUPPORTED_SCHEMA_VERSION
        else:
            reasons.add(
                AssessedEventInvalidReason.INVALID_PAYLOAD
                if len(location) > 1 and location[0] == "payload"
                else AssessedEventInvalidReason.INVALID_ENVELOPE
            )
            if kind == "missing":
                code = AssessedEventValidationCode.MISSING_REQUIRED_FIELD
            elif kind == "extra_forbidden":
                code = AssessedEventValidationCode.UNKNOWN_FIELD
            elif kind.endswith("_type") or kind == "is_instance_of":
                code = AssessedEventValidationCode.INVALID_TYPE
            else:
                code = AssessedEventValidationCode.INVALID_VALUE
        parent = (
            AssessedEventValidationField.PAYLOAD
            if location and location[0] == "payload" and len(location) > 1
            else AssessedEventValidationField.EVENT
        )
        field = (
            parent if kind == "extra_forbidden" else known_paths.get(location, parent)
        )
        issues[AssessedEventValidationIssue(field=field, code=code)] = None
    return AssessedEventValidationFailure(
        reason=next(
            reason for reason in AssessedEventInvalidReason if reason in reasons
        ),
        issues=tuple(issues),
    )
