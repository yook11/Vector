"""Embedding サービス — ベクトル生成と DB 永続化を担う。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.extraction.repository import ExtractionRepository
from app.analysis.repository import AnalysisRepository
from app.models.article_analysis import ArticleAnalysis

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EmbeddingResult:
    """埋め込み生成ユースケースの結果。

    ``skipped`` は extraction が存在しない、または対応する analysis がない
    （=OutOfScope として rejection 側に振れた）ケース。
    """

    status: Literal["created", "already_exists", "skipped"]


def build_embed_text(analysis: ArticleAnalysis) -> str:
    """分析結果から埋め込み対象の正規テキストを組み立てる。"""
    return f"{analysis.translated_title}\n{analysis.summary}"


class EmbeddingService:
    """1 記事の埋め込み生成と永続化を行うアトミックなユースケース。

    セッションの管理はサービス内部で完結し、呼び出し側は session factory のみ渡す。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(self, article_id: int, embedder: BaseEmbedder) -> EmbeddingResult:
        """1 記事の analysis に対する埋め込みベクトルを生成する。

        Returns:
            status を含む EmbeddingResult。

        Raises:
            AnalysisDomainError のサブクラス。リトライ判断は呼び出し側の責務。
        """
        async with self._session_factory() as session:
            extraction_repo = ExtractionRepository(session)
            analysis_repo = AnalysisRepository(session)

            # extraction → analysis の順で辿る（rejected 記事は analysis がなく skip）
            extraction = await extraction_repo.find_by_article_id(article_id)
            if extraction is None:
                logger.warning("embedding_extraction_not_found", article_id=article_id)
                return EmbeddingResult("skipped")

            analysis = await analysis_repo.find_by_extraction_id(extraction.id)
            if analysis is None:
                # rejected 側に振れたか、まだ分類が終わっていない
                logger.warning(
                    "embedding_analysis_not_found",
                    article_id=article_id,
                    extraction_id=extraction.id,
                )
                return EmbeddingResult("skipped")

            # 冪等性チェック
            if analysis.embedding is not None:
                return EmbeddingResult("already_exists")

            # 埋め込み生成（エラーはすべて Task 層まで伝播させる）
            text = build_embed_text(analysis)
            vector = await embedder.embed_document(text)

            # 永続化
            await analysis_repo.save_embedding(analysis, vector, embedder.MODEL)
            await session.commit()

            logger.info(
                "embedding_completed",
                article_id=article_id,
                model=embedder.MODEL,
            )
            return EmbeddingResult("created")
