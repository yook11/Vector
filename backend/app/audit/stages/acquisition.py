"""Stage 1 acquisition の監査イベントを組み立てる。"""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import AcquisitionPayload
from app.audit.error_chain import extract_error_chain
from app.audit.failure_projection import (
    FailureProjection,
    failure_action_value,
    project_failure,
    unknown_failure_projection,
)
from app.audit.repository import PipelineEventRepository
from app.collection.article_acquisition.errors import SourceAcquisitionError
from app.collection.article_acquisition.fetched_article_converter import (
    ConversionRejection,
)
from app.shared.security.redaction import redact_secrets

_ERROR_MESSAGE_LIMIT = 2000

_ARTICLE_CREATED = "article_created"
_INCOMPLETE_ARTICLE_CREATED = "incomplete_article_created"


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
        article_id: int,
        canonical_url: str,
    ) -> None:
        """即時獲得成功を記録する。"""
        payload = AcquisitionPayload(
            source_name=source_name, canonical_url=canonical_url
        )
        await self._events.append(
            stage=Stage.ACQUISITION,
            event_type=EventType.SUCCEEDED,
            outcome_code=_ARTICLE_CREATED,
            payload=payload,
            article_id=article_id,
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
            outcome_code=_INCOMPLETE_ARTICLE_CREATED,
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
        exc: SourceAcquisitionError | SQLAlchemyError,
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
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=projection.stage or Stage.ACQUISITION,
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=payload,
            source_id=source_id,
            error_class=_fqn(exc),
            retryability=projection.retryability,
        )

    async def append_conversion_rejected(
        self,
        *,
        source_id: int | None,
        rejection: ConversionRejection,
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
            error_class=_fqn(cause) if cause is not None else None,
        )


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
