"""Stage 5 (embedding) 専用の pipeline_events 監査リポジトリ。

監査 row の **shape SSoT**。Service / Task / application helper は本 class の
semantic method を呼ぶだけで、``EmbeddingPayload`` の組み立て・
``PipelineEventRepository.append()`` の引数列・``error_chain`` の FQN 組み立て
を一切知らない。

tx 境界は呼出側が握る (本 class は ``await session.commit()`` を呼ばない)。

設計 (Stage 4 と完全同形):

- ``append_success`` は成功 audit で、Service の業務 UPDATE (in_scope_assessments
  の embedding カラム書込) と同 tx に焼く。``outcome_code`` (``"embedding_completed"``)
  は Repository 内で hardcode (caller は固定文字列を持たない)
- ``append_failure`` は Task 層 2 marker dispatch + catch-all 経路で別 session
  別 tx として焼く (``exc`` から ``category`` / ``code`` を内部導出する SSoT)
- ``article_id`` は ``ReadyForEmbedding`` が運ぶため AuditRepository 内での
  DB 逆引きは不要 (案 3 = 厚い Ready)
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.audit.categories import Layer1Category
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import EmbeddingPayload
from app.audit.error_chain import extract_error_chain
from app.audit.failure_projection import (
    FailureProjection,
    legacy_category_for_projection,
    project_failure,
)
from app.audit.repository import PipelineEventRepository
from app.shared.security.redaction import redact_secrets

_ERROR_MESSAGE_LIMIT = 2000  # foundation 共通 (Stage 4 と同値)

_SUCCESS_OUTCOME_CODE = "embedding_completed"


class EmbeddingAuditRepository:
    """Stage 5 監査 row の semantic API。

    内部で ``PipelineEventRepository`` を compose し、generic な append SQL は
    そちらに委譲する。本 class の責務は **Stage 5 固有の payload shape と
    Layer1Category / code の決定** に閉じる。
    """

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
        """embedding 成功 audit を 1 行記録する。

        Service が業務 UPDATE と同 tx で呼ぶ。``embedder`` から ``model_name`` /
        ``dimension`` を property 経由で読み、``ready`` から ``article_id`` を取り出す
        (案 3 = 厚い Ready、DB 逆引き不要)。
        """
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
            category=Layer1Category.SUCCESS,
            code=_SUCCESS_OUTCOME_CODE,
        )

    # --- 失敗経路 (Task 層 2 marker dispatch + catch-all、別 session 別 tx) -

    async def append_failure(
        self,
        *,
        ready: ReadyForEmbedding,
        exc: BaseException,
        attempt: int,
    ) -> None:
        """2 marker dispatch + catch-all 経路の failure audit を 1 行記録する。

        ``category`` / ``code`` は ``exc`` から自動導出 (Layer 1 marker
        ``isinstance`` 分岐 + instance 属性 ``exc.code`` 抽出)。Service と独立に
        Task 層 dispatch 経路から **別 session 別 tx** として呼ばれる
        (caller は ``tasks.py`` の task 関数末尾で別 session を開閉 + commit;
        PR4 で helper 廃止、task 末尾に inline)。
        commit は caller 側で行う (本 method は単一行 append のみ)。

        ``error_chain`` は ``error_chain.py::extract_error_chain`` を再利用して
        ``__cause__`` / ``__context__`` を辿る。
        ``raise to_embedding_error(exc) from exc`` する想定のため、
        wrapper marker (``EmbeddingRecoverableError`` 等) と元 ``AIProviderError``
        の両方を payload に残す必要がある (Stage 4 と同 pattern、chain walking 必須)。
        """
        payload = EmbeddingPayload(
            embedding_model=None,
            vector_dimension=None,
            # red-team chain γ-2: SDK exception message に key prefix /
            # Authorization header が混入する経路を redact してから永続化
            # (Stage 4 と同 pattern)。
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            # `raise X from exc` 連鎖を辿って FQN 列を payload に残す。
            error_chain=extract_error_chain(exc),
        )
        projection = self._projection_of(exc)
        category = legacy_category_for_projection(
            stage=Stage.EMBEDDING, projection=projection
        )
        code = projection.code
        await self._events.append(
            stage=Stage.EMBEDDING,
            event_type=EventType.FAILED,
            outcome_code=code,
            payload=payload,
            article_id=ready.article_id,
            attempt=attempt,
            error_class=_fqn(exc),
            category=category,
            code=code,
        )

    # --- internal helpers -------------------------------------------------

    @staticmethod
    def _projection_of(exc: BaseException) -> FailureProjection:
        """Stage 5 失敗を class attr / adapter から projection する。"""
        return project_failure(exc)

    @staticmethod
    def _category_of(exc: BaseException) -> Layer1Category:
        """互換用: projection から legacy ``category`` を導出する。"""
        return legacy_category_for_projection(
            stage=Stage.EMBEDDING,
            projection=EmbeddingAuditRepository._projection_of(exc),
        )

    @staticmethod
    def _code_of(exc: BaseException) -> str:
        """互換用: projection から ``code`` を導出する。"""
        return EmbeddingAuditRepository._projection_of(exc).code


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
