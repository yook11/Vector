"""Stage 1 acquisition の監査イベントを組み立てる。"""

from __future__ import annotations

from enum import StrEnum
from typing import TypedDict

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import AcquisitionPayload
from app.audit.error_chain import extract_error_chain
from app.audit.error_fields import exception_fqn, redacted_audit_message
from app.audit.failure_projection import (
    FailureProjection,
    failure_action_value,
    project_failure,
    unknown_failure_projection,
)
from app.audit.repository import PipelineEventRepository
from app.collection.article_acquisition.errors import AcquisitionError
from app.collection.article_acquisition.fetched_article_converter import (
    AcquisitionConversionRejection,
)
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
)
from app.collection.external_fetch_errors import ExternalFetchError
from app.shared.security.redaction import redact_secrets


class AcquisitionOutcomeCode(StrEnum):
    """Stage.ACQUISITION の outcome code (stage ファイル内定義分のみ)。"""

    ARTICLE_CREATED = "article_created"
    INCOMPLETE_ARTICLE_CREATED = "incomplete_article_created"


class SourceAcquisitionAuditRepository:
    """Stage 1 専用の payload / outcome_code / failure projection を決める。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    async def append_article_created(
        self,
        *,
        source_id: int | None,
        source_name: str | None,
        analyzable_article_id: int,
        canonical_url: str,
    ) -> None:
        """即時獲得成功を記録する。"""
        payload = AcquisitionPayload(
            source_name=source_name, canonical_url=canonical_url
        )
        await self._events.append(
            stage=Stage.ACQUISITION,
            event_type=EventType.SUCCEEDED,
            outcome_code=AcquisitionOutcomeCode.ARTICLE_CREATED.value,
            payload=payload,
            article_id=analyzable_article_id,
            source_id=source_id,
            error_class=None,
        )

    async def append_incomplete_article_created(
        self,
        *,
        source_id: int | None,
        source_name: str | None,
        canonical_url: str,
    ) -> None:
        """補完待ち投入成功を記録する。"""
        payload = AcquisitionPayload(
            source_name=source_name, canonical_url=canonical_url
        )
        await self._events.append(
            stage=Stage.ACQUISITION,
            event_type=EventType.SUCCEEDED,
            outcome_code=AcquisitionOutcomeCode.INCOMPLETE_ARTICLE_CREATED.value,
            payload=payload,
            article_id=None,
            source_id=source_id,
            error_class=None,
        )

    async def append_failure(
        self,
        *,
        source_id: int | None,
        source_name: str | None,
        exc: AcquisitionError | SQLAlchemyError,
    ) -> None:
        """source 全体の acquisition 失敗を記録する。"""
        projection = project_failure(exc, fallback_code="unexpected_error")
        await self._append_failed_event(
            source_id=source_id,
            source_name=source_name,
            exc=exc,
            projection=projection,
        )

    async def append_unexpected_failure(
        self,
        *,
        source_id: int | None,
        source_name: str | None,
        exc: BaseException,
    ) -> None:
        """想定外の acquisition 失敗を unknown として記録する。"""
        await self._append_failed_event(
            source_id=source_id,
            source_name=source_name,
            exc=exc,
            projection=unknown_failure_projection(),
        )

    async def _append_failed_event(
        self,
        *,
        source_id: int | None,
        source_name: str | None,
        exc: BaseException,
        projection: FailureProjection,
    ) -> None:
        payload = AcquisitionPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            source_name=source_name,
            error_message=redacted_audit_message(_error_message(exc)),
            error_chain=extract_error_chain(exc),
            **_origin_payload_fields(exc),
        )
        await self._events.append(
            stage=projection.stage or Stage.ACQUISITION,
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=payload,
            source_id=source_id,
            error_class=exception_fqn(exc),
            retryability=projection.retryability,
        )

    async def append_conversion_rejected(
        self,
        *,
        source_id: int | None,
        rejection: AcquisitionConversionRejection,
    ) -> None:
        """per-entry 変換不能を rejected として記録する。

        ``outcome_code`` は責任元 VO の reason を verbatim で焼く (URL=SafeUrl 由来 /
        title 欠落・想定外=acquisition 由来)。``error_class`` / ``error_chain`` は
        原因例外 ``cause`` から導く (title 欠落は cause 無しなので NULL)。
        """
        cause = rejection.cause
        payload = AcquisitionPayload(
            source_name=rejection.source_name,
            error_chain=extract_error_chain(cause) if cause is not None else None,
            conversion_raw_url=(
                redact_secrets(rejection.raw_url) if rejection.raw_url else None
            ),
            conversion_has_title=rejection.has_title,
            conversion_body_length=rejection.body_length,
            conversion_has_published_at=rejection.has_published_at,
        )
        await self._events.append(
            stage=Stage.ACQUISITION,
            event_type=EventType.REJECTED,
            outcome_code=rejection.outcome_code,
            payload=payload,
            source_id=source_id,
            error_class=exception_fqn(cause) if cause is not None else None,
        )


class _OriginPayloadFields(TypedDict):
    """marker の origin specifics を構造化 payload 列へ展開する keyword (固定 key set)。

    key set を TypedDict で固定し ``**`` 展開で任意 keyword へ流入しないようにする
    (``failure_payload_fields`` と同じ理由)。read 側 3 (format / field / position) と
    fetch 側 3 (status / reason / retry_after) を持ち、該当しない側は None。
    """

    read_format: str | None
    read_field: str | None
    read_parser_position: str | None
    http_status: int | None
    fetch_reason: str | None
    fetch_retry_after_seconds: float | None


def _origin_payload_fields(exc: BaseException) -> _OriginPayloadFields:
    """統合 marker の origin specifics を構造化 payload 列へ写す。

    ``exc.origin`` が ``UnreadableResponseError`` なら read_* を、``ExternalFetchError``
    なら http_status / fetch_* を載せる。fetch subclass は status_code / reason /
    retry_after の有無が異なるため getattr+None で拾う。origin に当たらない例外
    (DB / 想定外) は全列 None で payload に影響しない (outcome_code = CODE とは別に、
    後から壊れた形式 / status / reason を復元できるようにする consumer 側整形)。
    """
    origin = getattr(exc, "origin", None)
    if isinstance(origin, UnreadableResponseError):
        return {
            "read_format": origin.response_format,
            "read_field": origin.field,
            "read_parser_position": origin.parser_position,
            "http_status": None,
            "fetch_reason": None,
            "fetch_retry_after_seconds": None,
        }
    if isinstance(origin, ExternalFetchError):
        return {
            "read_format": None,
            "read_field": None,
            "read_parser_position": None,
            "http_status": getattr(origin, "status_code", None),
            "fetch_reason": getattr(origin, "reason", None),
            "fetch_retry_after_seconds": getattr(origin, "retry_after_seconds", None),
        }
    return {
        "read_format": None,
        "read_field": None,
        "read_parser_position": None,
        "http_status": None,
        "fetch_reason": None,
        "fetch_retry_after_seconds": None,
    }


def _error_message(exc: BaseException) -> str:
    """error_message に焼く文字列を決める。

    統合 marker は origin の ``_default_message`` (PII-free な自己記述) を採り、explicit
    message に載りうる secret (SSRF guard 等) を ``str(origin)`` 経由で漏らさない。
    origin を持たない例外 (DB / 想定外) は ``str(exc)`` に退避する。
    """
    origin = getattr(exc, "origin", None)
    if isinstance(origin, ExternalFetchError | UnreadableResponseError):
        return origin._default_message()  # noqa: SLF001 (PII-free 既定の意図的利用)
    return str(exc)
