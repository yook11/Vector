"""EmbeddingService — Stage 5 のユースケース組み立てと永続化境界。

案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築) に従い、precondition
チェック (analysis 存在 + 既存 embedding 不在) と embedder 入力 text の取得は
``ReadyForEmbedding`` が構造保証する。本 Service は execute 内で
DB fetch / None チェックを行わず、``ready.text_for_embedding`` を直接 embedder に
渡す。Outcome は ``EmbeddedOutcome | InvalidInputOutcome`` の 2 variants に縮退する。

エラー処理方針 (feedback_error_handling_by_capability):
- ``InvalidInputError``: ユーザー入力起因の構造問題。Service で握って
  ``InvalidInputOutcome`` に縮退する (該当記事のみ skip)。
- ``RateLimitError`` / ``ProviderError`` / ``NetworkError`` /
  ``ConfigurationError``: Service で握らず Task 層 (再試行 / バックオフ /
  停止判断) に伝搬させる。

AI 呼び出しは session 外で行う (slow IO 中の DB 接続専有を排除 — Phase 1 で
確立した pattern)。
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedding.domain.embedding import Embedding, EmbeddingDraft
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.errors import InvalidInputError

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# EmbeddingOutcome — Service 戻り値の tagged union (案 3 で 2 variants に縮退)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbeddedOutcome:
    """新規に埋め込みが生成・永続化された (race 敗北 → 読戻し合流も含む)。"""

    embedding: Embedding


@dataclass(frozen=True, slots=True)
class InvalidInputOutcome:
    """embedder が入力を構造的に拒否した (該当記事のみ skip)。

    AlreadyEmbedded / SkippedOutcome は厚い Ready で precondition 型に責務移管
    したため廃止。残るのは embedder 自身が判断する入力品質問題のみ。
    """


EmbeddingOutcome = EmbeddedOutcome | InvalidInputOutcome
"""Stage 5 の実行結果型。"""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class EmbeddingService:
    """1 analysis の埋め込み生成と永続化を行うアトミックなユースケース。

    セッションの管理はサービス内部で完結し、呼び出し側は session factory のみ
    渡す (feedback_session_factory_di)。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self, ready: ReadyForEmbedding, embedder: BaseEmbedder
    ) -> EmbeddingOutcome:
        """Ready 型を入力に埋め込みベクトルを生成し永続化する。

        案 3: 受け取った Ready は precondition (analysis 存在 + embedding 未生成)
        + embedder 入力 text の全揃えを構造保証している。Service は値 fetch /
        None チェックを持たず、``ready.text_for_embedding`` を直接 embedder に
        渡す。

        Raises:
            ``RuntimeError``: race 敗北後の勝者読み戻しで行が消失している場合の
            fail-fast (Ready 構築時に存在を確認した analysis が処理中に消える
            異常状態、`feedback_failure_visibility`)。
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
            return InvalidInputOutcome()

        draft = EmbeddingDraft.from_inference(vector=vector)
        async with self._session_factory() as session:
            embedding_repo = EmbeddingRepository(session)
            saved = await embedding_repo.save(
                draft,
                analysis_id=ready.analysis_id,
                model_name=embedder.MODEL,
            )
            await session.commit()
            if saved is None:
                # 並行 save レース敗北: 他ワーカーが先に書いた行を読み戻す
                logger.info(
                    "embedding_concurrent_write",
                    analysis_id=ready.analysis_id,
                )
                saved = await embedding_repo.find_by_analysis_id(
                    ready.analysis_id,
                )
                if saved is None:
                    # Ready 構築時に存在を確認した analysis が処理中に消失
                    # = DB 整合性異常で即死
                    raise RuntimeError(
                        "embedding_race_winner_missing: "
                        f"analysis_id={ready.analysis_id}"
                    )

        logger.info(
            "embedding_completed",
            analysis_id=ready.analysis_id,
            model=embedder.MODEL,
        )
        return EmbeddedOutcome(embedding=saved)
