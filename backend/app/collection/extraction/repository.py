"""Extraction リポジトリ — DiscoveredArticle ルックアップと Article 永続化。

ingestion 側にも同名の ``DiscoveredArticleRepository`` が存在するが、
責務（URL 重複排除 vs 抽出対象ルックアップ）が異なるため名前空間で分離し
同名のまま扱う。各 Repo は Service 内部からのみ利用される想定。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.collection.extraction.candidate import (
    AlreadyExtracted,
    DiscoveredArticleLookup,
    DiscoveredNotFound,
    UnextractedDiscoveredArticle,
    UnextractedFound,
)
from app.collection.extraction.extractor import ExtractedContent
from app.models.article import Article
from app.models.discovered_article import DiscoveredArticle


class DiscoveredArticleRepository:
    """抽出対象となる ``DiscoveredArticle`` のルックアップを担う。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lookup_for_extraction(
        self, discovered_article_id: int
    ) -> DiscoveredArticleLookup:
        """抽出対象の状態を 1 クエリで判定して sum type で返す。

        - 行が存在しない → :class:`DiscoveredNotFound`
        - 行はあるが Article 未生成 → :class:`UnextractedFound`
        - 既に Article が存在する → :class:`AlreadyExtracted`
        """
        stmt = (
            select(DiscoveredArticle)
            .where(DiscoveredArticle.id == discovered_article_id)
            .options(selectinload(DiscoveredArticle.article))
        )
        discovered = (await self._session.execute(stmt)).scalar_one_or_none()
        if discovered is None:
            return DiscoveredNotFound()
        if discovered.article is not None:
            return AlreadyExtracted(article_id=discovered.article.id)
        return UnextractedFound(
            article=UnextractedDiscoveredArticle(
                id=discovered.id, url=discovered.original_url
            )
        )


class ArticleRepository:
    """抽出済みコンテンツから ``Article`` 行を作成する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def create(self, discovered_article_id: int, content: ExtractedContent) -> Article:
        """Article をセッションに追加して返す（commit / refresh は呼び出し側）。"""
        article = Article(
            discovered_article_id=discovered_article_id,
            original_title=content.title,
            original_content=content.body,
            published_at=(
                content.published_at.value if content.published_at is not None else None
            ),
        )
        self._session.add(article)
        return article
