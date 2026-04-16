"""Embedding サービス — ベクトル生成と DB 永続化を担う。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.repository import AnalysisRepository
from app.models.news_article import NewsArticle

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EmbeddingResult:
    """埋め込み生成ユースケースの結果。"""

    status: Literal["created", "already_exists"]


def build_embed_text(article: NewsArticle) -> str:
    """記事を埋め込み対象とする際の正規テキストを組み立てる。"""
    body = article.original_content or article.original_description or ""
    return f"{article.original_title}\n{body}"


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
            repo = AnalysisRepository(session)

            # analysis は事前に生成済みである前提（analyze_article から連鎖される）
            analysis = await repo.find_by_article_id(article_id)
            if analysis is None:
                msg = f"No analysis found for article {article_id}"
                raise ValueError(msg)

            # 冪等性チェック
            if analysis.embedding is not None:
                return EmbeddingResult("already_exists")

            # 埋め込み対象テキスト用に記事を取得
            article = await repo.get_article(article_id)
            if article is None:
                msg = f"Article {article_id} not found"
                raise ValueError(msg)

            # 埋め込み生成（エラーはすべて Task 層まで伝播させる）
            text = build_embed_text(article)
            vector = await embedder.embed_document(text)

            # 永続化
            await repo.save_embedding(analysis, vector, embedder.MODEL)
            await session.commit()

            logger.info(
                "embedding_completed",
                article_id=article_id,
                model=embedder.MODEL,
            )
            return EmbeddingResult("created")
