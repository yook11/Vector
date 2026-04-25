"""EmbeddingService — Stage 3 のユースケース組み立てと永続化境界。

ドメイン層 (Draft / Entity) と AI 層 (BaseEmbedder) を結び、冪等性チェック →
embedding 生成 → 永続化 → commit の順序を担う。冪等分岐
(already_embedded / skipped) では commit を呼ばない。

Stage 1/2 と同型 (``ExtractionService`` / ``ClassificationService``) で、
戻り値は Outcome tagged union。Task 層は ``isinstance`` で chain するか判断する。

エラー処理方針 (feedback_error_handling_by_capability):
- ``InvalidInputError``: ユーザー入力起因の構造問題。Service で握って
  ``SkippedOutcome("invalid_input")`` に縮退する (該当記事のみ skip)。
- ``RateLimitError`` / ``ProviderError`` / ``NetworkError`` /
  ``ConfigurationError``: Service で握らず Task 層 (再試行 / バックオフ /
  停止判断) に伝搬させる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.classification.domain.analysis import Analysis
from app.analysis.classification.rejection_repository import RejectionRepository
from app.analysis.classification.repository import AnalysisRepository
from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedding.domain.embedding import Embedding, EmbeddingDraft
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.errors import InvalidInputError
from app.analysis.extraction.repository import ExtractionRepository

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# EmbeddingOutcome — Service 戻り値の tagged union
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbeddedOutcome:
    """新規に埋め込みが生成・永続化された。"""

    embedding: Embedding


@dataclass(frozen=True, slots=True)
class AlreadyEmbeddedOutcome:
    """同 analysis に既に埋め込みが存在する (冪等ヒット or 並行レース敗北)。"""

    embedding: Embedding


SkipReason = Literal[
    "extraction_not_found",
    "analysis_pending",
    "analysis_rejected",
    "invalid_input",
]


@dataclass(frozen=True, slots=True)
class SkippedOutcome:
    """Stage 3 を実行できなかった (前段未完了 or 入力不正)。

    Reasons:
    - ``extraction_not_found``: Stage 1 (extraction) が未完了
    - ``analysis_pending``: Stage 2 が未完了 (rejection も無し)
    - ``analysis_rejected``: Stage 2 で OutOfScope 判定済み (analysis 無し)
    - ``invalid_input``: embedder が入力を拒否した (該当記事のみ skip)
    """

    reason: SkipReason


EmbeddingOutcome = EmbeddedOutcome | AlreadyEmbeddedOutcome | SkippedOutcome
"""Stage 3 の実行結果型。"""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class EmbeddingService:
    """1 記事の埋め込み生成と永続化を行うアトミックなユースケース。

    セッションの管理はサービス内部で完結し、呼び出し側は session factory のみ
    渡す (feedback_session_factory_di)。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self, article_id: int, embedder: BaseEmbedder
    ) -> EmbeddingOutcome:
        """1 記事の analysis に対する埋め込みベクトルを生成する。

        Raises:
            ``RateLimitError`` / ``ProviderError`` / ``NetworkError`` /
            ``ConfigurationError`` (Task 層 retry / 停止判断に委ねる)。
        """
        async with self._session_factory() as session:
            extraction_repo = ExtractionRepository(session)
            analysis_repo = AnalysisRepository(session)
            rejection_repo = RejectionRepository(session)
            embedding_repo = EmbeddingRepository(session)

            extraction = await extraction_repo.find_by_article_id(article_id)
            if extraction is None:
                logger.info(
                    "embedding_skipped",
                    article_id=article_id,
                    reason="extraction_not_found",
                )
                return SkippedOutcome(reason="extraction_not_found")

            analysis = await analysis_repo.find_by_extraction_id(extraction.id)
            if analysis is None:
                rejected = await rejection_repo.find_by_extraction_id(extraction.id)
                reason: SkipReason = (
                    "analysis_rejected" if rejected is not None else "analysis_pending"
                )
                logger.info(
                    "embedding_skipped",
                    article_id=article_id,
                    extraction_id=extraction.id,
                    reason=reason,
                )
                return SkippedOutcome(reason=reason)

            existing = await embedding_repo.find_by_analysis_id(analysis.id)
            if existing is not None:
                logger.info(
                    "embedding_already_exists",
                    article_id=article_id,
                    analysis_id=analysis.id,
                )
                return AlreadyEmbeddedOutcome(embedding=existing)

            text = self._build_text(analysis)
            try:
                vector = await embedder.embed_document(text)
            except InvalidInputError:
                logger.info(
                    "embedding_skipped",
                    article_id=article_id,
                    analysis_id=analysis.id,
                    reason="invalid_input",
                )
                return SkippedOutcome(reason="invalid_input")

            draft = EmbeddingDraft.from_inference(vector=vector)
            saved = await embedding_repo.save(
                draft,
                analysis_id=analysis.id,
                model_name=embedder.MODEL,
            )
            if not saved:
                # 並行 save レース敗北: 他ワーカーが先に書いた行を読み戻す
                concurrent = await embedding_repo.find_by_analysis_id(analysis.id)
                if concurrent is None:
                    # CHECK 制約と save の WHERE 条件を考えるとここには来ないはず
                    # (analysis_id 不在は execute 冒頭で弾いている) が、防御的に扱う
                    raise RuntimeError("embedding save returned False but no row found")
                logger.info(
                    "embedding_already_exists",
                    article_id=article_id,
                    analysis_id=analysis.id,
                    note="concurrent_write",
                )
                return AlreadyEmbeddedOutcome(embedding=concurrent)

            await session.commit()
            embedding = Embedding.from_draft(
                draft,
                analysis_id=analysis.id,
                model_name=embedder.MODEL,
            )
            logger.info(
                "embedding_completed",
                article_id=article_id,
                analysis_id=analysis.id,
                model=embedder.MODEL,
            )
            return EmbeddedOutcome(embedding=embedding)

    @staticmethod
    def _build_text(analysis: Analysis) -> str:
        """分析結果から埋め込み対象の正規テキストを組み立てる。

        ``translated_title`` と ``summary`` を改行で連結する。
        """
        return f"{analysis.translated_title}\n{analysis.summary}"
