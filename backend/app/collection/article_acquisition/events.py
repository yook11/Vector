from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Self
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


class ArticleAcquired(BaseModel):
    """分析可能な記事の保存が確定し、整形へ進める場合のイベント。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    EVENT_TYPE: ClassVar[str] = "article.acquired"
    SCHEMA_VERSION: ClassVar[int] = 1

    source_id: int = Field(gt=0)
    analyzable_article_id: int = Field(gt=0)


class IncompleteArticleRecorded(BaseModel):
    """本文不足の記事の保存が確定し、本文補完へ進める場合のイベント。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    EVENT_TYPE: ClassVar[str] = "article.incomplete_recorded"
    SCHEMA_VERSION: ClassVar[int] = 1

    source_id: int = Field(gt=0)
    incomplete_article_id: int = Field(gt=0)


class IncompleteArticleRecordedEvent(BaseModel):
    """未完成記事の保存が確定したイベント全体の契約。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    event_id: UUID
    event_type: str
    schema_version: int
    occurred_at: AwareDatetime
    payload: IncompleteArticleRecorded

    @classmethod
    def from_input(cls, data: object) -> Self:
        """入力を検証してイベントを構築し、違反は工程の検証例外にする。"""
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            invalid = cls._event_invalid_from_validation_error(exc)
        # 入力を含む検証例外をcontextに残さないよう、exceptの外で送出する。
        raise IncompleteArticleEventInvalidError(invalid)

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
        if value != IncompleteArticleRecorded.EVENT_TYPE:
            raise PydanticCustomError(
                "unsupported_event_type", "Unsupported event type"
            )
        return value

    @field_validator("schema_version")
    @classmethod
    def require_schema_version(cls, value: int) -> int:
        if value != IncompleteArticleRecorded.SCHEMA_VERSION:
            raise PydanticCustomError(
                "unsupported_schema_version", "Unsupported schema version"
            )
        return value

    @staticmethod
    def _event_invalid_from_validation_error(
        error: ValidationError,
    ) -> IncompleteArticleEventInvalid:
        """外側の構造・種類・version・payloadの順に安全な理由を選ぶ。"""
        reasons: set[IncompleteArticleEventInvalidReason] = set()
        issues: dict[IncompleteArticleEventInvalidIssue, None] = {}
        known_paths = {
            tuple(field.value.split(".")): field
            for field in IncompleteArticleEventInvalidField
        }
        for detail in error.errors(include_input=False, include_context=False):
            kind = detail["type"]
            location = detail["loc"]
            if kind == "unsupported_event_type":
                reasons.add(IncompleteArticleEventInvalidReason.UNSUPPORTED_EVENT_TYPE)
                code = IncompleteArticleEventInvalidCode.UNSUPPORTED_EVENT_TYPE
            elif kind == "unsupported_schema_version":
                reasons.add(
                    IncompleteArticleEventInvalidReason.UNSUPPORTED_SCHEMA_VERSION
                )
                code = IncompleteArticleEventInvalidCode.UNSUPPORTED_SCHEMA_VERSION
            else:
                reasons.add(
                    IncompleteArticleEventInvalidReason.INVALID_PAYLOAD
                    if len(location) > 1 and location[0] == "payload"
                    else IncompleteArticleEventInvalidReason.INVALID_ENVELOPE
                )
                if kind == "missing":
                    code = IncompleteArticleEventInvalidCode.MISSING_REQUIRED_FIELD
                elif kind == "extra_forbidden":
                    code = IncompleteArticleEventInvalidCode.UNKNOWN_FIELD
                elif kind.endswith("_type") or kind == "is_instance_of":
                    code = IncompleteArticleEventInvalidCode.INVALID_TYPE
                else:
                    code = IncompleteArticleEventInvalidCode.INVALID_VALUE
            parent = (
                IncompleteArticleEventInvalidField.PAYLOAD
                if len(location) > 1 and location[0] == "payload"
                else IncompleteArticleEventInvalidField.EVENT
            )
            field = (
                parent
                if kind == "extra_forbidden"
                else known_paths.get(location, parent)
            )
            issues[IncompleteArticleEventInvalidIssue(field=field, code=code)] = None
        return IncompleteArticleEventInvalid(
            reason=next(
                reason
                for reason in IncompleteArticleEventInvalidReason
                if reason in reasons
            ),
            issues=tuple(issues),
        )


class IncompleteArticleEventInvalidReason(StrEnum):
    INVALID_ENVELOPE = "invalid_envelope"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    INVALID_PAYLOAD = "invalid_payload"


class IncompleteArticleEventInvalidField(StrEnum):
    EVENT = "event"
    EVENT_ID = "event_id"
    EVENT_TYPE = "event_type"
    SCHEMA_VERSION = "schema_version"
    OCCURRED_AT = "occurred_at"
    PAYLOAD = "payload"
    SOURCE_ID = "payload.source_id"
    INCOMPLETE_ARTICLE_ID = "payload.incomplete_article_id"


class IncompleteArticleEventInvalidCode(StrEnum):
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"


@dataclass(frozen=True, slots=True)
class IncompleteArticleEventInvalidIssue:
    field: IncompleteArticleEventInvalidField
    code: IncompleteArticleEventInvalidCode


@dataclass(frozen=True, slots=True)
class IncompleteArticleEventInvalid:
    """優先する失敗理由と、重複のない検証詳細。"""

    reason: IncompleteArticleEventInvalidReason
    issues: tuple[IncompleteArticleEventInvalidIssue, ...]


class IncompleteArticleEventInvalidError(Exception):
    """未完成記事の保存イベントの契約違反を伝える。"""

    def __init__(self, invalid: IncompleteArticleEventInvalid) -> None:
        super().__init__()
        self.invalid = invalid
