"""EmbeddingService — Stage 5 のユースケース組み立てと永続化境界。

案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築) に従い、precondition
チェック (analysis 存在 + 既存 embedding 不在) と embedder 入力 text の取得は
``ReadyForEmbedding`` が構造保証する。本 Service は execute 内で
DB fetch / None チェックを行わず、``ready.text_for_embedding`` を直接 embedder に
渡す。

Stage 5 は pipeline 終端ゆえ Outcome / Entity の伝搬価値が無いため、execute は
副作用 (永続化) のみを行い ``None`` を返す (Stage 4 Assessment 同型)。楽観ロック
採用上不可避な「並行 update で先に書き込まれていたため自分の save が空振り
する」状況は業務正常パスとして log + 短絡で抜ける (読戻しは行わない)。

エラー処理方針 (feedback_error_handling_by_capability):
- ``InvalidInputError``: ユーザー入力起因の構造問題。Service で握って log +
  ``None`` 短絡 (該当記事のみ skip)。
- ``RateLimitError`` / ``ProviderError`` / ``NetworkError`` /
  ``ConfigurationError``: Service で握らず Task 層 (再試行 / バックオフ /
  停止判断) に伝搬させる。

AI 呼び出しは session 外で行う (slow IO 中の DB 接続専有を排除 — Phase 1 で
確立した pattern)。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.embedding import EmbeddingDraft
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.errors import InvalidInputError

logger = structlog.get_logger(__name__)


class EmbeddingService:
    """1 analysis の埋め込み生成と永続化を行うアトミックなユースケース。

    セッションの管理はサービス内部で完結し、呼び出し側は session factory のみ
    渡す (feedback_session_factory_di)。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(self, ready: ReadyForEmbedding, embedder: BaseEmbedder) -> None:
        """Ready 型を入力に埋め込みベクトルを生成し永続化する。

        案 3: 受け取った Ready は precondition (analysis 存在 + embedding 未生成)
        + embedder 入力 text の全揃えを構造保証している。Service は値 fetch /
        None チェックを持たず、``ready.text_for_embedding`` を直接 embedder に
        渡す。

        Raises:
            ``RateLimitError`` / ``ProviderError`` / ``NetworkError`` /
            ``ConfigurationError`` (Task 層 retry / 停止判断に委ねる)。
        """
        try:
            vector = await embedder.embed_document(ready.text_for_embedding)
        except InvalidInputError:
            logger.info(
                "embedding_input_rejected",
                analysis_id=ready.analysis_id,
            )
            return

        draft = EmbeddingDraft.from_inference(vector=vector)
        async with self._session_factory() as session:
            embedding_repo = EmbeddingRepository(session)
            saved = await embedding_repo.save(
                draft,
                analysis_id=ready.analysis_id,
                model_name=embedder.MODEL,
            )
            await session.commit()

        if not saved:
            # 楽観ロックにより並行 update で先に書き込まれていた → 業務正常パス
            logger.info(
                "embedding_concurrent_write",
                analysis_id=ready.analysis_id,
            )
            return

        logger.info(
            "embedding_completed",
            analysis_id=ready.analysis_id,
            model=embedder.MODEL,
        )
