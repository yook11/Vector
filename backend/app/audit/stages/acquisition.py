"""Stage 1 acquisition の監査イベントを組み立てる。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import (
    AcquisitionPayload,
    BasePipelineEventPayload,
    RssFeedFailurePayload,
)
from app.audit.error_chain import extract_error_chain
from app.audit.error_fields import exception_fqn, redacted_audit_message
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    failure_action_value,
    project_db_failure,
    unknown_failure_projection,
)
from app.audit.repository import PipelineEventRepository
from app.collection.article_acquisition.errors import RssFeedErrors
from app.collection.article_acquisition.fetched_article_converter import (
    AcquisitionConversionRejection,
)
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
)
from app.collection.external_fetch_failure import (
    RetryableFetchFailure,
    classify_external_fetch_failure,
)
from app.http.destination_policy import HostBlockedError
from app.http.errors import HttpResponseError, HttpTransportError
from app.shared.security.redaction import redact_secrets


class AcquisitionOutcomeCode(StrEnum):
    """Stage.ACQUISITION の outcome code (stage ファイル内定義分のみ)。"""

    ARTICLE_CREATED = "article_created"
    INCOMPLETE_ARTICLE_CREATED = "incomplete_article_created"


class SourceAcquisitionAuditRepository:
    """Stage 1 専用の payload / outcome_code / failure projection を決める。"""

    STAGE: ClassVar[Stage] = Stage.ACQUISITION

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
        await self._append_event(
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
        await self._append_event(
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
        exc: Exception,
    ) -> None:
        """source 全体の acquisition 失敗を記録する。"""
        now = datetime.now(UTC)
        projection = _project_failure(exc, now=now)
        payload = AcquisitionPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            source_name=source_name,
            error_message=_error_message(exc),
            error_chain=extract_error_chain(exc),
            feed_failures=_feed_failure_payloads(exc, now=now),
            **_failure_details(exc),
        )
        await self._append_event(
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
        await self._append_event(
            event_type=EventType.REJECTED,
            outcome_code=rejection.outcome_code,
            payload=payload,
            source_id=source_id,
            error_class=exception_fqn(cause) if cause is not None else None,
        )

    async def _append_event(
        self,
        *,
        event_type: EventType,
        outcome_code: str,
        payload: BasePipelineEventPayload,
        article_id: int | None = None,
        source_id: int | None = None,
        error_class: str | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        await self._events.append(
            stage=self.STAGE,
            event_type=event_type,
            outcome_code=outcome_code,
            payload=payload,
            article_id=article_id,
            source_id=source_id,
            error_class=error_class,
            retryability=retryability,
        )


def _project_failure(exc: Exception, *, now: datetime) -> FailureProjection:
    """取得失敗を監査の分類へ写し、HTTP起因の再試行可否は外部取得の失敗判断に従う。"""
    if isinstance(exc, RssFeedErrors):
        retryable = any(
            _project_failure(failure.error, now=now).retryability
            is Retryability.RETRYABLE
            for failure in exc.failures
        )
        return FailureProjection(
            failure_kind="rss_feeds",
            retryability=(
                Retryability.RETRYABLE if retryable else Retryability.NON_RETRYABLE
            ),
            failure_action=None,
            code=exc.CODE,
        )
    if isinstance(exc, UnreadableResponseError):
        return FailureProjection(
            failure_kind="unreadable_response",
            retryability=Retryability.NON_RETRYABLE,
            failure_action=None,
            code=exc.CODE,
        )
    fetch_failure = classify_external_fetch_failure(exc, now=now)
    if fetch_failure is not None:
        return FailureProjection(
            failure_kind="external_fetch",
            retryability=(
                Retryability.RETRYABLE
                if isinstance(fetch_failure, RetryableFetchFailure)
                else Retryability.NON_RETRYABLE
            ),
            failure_action=None,
            code=fetch_failure.code,
        )
    return project_db_failure(exc) or unknown_failure_projection()


class _FailureDetails(TypedDict):
    """失敗の事実を構造化 payload 列へ展開する keyword (固定 key set)。

    key set を TypedDict で固定し ``**`` 展開で任意 keyword へ流入しないようにする。
    該当しない列は None に保つ。
    """

    http_status: int | None
    reason_code: str | None
    read_format: str | None
    read_field: str | None
    read_parser_position: str | None


def _failure_details(exc: BaseException) -> _FailureDetails:
    """応答のstatus・通信失敗の理由・読取失敗の位置を、outcome_code とは別に残す。"""
    read = exc if isinstance(exc, UnreadableResponseError) else None
    return {
        "http_status": exc.status_code if isinstance(exc, HttpResponseError) else None,
        "reason_code": (
            exc.failure.reason.value if isinstance(exc, HttpTransportError) else None
        ),
        "read_format": read.response_format if read is not None else None,
        "read_field": read.field if read is not None else None,
        "read_parser_position": read.parser_position if read is not None else None,
    }


def _error_message(exc: BaseException) -> str | None:
    """error_message に焼く文字列を決める。

    共通HTTPエラーと宛先拒否は自由文を載せず、事実は構造化列に残す。読取失敗は
    PII-free な既定メッセージを採り、explicit message に載りうる secret を漏らさない。
    それ以外 (DB / 想定外) は ``str(exc)`` に退避する。
    """
    if isinstance(exc, HttpResponseError | HttpTransportError | HostBlockedError):
        return None
    if isinstance(exc, UnreadableResponseError):
        return redacted_audit_message(exc._default_message())  # noqa: SLF001 (PII-free 既定の意図的利用)
    return redacted_audit_message(str(exc))


def _feed_failure_payloads(
    exc: BaseException, *, now: datetime
) -> list[RssFeedFailurePayload] | None:
    """各フィードの元の原因を既存の秘匿規則で監査へ写す。"""
    if not isinstance(exc, RssFeedErrors):
        return None
    payloads: list[RssFeedFailurePayload] = []
    for failure in exc.failures:
        details = _failure_details(failure.error)
        payloads.append(
            RssFeedFailurePayload(
                feed_url=redact_secrets(failure.feed_url),
                code=_project_failure(failure.error, now=now).code,
                error_class=exception_fqn(failure.error),
                error_message=_error_message(failure.error),
                error_chain=extract_error_chain(failure.error),
                http_status=details["http_status"],
                reason_code=details["reason_code"],
            )
        )
    return payloads
