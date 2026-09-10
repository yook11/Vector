"""EmbeddingService — Stage 5 の AI 埋め込み生成と永続化境界。

AI処理後に保存対象を行ロックし、記事不存在・生成済み・未生成を区別する。
ベクトルと成功監査を同一トランザクションで保存する。
"""

from __future__ import annotations

from enum import StrEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import AIProviderError
from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.errors import (
    EmbeddingAnalyzedArticleMissingError,
    to_embedding_error,
)
from app.analysis.embedding.metrics import record_embedding_processing_outcome
from app.analysis.embedding.repository import EmbeddingRepository, EmbeddingSaveState
from app.audit.stages.embedding import EmbeddingAuditRepository
from app.logfire.article_stage import set_embedding_stage_result

logger = structlog.get_logger(__name__)


class EmbeddingCompletion(StrEnum):
    """保存完了または生成済みの確認による正常終了。"""

    SAVED = "saved"
    ALREADY_EMBEDDED = "already_embedded"


class EmbeddingService:
    """1 analysis の埋め込み生成と永続化を行うアトミックなユースケース。

    セッション管理はサービス内部で完結し、呼び出し側は session factory だけを渡す。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        ready: ReadyForEmbedding,
        embedder: BaseEmbedder,
        *,
        analyzable_article_id: int,
    ) -> EmbeddingCompletion:
        """Ready 型を入力に埋め込みベクトルを生成し永続化する。

        AI呼び出し後に行ロックを取得し、生成済みなら短絡、不存在なら失敗とする。
        未生成の記事だけを更新し、成功監査と同時にコミットする。

        Returns:
            保存完了はSAVED、生成済みの確認による終了はALREADY_EMBEDDED。

        Raises:
            EmbeddingError: 記事不存在・応答不正・プロバイダー障害。
            DB障害と想定外例外も、そのまま呼び出し元へ伝播する。
        """
        try:
            vector = await embedder.embed_document(ready)
        except AIProviderError as exc:
            # Stage marker に詰め替え、audit で元 provider error まで辿れるよう
            # ``__cause__`` を保持する。
            raise to_embedding_error(exc) from exc

        async with self._session_factory() as session:
            repo = EmbeddingRepository(session)
            state = await repo.lock_save_state(ready.analyzed_article_id)
            if state is EmbeddingSaveState.ARTICLE_MISSING:
                raise EmbeddingAnalyzedArticleMissingError()
            if state is EmbeddingSaveState.EMBEDDED:
                logger.info(
                    "embedding_concurrent_write",
                    analyzed_article_id=ready.analyzed_article_id,
                )
                set_embedding_stage_result("skipped")
                return EmbeddingCompletion.ALREADY_EMBEDDED
            saved = await repo.save(
                vector,
                analyzed_article_id=ready.analyzed_article_id,
            )
            if not saved:
                raise RuntimeError("embedding_update_missing_after_row_lock")
            # 業務 UPDATE + audit を同一 tx で commit
            await EmbeddingAuditRepository(session).append_success(
                analyzed_article_id=ready.analyzed_article_id,
                article_id=analyzable_article_id,
                embedder=embedder,
            )
            await session.commit()

        logger.info(
            "embedding_completed",
            analyzed_article_id=ready.analyzed_article_id,
            model=embedder.model_name,
        )
        set_embedding_stage_result("succeeded")
        record_embedding_processing_outcome("succeeded")
        return EmbeddingCompletion.SAVED
