"""EmbeddingService — Stage 5 の AI 埋め込み生成と永続化境界。

``ReadyForEmbedding`` が precondition と入力 text を保証するため、Service は
AI 呼び出し、条件付き保存、audit + commit だけを担う。楽観的ロックに敗れた
worker は audit / commit せず短絡する。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import AIProviderError
from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.errors import to_embedding_error
from app.analysis.embedding.metrics import record_embedding_processing_outcome
from app.analysis.embedding.repository import EmbeddingRepository
from app.audit.stages.embedding import EmbeddingAuditRepository
from app.logfire.article_stage import set_embedding_stage_result

logger = structlog.get_logger(__name__)


class EmbeddingService:
    """1 analysis の埋め込み生成と永続化を行うアトミックなユースケース。

    セッション管理はサービス内部で完結し、呼び出し側は session factory だけを渡す。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(self, ready: ReadyForEmbedding, embedder: BaseEmbedder) -> None:
        """Ready 型を入力に埋め込みベクトルを生成し永続化する。

        AI 呼び出しは session 外で行い、保存成功時のみ audit を同一 tx で commit
        する。並行 update に先を越された場合は業務正常パスとして短絡する。

        Raises:
            ``EmbeddingRecoverableError`` / ``EmbeddingTerminalError``
            (Task 層 2 marker dispatch に委ねる)。``AIProviderError`` は ACL で
            Stage 5 marker に詰め替えてから raise される。
        """
        try:
            vector = await embedder.embed_document(ready)
        except AIProviderError as exc:
            # Stage marker に詰め替え、audit で元 provider error まで辿れるよう
            # ``__cause__`` を保持する。
            raise to_embedding_error(exc) from exc

        async with self._session_factory() as session:
            saved = await EmbeddingRepository(session).save(
                vector,
                analyzed_article_id=ready.analyzed_article_id,
            )
            if not saved:
                # 楽観的ロック敗北時は、勝者だけが audit / commit する。
                logger.info(
                    "embedding_concurrent_write",
                    analyzed_article_id=ready.analyzed_article_id,
                )
                set_embedding_stage_result("skipped")
                return
            # 業務 UPDATE + audit を同一 tx で commit
            await EmbeddingAuditRepository(session).append_success(
                ready=ready,
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
