"""EmbeddingService — Stage E のユースケース組み立てと永続化境界。

Pattern A' (typed-pipeline-preconditions.md §1.1 / §3.2 / §6.1) に従い、precondition
チェック (extraction / analysis 存在 + 既存 embedding 不在) は ``ReadyForEmbedding``
が構造保証するため Service は execute 内で行わない。Outcome は
``EmbeddedOutcome | InvalidInputOutcome`` の 2 variants に縮退する。

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
# EmbeddingOutcome — Service 戻り値の tagged union (Pattern A' で 2 variants に縮退)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbeddedOutcome:
    """新規に埋め込みが生成・永続化された (race 敗北 → 読戻し合流も含む)。"""

    embedding: Embedding


@dataclass(frozen=True, slots=True)
class InvalidInputOutcome:
    """embedder が入力を構造的に拒否した (該当記事のみ skip)。

    AlreadyEmbedded / SkippedOutcome は Pattern A' で precondition 型に責務移管
    したため廃止。残るのは embedder 自身が判断する入力品質問題のみ。
    """


EmbeddingOutcome = EmbeddedOutcome | InvalidInputOutcome
"""Stage E の実行結果型。"""


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

        Pattern A': 受け取った Ready は precondition (embedding 未生成) を構造
        保証している。embedder 入力テキストは ``in_scope_assessments`` 行から
        都度 fetch する (Stage 4 INSERT 後の不変 snapshot を DB SSoT として再 read、
        `feedback_bc_boundary_guarantees_downstream`)。AI 呼び出しと永続化で
        session を分けて slow IO 中の DB 接続専有を避ける既存原則を維持する。

        Raises:
            ``RuntimeError``: Pattern A' 違反 (analysis 行が消失) を fail-fast で
            可視化する (`feedback_failure_visibility`)。
            ``RateLimitError`` / ``ProviderError`` / ``NetworkError`` /
            ``ConfigurationError`` (Task 層 retry / 停止判断に委ねる)。
        """
        async with self._session_factory() as session:
            text = await EmbeddingRepository(session).fetch_text_for_embedding(
                ready.analysis_id
            )
        if text is None:
            raise RuntimeError(
                f"embedding_assessment_missing: analysis_id={ready.analysis_id}"
            )

        try:
            vector = await embedder.embed_document(text)
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
                    # Pattern A' 違反 (Ready 構築時に analysis 存在を前提と
                    # しているのに行が消失している) = DB 整合性異常で即死
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
