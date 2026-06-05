"""Stage 5 embedding の監査イベントを組み立てる。"""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildBlockedError,
    ReadyForEmbedding,
)
from app.analysis.embedding.errors import EmbeddingError
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import EmbeddingPayload
from app.audit.error_chain import extract_error_chain
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    failure_action_value,
    project_failure,
    unknown_failure_projection,
)
from app.audit.ready_build import project_ready_build_failure
from app.audit.repository import PipelineEventRepository
from app.models.backfill_exclusion import BackfillExclusionReason
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
            analysis_id=ready.analysis_id,
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

    # --- 救済断念経路 (backfill exclusion と同一 tx) ----------------------

    async def append_backfill_embedding_aged_out(
        self,
        *,
        analysis_id: int,
        article_id: int,
    ) -> None:
        """古い embedding NULL analysis を backfill が対象外にした事実を記録する。"""
        await self._events.append(
            stage=Stage.BACKFILL_EMBED,
            event_type=EventType.REJECTED,
            outcome_code=BackfillExclusionReason.EMBEDDING_AGED_OUT.value,
            payload=EmbeddingPayload(analysis_id=analysis_id),
            article_id=article_id,
        )

    # --- Ready 構築 blocked / failed ---------------------------------------

    async def append_ready_build_blocked(
        self, *, analysis_id: int, exc: EmbeddingReadyBuildBlockedError
    ) -> None:
        """Ready 構築が domain precondition により進めなかった事実を記録する。

        Domain が reason code で説明できた停止なので rejected として焼く。
        """
        await self._events.append(
            stage=Stage.EMBEDDING,
            event_type=EventType.REJECTED,
            outcome_code=exc.code.value,
            payload=EmbeddingPayload(analysis_id=analysis_id),
        )

    async def append_ready_build_failed(
        self, *, analysis_id: int, exc: Exception
    ) -> None:
        """Ready 構築中に blocked 以外の例外が出た事実を failed として記録する。"""
        projection = project_ready_build_failure(stage_prefix="embedding", exc=exc)
        payload = EmbeddingPayload(
            failure_kind=projection.failure_kind,
            analysis_id=analysis_id,
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=Stage.EMBEDDING,
            event_type=EventType.FAILED,
            outcome_code=projection.outcome_code,
            payload=payload,
            error_class=_fqn(exc),
            retryability=Retryability.UNKNOWN,
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
            failure_reason=projection.failure_reason,
            analysis_id=ready.analysis_id,
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
