"""Stage 1 (source_fetch) 専用の pipeline_events 監査リポジトリ。

監査 row の **shape SSoT**。Handler は本 class の semantic method を呼ぶだけで、
``SourceFetchPayload`` の組み立て・``PipelineEventRepository.append()`` の引数列・
``error_chain`` の FQN 組み立てを知らない。

- 失敗経路のみ (``append_failure``)。成功側 audit (件数 / breakdown 集計) は
  本 spec 範囲外で ``FetchLog`` が担う。
- ``category`` は collection stage では ``Layer1Category`` の語彙が合わないため
  常に ``NULL`` (foundation taxonomy 準拠、``_category_of`` は持たない)。
- ``code`` は origin ``ExternalFetchError.CODE`` (= ``SourceFetchError.code``) を
  top-level 列に焼く。``payload`` には ``code`` を二重に持たせない (state は
  top-level 軸で完全識別可能)。
- tx 境界は呼出側が握る (本 class は ``await session.commit()`` を呼ばない)。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import SourceFetchPayload
from app.observability.recording import _extract_error_chain
from app.observability.redact import redact_secrets
from app.observability.repository import PipelineEventRepository

_ERROR_MESSAGE_LIMIT = 2000  # foundation 共通 (Extraction / Assessment と同値)


class SourceFetchAuditRepository:
    """Stage 1 監査 row の semantic API。

    内部で ``PipelineEventRepository`` を compose し、generic な append SQL は
    そちらに委譲する。本 class の責務は Stage 1 固有の ``SourceFetchPayload``
    shape と ``code`` 決定に閉じる (``category`` は常に ``NULL``)。
    """

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

        ``code`` は ``exc`` (通常 ``SourceFetchError``、想定外時は素の
        ``Exception``) の instance 属性 ``code`` から導出する。Stage 1 は
        ``category`` を持たない (foundation taxonomy、常に ``NULL``)。commit は
        呼出側で行う (本 method は単一行 append のみ)。

        ``error_chain`` は ``recording.py::_extract_error_chain`` を再利用して
        ``raise SourceFetchError(...) from exc`` の ``__cause__`` 連鎖
        (origin ``ExternalFetchError``) まで FQN 列に残す。
        """
        code = _code_of(exc)
        payload = SourceFetchPayload(
            source_name=source_name,
            # red-team chain γ-2: exception message に混入しうる secret prefix を
            # redact してから永続化する (他 Stage と同 pattern)。
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=_extract_error_chain(exc),
        )
        await self._events.append(
            stage=Stage.SOURCE_FETCH,
            event_type=EventType.FAILED,
            outcome_code=code,
            payload=payload,
            source_id=source_id,
            attempt=attempt,
            error_class=_fqn(exc),
            category=None,
            code=code,
        )


def _code_of(exc: BaseException) -> str:
    """``exc`` の instance 属性 ``code`` を抽出する。

    Stage 1 marker (``SourceFetchError``) は ctor で ``code: str`` を必須キーワード
    として受け instance attr に保持する設計。未定義 / 空 / 非 str は想定外
    Exception 経路として catch-all label に fallback する。
    """
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else "unexpected_error"


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
