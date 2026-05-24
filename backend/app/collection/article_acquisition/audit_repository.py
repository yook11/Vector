"""Stage 1 (article_acquisition) 専用の pipeline_events 監査リポジトリ。

ソース全体失敗 (``append_failure``、FAILED) と per-entry 変換棄却
(``append_conversion_rejected``、REJECTED) の 2 経路。``category`` は常に
``NULL``。tx 境界は呼出側が握る (本 class は commit しない)。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.article_acquisition.errors import FetchedArticleConversionError
from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import AcquisitionPayload
from app.observability.recording import _extract_error_chain
from app.observability.redact import redact_secrets
from app.observability.repository import PipelineEventRepository

_ERROR_MESSAGE_LIMIT = 2000  # foundation 共通 (Extraction / Assessment と同値)


class SourceAcquisitionAuditRepository:
    """Stage 1 監査 row の semantic API。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    async def append_failure(
        self,
        *,
        source_id: int | None,
        source_name: str | None,
        exc: BaseException,
        attempt: int,
    ) -> None:
        """Stage 1 失敗経路の audit を 1 行記録する。

        ``code`` は ``exc`` の instance 属性 ``code`` から導出する。
        ``error_chain`` は ``__cause__`` 連鎖を FQN 列に残す。commit は呼出側。
        """
        code = _code_of(exc)
        payload = AcquisitionPayload(
            source_name=source_name,
            # exception message に混入しうる secret を redact してから永続化する。
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=_extract_error_chain(exc),
        )
        await self._events.append(
            stage=Stage.ACQUISITION,
            event_type=EventType.FAILED,
            outcome_code=code,
            payload=payload,
            source_id=source_id,
            attempt=attempt,
            error_class=_fqn(exc),
            category=None,
            code=code,
        )

    async def append_conversion_rejected(
        self,
        *,
        source_id: int | None,
        exc: FetchedArticleConversionError,
        attempt: int,
    ) -> None:
        """per-entry 変換不能 entry の棄却を 1 行記録する。

        ``event_type`` は常に ``REJECTED``。``conversion_raw_url`` は URL query
        に token 混入の可能性があるため ``redact_secrets`` を通す。commit は
        呼出側。
        """
        payload = AcquisitionPayload(
            source_name=exc.source_name,
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=_extract_error_chain(exc),
            # ``conversion_analyzable_reason`` カラムは新コードでは未使用
            # (NULL)。DB 列は legacy row との互換のため据え置き。
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
            attempt=attempt,
            error_class=_fqn(exc),
            category=None,
            code=exc.code,
        )


def _code_of(exc: BaseException) -> str:
    """``exc`` の instance 属性 ``code`` を抽出する。

    未定義 / 空 / 非 str は catch-all label に fallback する。
    """
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else "unexpected_error"


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
