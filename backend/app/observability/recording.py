"""例外パス用の監査書込ヘルパー。

業務処理が例外 raise した場合、業務 session は rollback される (同 tx で
監査書込していたら一緒に消える)。本モジュールは **新 session で別 tx** を
開いて FAILED 行を書く。

3 段防御:
- 第 1 防御: DB INSERT (本ヘルパー)
- 第 2 防御: 失敗時 ``structlog`` で構造化ログ (業務エラー + 監査エラーを両方)
- 第 3 防御: ``articles`` の "穴" 症状検知 (運用 SQL、本実装範囲外)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.observability.categories import Layer1Category
from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import (
    BasePipelineEventPayload,
    ClassificationPayload,
    ContentFetchPayload,
    DispatchPayload,
    EmbeddingPayload,
    ExtractionPayload,
    SourceFetchPayload,
)
from app.observability.repository import PipelineEventRepository

logger = structlog.get_logger(__name__)

_MAX_CHAIN_DEPTH = 8
_ERR_MSG_LIMIT = 2000

_PAYLOAD_BY_STAGE: dict[Stage, type[BasePipelineEventPayload]] = {
    Stage.DISPATCH: DispatchPayload,
    Stage.SOURCE_FETCH: SourceFetchPayload,
    Stage.CONTENT_FETCH: ContentFetchPayload,
    Stage.EXTRACTION: ExtractionPayload,
    Stage.CLASSIFICATION: ClassificationPayload,
    Stage.EMBEDDING: EmbeddingPayload,
    # backfill_* は PR4 で対応 (専用 Payload variant が必要かを判断後に追加)
}


def _extract_error_chain(exc: BaseException) -> list[str]:
    """``__cause__`` / ``__context__`` を辿って FQN リスト化。

    深さ上限 ``_MAX_CHAIN_DEPTH`` + ``id()`` 集合で循環防止。``__cause__``
    優先、無ければ ``__context__``。
    """
    chain: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and len(chain) < _MAX_CHAIN_DEPTH:
        if id(cur) in seen:
            break
        seen.add(id(cur))
        chain.append(f"{type(cur).__module__}.{type(cur).__qualname__}")
        cur = cur.__cause__ or cur.__context__
    return chain


def build_failure_payload(
    stage: Stage,
    exc: BaseException,
    extra: Mapping[str, Any] | None = None,
) -> BasePipelineEventPayload:
    """Stage に対応する Payload variant を組み立てる。"""
    cls = _PAYLOAD_BY_STAGE[stage]
    base: dict[str, Any] = {
        "error_chain": _extract_error_chain(exc),
        "error_message": str(exc)[:_ERR_MSG_LIMIT] or None,
    }
    if extra:
        base.update(extra)
    return cls(**base)


async def _record_failure_event(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    stage: Stage,
    outcome_code: str,
    exc: BaseException,
    attempt: int,
    duration_ms: int | None,
    source_id: int | None = None,
    article_id: int | None = None,
    payload_extra: Mapping[str, Any] | None = None,
    category: Layer1Category | None = None,
) -> None:
    """例外パス用、新 session で別 tx commit。

    ``category`` は article-bound analysis stages (extraction / classification /
    embedding) のみ呼出側が指定する。collection 系 (dispatch / source_fetch /
    content_fetch) は ``None`` のままで良い (``Layer1Category`` の語彙が合わない)。

    第 1 防御に失敗した場合は ``structlog.exception`` で fallback ログを残す
    (業務エラーと監査エラーを必ず両方 key にする)。
    """
    error_class_fqn = f"{type(exc).__module__}.{type(exc).__qualname__}"
    try:
        payload = build_failure_payload(stage, exc, payload_extra)
        async with session_factory() as session:
            repo = PipelineEventRepository(session)
            await repo.append(
                stage=stage,
                event_type=EventType.FAILED,
                outcome_code=outcome_code,
                payload=payload,
                source_id=source_id,
                article_id=article_id,
                attempt=attempt,
                duration_ms=duration_ms,
                error_class=error_class_fqn,
                category=category,
            )
            await session.commit()
    except Exception as audit_exc:
        logger.exception(
            "pipeline_event_record_failure_dropped",
            stage=stage.value,
            outcome_code=outcome_code,
            attempt=attempt,
            source_id=source_id,
            article_id=article_id,
            business_error_class=error_class_fqn,
            business_error_message=str(exc)[:500],
            audit_error_class=(
                f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
            ),
            audit_error_message=str(audit_exc)[:500],
        )
