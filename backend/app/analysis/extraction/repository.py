"""Extraction リポジトリ — Stage 1 固有の DB 操作。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.article_analysis import ArticleAnalysis
from app.models.news_article import NewsArticle


class ExtractionRepository:
    """事実抽出（Stage 1）に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_already_analyzed(self, article_id: int) -> bool:
        """この記事に対する分析結果が既に存在するかを返す。"""
        stmt = select(ArticleAnalysis.id).where(
            ArticleAnalysis.news_article_id == article_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def get_article(self, article_id: int) -> NewsArticle | None:
        """ID から記事を取得する。"""
        return await self._session.get(NewsArticle, article_id)

    async def save_analysis(self, analysis: ArticleAnalysis) -> ArticleAnalysis:
        """分析結果を永続化する（flush のみ、commit しない）。"""
        self._session.add(analysis)
        await self._session.flush()
        return analysis

    async def mark_article_skipped(self, article: NewsArticle) -> None:
        """記事を恒久的にスキップ対象としてマークする。"""
        article.discard_content()
        self._session.add(article)
