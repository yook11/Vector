"""append-only の pipeline_events repository。"""

from __future__ import annotations

import contextvars

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import BasePipelineEventPayload
from app.audit.failure_projection import Retryability
from app.models.article import Article
from app.models.pipeline_event import PipelineEvent

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "pipeline_event_trace_id", default=None
)


class PipelineEventRepository:
    """1 行の監査イベントを追加する。commit は呼び出し側が担う。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        stage: Stage,
        event_type: EventType,
        outcome_code: str,
        payload: BasePipelineEventPayload,
        article_id: int | None = None,
        source_id: int | None = None,
        duration_ms: int | None = None,
        error_class: str | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        """1 行 INSERT。``commit()`` は呼出側で。

        ``outcome_code`` が event code の主契約。失敗の retry 軸は
        ``retryability``、stage-local な失敗詳細は payload に保存する。
        """
        # source_id 自動補完: article_id だけ与えられた場合に逆引き
        if source_id is None and article_id is not None:
            source_id = await self._session.scalar(
                select(Article.source_id).where(Article.id == article_id)
            )

        event = PipelineEvent(
            stage=stage.value,
            event_type=event_type.value,
            outcome_code=outcome_code,
            retryability=retryability.value if retryability is not None else None,
            source_id=source_id,
            article_id=article_id,
            duration_ms=duration_ms,
            error_class=error_class,
            trace_id=self._get_current_trace_id(),
            payload=payload.model_dump(mode="json", exclude_none=False),
        )
        self._session.add(event)

    @staticmethod
    def _get_current_trace_id() -> str | None:
        return _trace_id_var.get()
