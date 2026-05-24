"""``PipelineEventRepository`` — append-only 監査イベント Repository。

Vector 既存 Repository 流儀: ``__init__(self, session)`` で session を ctor
で受け、``append()`` は instance method、commit は呼び出し側責務。
"""

from __future__ import annotations

import contextvars

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.categories import Layer1Category
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import BasePipelineEventPayload
from app.models.article import Article
from app.models.pipeline_event import PipelineEvent

# trace_id 伝播用 contextvar — PR1 では誰もセットしない (常に None)。
# post-v1 で OpenTelemetry / Logfire 連携時に span ID を入れる経路。
_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "pipeline_event_trace_id", default=None
)


class PipelineEventRepository:
    """append-only。session を ctor で受け、commit は呼出側責務。"""

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
        attempt: int = 1,
        duration_ms: int | None = None,
        error_class: str | None = None,
        category: Layer1Category | None = None,
        code: str | None = None,
    ) -> None:
        """1 行 INSERT。``commit()`` は呼出側で。

        ``category`` / ``code`` は article-bound analysis stages (extraction /
        assessment / embedding) のみ呼出側が指定する。dispatch / acquisition
        / content_fetch では ``Layer1Category`` の語彙が合わないため ``None`` の
        まま (DB 列は nullable + ``IS NULL OR`` を許容する CHECK 制約)。
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
            category=category.value if category is not None else None,
            code=code,
            source_id=source_id,
            article_id=article_id,
            attempt=attempt,
            duration_ms=duration_ms,
            error_class=error_class,
            trace_id=self._get_current_trace_id(),
            payload=payload.model_dump(mode="json", exclude_none=False),
        )
        self._session.add(event)

    @staticmethod
    def _get_current_trace_id() -> str | None:
        return _trace_id_var.get()
