"""Stage 5 embedding の監査イベントを組み立てる。"""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.errors import EmbeddingError
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import EmbeddingPayload
from app.audit.error_chain import extract_error_chain
from app.audit.failure_projection import (
    FailureProjection,
    failure_action_value,
    project_failure,
    unknown_failure_projection,
)
from app.audit.repository import PipelineEventRepository
from app.shared.security.redaction import redact_secrets

_ERROR_MESSAGE_LIMIT = 2000

_SUCCESS_OUTCOME_CODE = "embedding_completed"


class EmbeddingAuditRepository:
    """Stage 5 専用の payload / outcome_code / failure projection を決める。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    # --- 成功経路 (Service の業務 UPDATE と同 tx) -------------------------

    async def append_success(
        self,
        *,
        ready: ReadyForEmbedding,
        embedder: BaseEmbedder,
    ) -> None:
        """embedding 成功を記録する。"""
        payload = EmbeddingPayload(
            embedding_model=embedder.model_name,
            vector_dimension=embedder.dimension,
        )
        await self._events.append(
            stage=Stage.EMBEDDING,
            event_type=EventType.SUCCEEDED,
            outcome_code=_SUCCESS_OUTCOME_CODE,
            payload=payload,
            article_id=ready.article_id,
        )

    # --- 失敗経路 (Task 層 2 marker dispatch + catch-all、別 session 別 tx) -

    async def append_failure(
        self,
        *,
        ready: ReadyForEmbedding,
        exc: EmbeddingError | SQLAlchemyError,
    ) -> None:
        """embedding 失敗を記録する。"""
        projection = self._projection_of(exc)
        await self._append_failed_event(ready=ready, exc=exc, projection=projection)

    async def append_unexpected_failure(
        self,
        *,
        ready: ReadyForEmbedding,
        exc: BaseException,
    ) -> None:
        """想定外の embedding 失敗を unknown として記録する。"""
        await self._append_failed_event(
            ready=ready,
            exc=exc,
            projection=unknown_failure_projection(),
        )

    async def _append_failed_event(
        self,
        *,
        ready: ReadyForEmbedding,
        exc: BaseException,
        projection: FailureProjection,
    ) -> None:
        payload = EmbeddingPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            embedding_model=None,
            vector_dimension=None,
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=projection.stage or Stage.EMBEDDING,
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=payload,
            article_id=ready.article_id,
            error_class=_fqn(exc),
            retryability=projection.retryability,
        )

    # --- internal helpers -------------------------------------------------

    @staticmethod
    def _projection_of(exc: BaseException) -> FailureProjection:
        """Stage 5 失敗を class attr / adapter から projection する。"""
        return project_failure(exc)


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
