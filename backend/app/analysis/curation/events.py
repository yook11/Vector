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


class ArticleCuratedSignal(BaseModel):
    """Signalの整形結果を保存した場合だけ発行し、Noiseは対象にしない。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    EVENT_TYPE: ClassVar[str] = "article.curated_signal"
    SCHEMA_VERSION: ClassVar[int] = 1

    analyzable_article_id: int = Field(gt=0)
    curation_id: int = Field(gt=0)


class ArticleCuratedSignalEvent(BaseModel):
    """送受信で共有する、Signalイベント全体の契約。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    event_id: UUID
    event_type: str
    schema_version: int
    occurred_at: AwareDatetime
    payload: ArticleCuratedSignal

    @classmethod
    def from_input(cls, data: object) -> Self:
        """入力を検証し、イベントまたは安全な検証例外を返す。"""
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            failure = curated_event_validation_failure(exc)
        # 入力値を含むValidationErrorをcontextに残さない。
        raise CuratedEventValidationError(failure)

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
        if value != ArticleCuratedSignal.EVENT_TYPE:
            raise PydanticCustomError(
                "unsupported_event_type", "Unsupported event type"
            )
        return value

    @field_validator("schema_version")
    @classmethod
    def require_schema_version(cls, value: int) -> int:
        if value != ArticleCuratedSignal.SCHEMA_VERSION:
            raise PydanticCustomError(
                "unsupported_schema_version", "Unsupported schema version"
            )
        return value


class CuratedEventInvalidReason(StrEnum):
    """入力値を含めずに共有できる、イベント契約違反の理由。"""

    INVALID_ENVELOPE = "invalid_envelope"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    INVALID_PAYLOAD = "invalid_payload"


class CuratedEventValidationField(StrEnum):
    """診断に公開できる、契約で定義済みの項目。"""

    EVENT = "event"
    EVENT_ID = "event_id"
    EVENT_TYPE = "event_type"
    SCHEMA_VERSION = "schema_version"
    OCCURRED_AT = "occurred_at"
    PAYLOAD = "payload"
    CURATION_ID = "payload.curation_id"
    ANALYZABLE_ARTICLE_ID = "payload.analyzable_article_id"


class CuratedEventValidationCode(StrEnum):
    """入力値や検証ライブラリの自由文に依存しない違反コード。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"


@dataclass(frozen=True, slots=True)
class CuratedEventValidationIssue:
    """入力の内容を保持しない、一項目の契約違反。"""

    field: CuratedEventValidationField
    code: CuratedEventValidationCode


@dataclass(frozen=True, slots=True)
class CuratedEventValidationFailure:
    """優先する失敗理由と、重複のない安全な検証詳細。"""

    reason: CuratedEventInvalidReason
    issues: tuple[CuratedEventValidationIssue, ...]


class CuratedEventValidationError(VectorDomainError):
    """イベントの構築失敗を、安全な理由と検証詳細で伝える。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("failure",)

    def __init__(self, failure: CuratedEventValidationFailure) -> None:
        super().__init__()
        self.failure = failure


def curated_event_validation_failure(
    error: ValidationError,
) -> CuratedEventValidationFailure:
    """外側の構造、種別、バージョン、payloadの順に分類し詳細を抽出する。"""
    reasons: set[CuratedEventInvalidReason] = set()
    issues: dict[CuratedEventValidationIssue, None] = {}
    known_paths = {
        tuple(field.value.split(".")): field for field in CuratedEventValidationField
    }
    for detail in error.errors(include_input=False, include_context=False):
        kind = detail["type"]
        location = detail["loc"]
        if kind == "unsupported_event_type":
            reasons.add(CuratedEventInvalidReason.UNSUPPORTED_EVENT_TYPE)
            code = CuratedEventValidationCode.UNSUPPORTED_EVENT_TYPE
        elif kind == "unsupported_schema_version":
            reasons.add(CuratedEventInvalidReason.UNSUPPORTED_SCHEMA_VERSION)
            code = CuratedEventValidationCode.UNSUPPORTED_SCHEMA_VERSION
        else:
            reasons.add(
                CuratedEventInvalidReason.INVALID_PAYLOAD
                if len(location) > 1 and location[0] == "payload"
                else CuratedEventInvalidReason.INVALID_ENVELOPE
            )
            if kind == "missing":
                code = CuratedEventValidationCode.MISSING_REQUIRED_FIELD
            elif kind == "extra_forbidden":
                code = CuratedEventValidationCode.UNKNOWN_FIELD
            elif kind.endswith("_type") or kind == "is_instance_of":
                code = CuratedEventValidationCode.INVALID_TYPE
            else:
                code = CuratedEventValidationCode.INVALID_VALUE
        parent = (
            CuratedEventValidationField.PAYLOAD
            if location and location[0] == "payload" and len(location) > 1
            else CuratedEventValidationField.EVENT
        )
        field = (
            parent if kind == "extra_forbidden" else known_paths.get(location, parent)
        )
        issues[CuratedEventValidationIssue(field=field, code=code)] = None
    return CuratedEventValidationFailure(
        reason=next(
            reason for reason in CuratedEventInvalidReason if reason in reasons
        ),
        issues=tuple(issues),
    )
