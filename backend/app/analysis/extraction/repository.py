"""Extraction リポジトリ — Stage 1 固有の DB 操作。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.article import Article
from app.models.article_extraction import ArticleExtraction


class ExtractionRepository:
    """事実抽出（Stage 1）に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_article_id(self, article_id: int) -> ArticleExtraction | None:
        """記事に対する既存の抽出結果を取得する（冪等性チェック兼用）。"""
        stmt = select(ArticleExtraction).where(
            ArticleExtraction.article_id == article_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_article(self, article_id: int) -> Article | None:
        """ID から記事を取得する。"""
        return await self._session.get(Article, article_id)

    async def save_extraction(self, extraction: ArticleExtraction) -> ArticleExtraction:
        """抽出結果を永続化する（flush のみ、commit しない）。"""
        self._session.add(extraction)
        await self._session.flush()
        return extraction
