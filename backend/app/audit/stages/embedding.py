"""Stage 5 embedding の監査イベントを組み立てる。"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Literal, assert_never

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_providers.errors import AIProviderError
from app.analysis.embedding.errors import EmbeddingError, EmbeddingFailureReason
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import BasePipelineEventPayload, EmbeddingPayload
from app.audit.error_chain import extract_error_chain
from app.audit.error_fields import error_message_of, exception_fqn
from app.audit.failure_projection import (
    FailureProjection,
    project_db_failure,
    unknown_failure_projection,
)
from app.audit.repository import PipelineEventRepository
from app.models.backfill_exclusion import BackfillExclusionReason

if TYPE_CHECKING:
    from app.analysis.embedding.ai.base import BaseEmbedder
    from app.analysis.embedding.domain.ready import EmbeddingReadyBuildRejected


class EmbeddingOutcomeCode(StrEnum):
    """Stage.EMBEDDING の outcome code (stage ファイル内定義分のみ)。"""

    COMPLETED = "embedding_completed"


class EmbeddingAuditRepository:
    """Stage 5 専用の payload / outcome_code / failure projection を決める。"""

    STAGE: ClassVar[Stage] = Stage.EMBEDDING
    BACKFILL_STAGE: ClassVar[Stage] = Stage.BACKFILL_EMBED

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    # --- 成功経路 (Service の業務 UPDATE と同 tx) -------------------------

    async def append_success(
        self,
        *,
        analyzed_article_id: int,
        article_id: int,
        embedder: BaseEmbedder,
    ) -> None:
        """embedding 成功を記録する。"""
        payload = EmbeddingPayload(
            analyzed_article_id=analyzed_article_id,
            ai_model=embedder.model_name,
            vector_dimension=embedder.dimension,
        )
        await self._append_event(
            event_type=EventType.SUCCEEDED,
            outcome_code=EmbeddingOutcomeCode.COMPLETED.value,
            payload=payload,
            article_id=article_id,
        )

    # --- 救済断念経路 (backfill exclusion と同一 tx) ----------------------

    async def append_backfill_embedding_aged_out(
        self,
        *,
        analyzed_article_id: int,
        analyzable_article_id: int,
    ) -> None:
        """古い embedding NULL analyzed article を対象外にした事実を記録する。"""
        await self._append_backfill_event(
            event_type=EventType.REJECTED,
            outcome_code=BackfillExclusionReason.EMBEDDING_AGED_OUT.value,
            payload=EmbeddingPayload(analyzed_article_id=analyzed_article_id),
            article_id=analyzable_article_id,
        )

    # --- Ready 構築 rejected / failed ---------------------------------------

    async def append_ready_build_rejected(
        self, *, analyzed_article_id: int, rejected: EmbeddingReadyBuildRejected
    ) -> None:
        """Ready側の拒否理由と確認済み記事IDをそのまま記録する。"""
        await self._append_event(
            event_type=EventType.REJECTED,
            outcome_code=rejected.reason.value,
            payload=EmbeddingPayload(analyzed_article_id=analyzed_article_id),
            article_id=rejected.analyzable_article_id,
        )

    # --- Consumerが扱いを決めた失敗の監査 ---

    async def append_failure(
        self,
        *,
        analyzed_article_id: int,
        article_id: int | None,
        exc: Exception,
        failure_action: Literal["retry", "no_retry"],
    ) -> None:
        """元の例外とConsumerが決めた扱いを、失敗監査へ記録する。"""
        projection = _project_failure(exc)
        payload = EmbeddingPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action,
            failure_reason=projection.failure_reason,
            analyzed_article_id=analyzed_article_id,
            ai_model=None,
            vector_dimension=None,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc),
        )
        await self._append_event(
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=payload,
            article_id=article_id,
            error_class=exception_fqn(exc),
        )

    # --- internal helpers -------------------------------------------------

    async def _append_event(
        self,
        *,
        event_type: EventType,
        outcome_code: str,
        payload: BasePipelineEventPayload,
        article_id: int | None = None,
        source_id: int | None = None,
        error_class: str | None = None,
    ) -> None:
        await self._events.append(
            stage=self.STAGE,
            event_type=event_type,
            outcome_code=outcome_code,
            payload=payload,
            article_id=article_id,
            source_id=source_id,
            error_class=error_class,
        )

    async def _append_backfill_event(
        self,
        *,
        event_type: EventType,
        outcome_code: str,
        payload: BasePipelineEventPayload,
        article_id: int | None = None,
        source_id: int | None = None,
        error_class: str | None = None,
    ) -> None:
        await self._events.append(
            stage=self.BACKFILL_STAGE,
            event_type=event_type,
            outcome_code=outcome_code,
            payload=payload,
            article_id=article_id,
            source_id=source_id,
            error_class=error_class,
        )


def _project_failure(exc: Exception) -> FailureProjection:
    """Consumerの失敗を監査の分類へ写す。"""
    if isinstance(exc, AIProviderError):
        return FailureProjection(
            code=exc.CODE,
            failure_kind=None,
            failure_reason=exc.reason.value,
            retryability=None,
            failure_action=None,
        )
    if isinstance(exc, EmbeddingError):
        match exc.reason:
            case EmbeddingFailureReason.ARTICLE_MISSING:
                failure_kind = "target_missing"
            case EmbeddingFailureReason.RESPONSE_INVALID:
                failure_kind = "ai_response_invalid"
            case _:
                assert_never(exc.reason)
        return FailureProjection(
            code=exc.code,
            failure_kind=failure_kind,
            retryability=None,
            failure_action=None,
        )
    return project_db_failure(exc) or unknown_failure_projection()
