"""Stage 1 acquisition の監査イベントを組み立てる。"""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import AcquisitionPayload
from app.audit.error_chain import extract_error_chain
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    failure_action_value,
    project_db_failure,
    unknown_failure_projection,
)
from app.audit.repository import PipelineEventRepository
from app.collection.article_acquisition.errors import (
    FetchedArticleConversionError,
    SourceAcquisitionError,
    UnreadableResponseError,
)
from app.collection.article_acquisition.tools.http_error_translation import (
    RECOVERABLE_FETCH_ERRORS,
)
from app.collection.external_fetch_errors import ExternalFetchError
from app.shared.security.redaction import redact_secrets

_ERROR_MESSAGE_LIMIT = 2000

_ARTICLE_CREATED = "article_created"
_INCOMPLETE_ARTICLE_CREATED = "incomplete_article_created"


def _external_fetch_error_types(
    root: type[ExternalFetchError],
) -> tuple[type[ExternalFetchError], ...]:
    """``ExternalFetchError`` の具象 subclass を再帰的に列挙する。"""
    found: list[type[ExternalFetchError]] = []
    for subclass in root.__subclasses__():
        found.append(subclass)
        found.extend(_external_fetch_error_types(subclass))
    return tuple(found)


_RECOVERABLE_EXTERNAL_FETCH_CODES = {cls.CODE for cls in RECOVERABLE_FETCH_ERRORS}
_EXTERNAL_FETCH_CODES = {
    cls.CODE for cls in _external_fetch_error_types(ExternalFetchError)
}


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
        projection = self._projection_of(exc)
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
        exc: FetchedArticleConversionError,
    ) -> None:
        """per-entry 変換不能を rejected として記録する。"""
        payload = AcquisitionPayload(
            source_name=exc.source_name,
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
            conversion_observed_reason=str(exc.conversion_reason),
            conversion_raw_url=(redact_secrets(exc.raw_url) if exc.raw_url else None),
            conversion_has_title=exc.has_title,
            conversion_body_length=exc.body_length,
            conversion_has_published_at=exc.has_published_at,
        )
        await self._events.append(
            stage=Stage.ACQUISITION,
            event_type=EventType.REJECTED,
            outcome_code=exc.code,
            payload=payload,
            source_id=source_id,
            error_class=_fqn(exc),
        )

    @staticmethod
    def _projection_of(exc: BaseException) -> FailureProjection:
        """origin error code を Stage 1 の失敗属性へ投影する。"""
        if isinstance(exc, SourceAcquisitionError):
            code = _extract_outcome_code(exc)
            if code == UnreadableResponseError.CODE:
                return FailureProjection(
                    failure_kind="unreadable_response",
                    retryability=Retryability.NON_RETRYABLE,
                    failure_action=None,
                    code=code,
                    stage=Stage.ACQUISITION,
                )
            if code in _EXTERNAL_FETCH_CODES:
                retryability = (
                    Retryability.RETRYABLE
                    if code in _RECOVERABLE_EXTERNAL_FETCH_CODES
                    else Retryability.NON_RETRYABLE
                )
                return FailureProjection(
                    failure_kind="external_fetch",
                    retryability=retryability,
                    failure_action=None,
                    code=code,
                    stage=Stage.ACQUISITION,
                )
            if code.startswith("fetch_"):
                return FailureProjection(
                    failure_kind="external_fetch",
                    retryability=Retryability.UNKNOWN,
                    failure_action=None,
                    code=code,
                    stage=Stage.ACQUISITION,
                )
            return FailureProjection(
                failure_kind="source_acquisition",
                retryability=Retryability.UNKNOWN,
                failure_action=None,
                code=code,
                stage=Stage.ACQUISITION,
            )
        db = project_db_failure(exc)
        return db if db is not None else unknown_failure_projection()


def _extract_outcome_code(exc: BaseException) -> str:
    """``exc.code`` から event code を取り出し、無ければ catch-all にする。"""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else "unexpected_error"


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
